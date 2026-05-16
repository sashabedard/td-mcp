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

DEFAULT_CACHE_DIR = Path(
    os.environ.get(
        "TD_MCP_YOUTUBE_CACHE",
        str(Path.home() / ".cache" / "td-mcp" / "youtube"),
    )
)
DEFAULT_WHISPER_MODEL = os.environ.get("TD_MCP_WHISPER_MODEL", "base")
SOURCES_CONFIG = Path(__file__).parent.parent / "kb" / "data" / "youtube_sources.json"


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
    cmd = [
        "yt-dlp",
        "--quiet",
        "--flat-playlist",
        "--print",
        "%(id)s|%(title)s|%(duration)s|%(channel)s|%(webpage_url)s",
    ]
    if limit:
        cmd.extend(["--playlist-end", str(limit)])
    cmd.append(channel_url)

    out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    videos = []
    for line in out.strip().splitlines():
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        vid, title, duration, channel, url = parts
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


def download_audio(meta: VideoMeta, channel_handle: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    """Download bestaudio as m4a. Skip if already cached."""
    vdir = _video_dir(channel_handle, meta.video_id, cache_dir)
    vdir.mkdir(parents=True, exist_ok=True)
    audio_path = vdir / "audio.m4a"
    meta_path = vdir / "meta.json"

    if not meta_path.exists():
        meta_path.write_text(json.dumps(meta.to_dict(), indent=2))

    if audio_path.exists():
        return audio_path

    cmd = [
        "yt-dlp",
        "--quiet",
        "-f",
        "bestaudio[ext=m4a]/bestaudio",
        "-o",
        str(audio_path).replace(".m4a", ".%(ext)s"),
        meta.url,
    ]
    subprocess.check_call(cmd, stderr=subprocess.DEVNULL)

    # yt-dlp may write a different extension if m4a isn't available
    if not audio_path.exists():
        candidates = list(vdir.glob("audio.*"))
        if candidates:
            return candidates[0]
    return audio_path


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
    json_path.write_text(json.dumps(serializable, indent=2))
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
                text=f"{timestamp} (from {meta.channel} — {meta.title})\n\n{text}",
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
