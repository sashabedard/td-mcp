"""YouTube ingest — Phase 4.2.

Pipeline: yt-dlp lists/downloads audio from a channel → openai-whisper
transcribes locally → chunks land in vector KB as source="tutorial".

Cross-platform by design: openai-whisper runs CPU by default on Mac,
CUDA on Linux/Windows with NVIDIA. Default model = "base" (~140MB,
~5x realtime on CPU) to keep iteration fast; override via env var
TD_MCP_WHISPER_MODEL=large-v3 for production transcripts (requires the
PC's RTX 4080 to be practical at scale).

Cache layout:
    ~/.cache/td-mcp/youtube/<channel_handle>/<video_id>/
        meta.json     — video metadata (title, duration, url, ...)
        audio.m4a     — downloaded audio (kept for re-transcription)
        transcript.txt — plain text from whisper
        transcript.json — segments with timestamps

Separation between download/transcription/chunking means each step is
restartable and idempotent — re-running skips work already on disk.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from td_mcp.kb.vector import Chunk
from td_mcp.util import write_json_atomic

DEFAULT_CACHE_DIR = Path(
    os.environ.get(
        "TD_MCP_YOUTUBE_CACHE",
        str(Path.home() / ".cache" / "td-mcp" / "youtube"),
    )
)
DEFAULT_WHISPER_MODEL = os.environ.get("TD_MCP_WHISPER_MODEL", "base")
SOURCES_CONFIG = Path(__file__).parent.parent / "kb" / "data" / "youtube_sources.json"
# YouTube's anti-bot behaviour changes every few months, so nothing here is
# hardcoded. The 2026-07 workaround (browser cookies + web_safari client +
# HLS) had fully expired by 2026-08: web_safari now returns no formats at
# all, and *stale* browser cookies are worse than none — YouTube degrades
# the session to images-only instead of refusing it outright. Empty defaults
# let yt-dlp rotate player clients on its own, which is what works now.
# Set these only when a specific breakage demands it.
YTDLP_COOKIES_BROWSER = os.environ.get("TD_MCP_YTDLP_COOKIES_BROWSER", "")
YTDLP_FORMAT_SORT = os.environ.get("TD_MCP_YTDLP_FORMAT_SORT", "")
# Fallback chain rather than a single pinned client: YouTube's posture
# shifts week to week (during one 2026-08 session 'web' went from working
# to failing within minutes), and some clients hand back format metadata
# they then 403 on — android_vr advertises format 140 and refuses the
# bytes, which is why yt-dlp's own rotation fails intermittently. Tried in
# order, first one to actually deliver audio wins. Trailing "" = no
# extractor arg, i.e. let yt-dlp rotate on its own as a last resort.
YTDLP_PLAYER_CLIENTS = [
    c.strip()
    for c in os.environ.get(
        "TD_MCP_YTDLP_PLAYER_CLIENTS", "web_embedded,mweb,android,"
    ).split(",")
]


@dataclass
class VideoMeta:
    video_id: str
    title: str
    duration_sec: float
    channel: str
    url: str

    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "duration_sec": self.duration_sec,
            "channel": self.channel,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VideoMeta":
        return cls(**d)


def load_sources() -> dict:
    return json.loads(SOURCES_CONFIG.read_text())


def list_channel_videos(channel_url: str, limit: int | None = None) -> list[VideoMeta]:
    """Use yt-dlp's flat-playlist mode to enumerate videos without downloading."""
    # ASCII Unit Separator (0x1f) won't appear in YouTube titles (which often
    # contain '|' as a separator), so it's safe as a field delimiter.
    # The title goes LAST as defence in depth: it is the only free-text
    # field, so anything it smuggles past the delimiter (a newline splits
    # one logical row across two printed lines) can only truncate the title
    # instead of shifting channel and url out of alignment. Three cached
    # records still carry that damage from the era when '|' was the
    # delimiter — a corrupt url is the costly one, since chunks cite
    # f"{meta.url}&t=..." and a mangled url is a dead source link.
    SEP = "\x1f"
    cmd = ["yt-dlp", "--quiet"]
    if YTDLP_COOKIES_BROWSER:
        cmd += ["--cookies-from-browser", YTDLP_COOKIES_BROWSER]
    cmd += [
        "--flat-playlist",
        "--print",
        # playlist_channel, NOT channel: --flat-playlist never populates the
        # per-entry channel field, it prints "NA" for every video on every
        # channel. That silently attributed 133 of 152 cached videos to a
        # channel named "NA" — and the attribution is embedded into chunk
        # text, so those videos were unfindable by channel name.
        f"%(id)s{SEP}%(duration)s{SEP}%(playlist_channel)s{SEP}%(webpage_url)s{SEP}%(title)s",
    ]
    if limit:
        cmd.extend(["--playlist-end", str(limit)])
    cmd.append(channel_url)

    # timeout: a hung yt-dlp (network stall, interactive cookie prompt)
    # otherwise blocks the MCP server indefinitely.
    out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=120)
    videos = []
    for line in out.strip().splitlines():
        parts = line.split(SEP, 4)
        if len(parts) != 5:
            continue
        vid, duration, channel, url, title = parts
        try:
            dur = float(duration) if duration and duration != "NA" else 0.0
        except ValueError:
            dur = 0.0
        videos.append(VideoMeta(vid, title, dur, channel, url))
    return videos


def _video_dir(channel_handle: str, video_id: str, cache_dir: Path) -> Path:
    return cache_dir / _safe_handle(channel_handle) / video_id


def _safe_handle(handle: str) -> str:
    """Strip leading @, replace slashes — file-system safe folder name."""
    return handle.lstrip("@").replace("/", "_")


def _cached_audio(vdir: Path) -> Path | None:
    """Complete cached audio file, whatever the extension. Partial
    downloads (.part, .ytdl) don't count."""
    for candidate in sorted(vdir.glob("audio.*")):
        if candidate.suffix in (".part", ".ytdl", ".tmp"):
            continue
        return candidate
    return None


def _ytdlp_cmd(
    url: str,
    out_template: str,
    format_selector: str,
    *,
    cookies_browser: str,
    player_client: str,
) -> list[str]:
    cmd = ["yt-dlp", "--quiet"]
    if cookies_browser:
        cmd += ["--cookies-from-browser", cookies_browser]
    if player_client:
        cmd += ["--extractor-args", f"youtube:player_client={player_client}"]
    if YTDLP_FORMAT_SORT:
        cmd += ["-S", YTDLP_FORMAT_SORT]
    # "--" so a video id beginning with '-' isn't parsed as a flag.
    cmd += ["-f", format_selector, "-o", out_template, "--", url]
    return cmd


def _download_attempts() -> list[tuple[str, str]]:
    """(cookies_browser, player_client) pairs to try, in order.

    Anonymous first across the whole client chain. Cookies come last and
    only if configured: a browser jar rotates and goes stale silently, and
    YouTube answers a stale jar by degrading the session to images-only
    rather than refusing it — worse than sending no cookies at all.
    """
    attempts = [("", client) for client in YTDLP_PLAYER_CLIENTS]
    if YTDLP_COOKIES_BROWSER:
        attempts.append((YTDLP_COOKIES_BROWSER, YTDLP_PLAYER_CLIENTS[0]))
    return attempts


def run_ytdlp_download(
    url: str, out_template: str, format_selector: str, *, timeout: int = 1800
) -> None:
    """Download `url` with yt-dlp, walking the player-client fallback chain.

    Shared by the audio and video paths deliberately: the two had separate
    copies of the same hardcoded workaround, so the 2026-07 arguments went
    stale in both places at once and only one got fixed. One chain, one
    place to update when YouTube shifts again.

    Raises RuntimeError carrying each attempt's stderr — callers decide
    whether that's fatal or a video to skip.
    """
    errors = []
    for cookies_browser, player_client in _download_attempts():
        cmd = _ytdlp_cmd(
            url,
            out_template,
            format_selector,
            cookies_browser=cookies_browser,
            player_client=player_client,
        )
        proc = subprocess.run(
            cmd, stderr=subprocess.PIPE, text=True, timeout=timeout, check=False
        )
        if proc.returncode == 0:
            return
        label = player_client or "auto"
        if cookies_browser:
            label += f"+cookies:{cookies_browser}"
        errors.append(f"[{label}] {(proc.stderr or '').strip()[-200:] or 'no stderr'}")
    raise RuntimeError(
        f"yt-dlp failed for {url} after {len(errors)} attempts: " + " | ".join(errors)
    )


def download_audio(meta: VideoMeta, channel_handle: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    """Download bestaudio as m4a. Skip if already cached — including when
    yt-dlp fell back to another container (.webm): resume must not hit the
    network for every non-m4a video on every run."""
    vdir = _video_dir(channel_handle, meta.video_id, cache_dir)
    vdir.mkdir(parents=True, exist_ok=True)
    audio_path = vdir / "audio.m4a"
    meta_path = vdir / "meta.json"

    if not meta_path.exists():
        write_json_atomic(meta_path, meta.to_dict(), indent=2)

    cached = _cached_audio(vdir)
    if cached is not None:
        return cached

    run_ytdlp_download(
        meta.url,
        str(audio_path).replace(".m4a", ".%(ext)s"),
        "bestaudio[ext=m4a]/bestaudio/best",
    )

    # yt-dlp may have written a different extension if m4a wasn't available
    cached = _cached_audio(vdir)
    return cached if cached is not None else audio_path


def transcribe(audio_path: Path, model_name: str = DEFAULT_WHISPER_MODEL) -> dict:
    """Run openai-whisper on an audio file. Returns the full result dict
    (segments + text). Caches result alongside the audio as transcript.json."""
    txt_path = audio_path.parent / "transcript.txt"
    json_path = audio_path.parent / "transcript.json"

    if txt_path.exists() and json_path.exists():
        return json.loads(json_path.read_text())

    # Mac fix: openai-whisper uses urllib for model download which doesn't
    # pick up the certifi bundle by default. Set the env var BEFORE importing
    # whisper so the first-time download succeeds behind corporate proxies.
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())

    import whisper  # lazy — model load is heavy

    model = whisper.load_model(model_name)
    result = model.transcribe(str(audio_path), verbose=False)
    txt_path.write_text(result["text"])
    # Strip non-serializable internal fields before persisting
    serializable = {
        "text": result["text"],
        "language": result.get("language", ""),
        "segments": [
            {
                "id": s["id"],
                "start": s["start"],
                "end": s["end"],
                "text": s["text"],
            }
            for s in result.get("segments", [])
        ],
    }
    write_json_atomic(json_path, serializable, indent=2)
    return serializable


def _segment_into_chunks(transcript: dict, max_words: int = 400) -> list[tuple[float, float, str]]:
    """Group whisper segments into ~max_words chunks. Each chunk is
    (start_sec, end_sec, text). Splitting on segment boundaries preserves
    sentence integrity which matters for embedding quality."""
    chunks: list[tuple[float, float, str]] = []
    current: list[dict] = []
    current_words = 0

    for seg in transcript.get("segments", []):
        seg_text = seg["text"].strip()
        seg_words = len(seg_text.split())
        if current_words + seg_words > max_words and current:
            text = " ".join(s["text"].strip() for s in current)
            chunks.append((current[0]["start"], current[-1]["end"], text))
            current = []
            current_words = 0
        current.append(seg)
        current_words += seg_words

    if current:
        text = " ".join(s["text"].strip() for s in current)
        chunks.append((current[0]["start"], current[-1]["end"], text))

    return chunks


def _attribution(meta: VideoMeta, channel_handle: str) -> str:
    """How a chunk names its channel — this string is embedded, so it is
    what channel-name search actually matches against.

    Carries the handle alongside the display name because neither alone is
    sufficient: legacy records hold a useless display name ("NA"), while
    some real names are poor discriminators (Derivative's channel is
    literally called "TouchDesigner"). The handle comes from the cache
    folder, which is authoritative by construction.
    """
    handle = _safe_handle(channel_handle)
    display = (meta.channel or "").strip()
    if not display or display == "NA":
        return handle
    # Several channels already carry the handle in their display name
    # ("bileam tschepe (elekktronaut)"), so appending it would double it.
    if handle.lower() in display.lower():
        return display
    return f"{display} ({handle})"


def build_chunks_from_video(meta: VideoMeta, channel_handle: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> list[Chunk]:
    """Read a cached transcript and produce vector-KB chunks."""
    vdir = _video_dir(channel_handle, meta.video_id, cache_dir)
    json_path = vdir / "transcript.json"
    if not json_path.exists():
        return []

    transcript = json.loads(json_path.read_text())
    segments = _segment_into_chunks(transcript)
    chunks: list[Chunk] = []
    for i, (start, end, text) in enumerate(segments):
        timestamp = f"[{int(start // 60):02d}:{int(start % 60):02d}-{int(end // 60):02d}:{int(end % 60):02d}]"
        chunks.append(
            Chunk(
                id=f"yt_{meta.video_id}_{i:02d}",
                source="tutorial",
                source_url=f"{meta.url}&t={int(start)}s",
                title=f"{meta.title} {timestamp}",
                text=f"{timestamp} (from {_attribution(meta, channel_handle)} — {meta.title})\n\n{text}",
            )
        )
    return chunks


def build_chunks_from_cache(cache_dir: Path = DEFAULT_CACHE_DIR) -> list[Chunk]:
    """Walk the cache dir, build chunks for every video with a transcript."""
    if not cache_dir.exists():
        return []
    chunks: list[Chunk] = []
    for channel_dir in cache_dir.iterdir():
        if not channel_dir.is_dir():
            continue
        for video_dir in channel_dir.iterdir():
            meta_path = video_dir / "meta.json"
            if not meta_path.exists():
                continue
            meta = VideoMeta.from_dict(json.loads(meta_path.read_text()))
            chunks.extend(build_chunks_from_video(meta, channel_dir.name, cache_dir))
    return chunks


def manifest(cache_dir: Path = DEFAULT_CACHE_DIR) -> dict:
    if not cache_dir.exists():
        return {"cache_dir": str(cache_dir), "exists": False, "channels": [], "total_videos": 0, "transcribed": 0}

    channels = []
    total = 0
    transcribed = 0
    for channel_dir in cache_dir.iterdir():
        if not channel_dir.is_dir():
            continue
        videos = list(channel_dir.iterdir())
        channel_transcribed = sum(1 for v in videos if (v / "transcript.txt").exists())
        channels.append({
            "handle": channel_dir.name,
            "videos": len(videos),
            "transcribed": channel_transcribed,
        })
        total += len(videos)
        transcribed += channel_transcribed

    return {
        "cache_dir": str(cache_dir),
        "exists": True,
        "channels": channels,
        "total_videos": total,
        "transcribed": transcribed,
    }
