"""Tutorial vision ingestion — Phase 5.

Goes beyond the Whisper transcript: watches the tutorial. Pipeline:

    yt-dlp video (≤1080p) → ffmpeg scene-detect keyframes
    → segments (keyframes + time-aligned Whisper transcript)
    → Sonnet 5 structured extraction (operators, parameters, wiring)
    → Chunk(source="tutorial") with operators/families actually filled.

Rationale: TD tutorials are visual — the narrator says "connect this
here and raise that" while the screen shows `noise1 → feedback1` and
`Roughness: 0.35`. Transcription captures the demonstratives, vision
captures the referents. Model default is claude-sonnet-5: it shares the
high-resolution vision tier (2576px long edge) with Opus 4.8 at 40% of
the price, which is what makes dense TD parameter panels legible.

Extends the youtube.py cache — same per-video folder, new artifacts:

    ~/.cache/td-mcp/youtube/<channel_handle>/<video_id>/
        video.mp4        — downloaded video (kept for re-extraction)
        keyframes/       — scene-detected PNGs, kf_<pts_ms>.png
        techniques.json  — per-segment VLM extractions (resumable)

Each step checks disk before working; re-running skips completed work.
Segments that fail extraction are stored with status="error" and retried
on the next run — never stored as plausible-looking defaults.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from td_mcp.ingest.youtube import DEFAULT_CACHE_DIR, VideoMeta, _video_dir
from td_mcp.kb.vector import Chunk
from td_mcp.util import write_json_atomic

logger = logging.getLogger(__name__)

DEFAULT_VISION_MODEL = os.environ.get("TD_MCP_VISION_MODEL", "claude-sonnet-5")
# YouTube increasingly bot-checks anonymous downloads; point this at a browser
# ("chrome", "safari", ...) to reuse its logged-in cookies via yt-dlp.
YTDLP_COOKIES_BROWSER = os.environ.get("TD_MCP_YTDLP_COOKIES_BROWSER", "")
SCENE_THRESHOLD = 0.08          # ffmpeg scene score — screencasts change slowly
MIN_KEYFRAME_GAP_SEC = 3.0      # drop near-duplicate scene cuts
MAX_KEYFRAMES_PER_VIDEO = 120   # cost ceiling; evenly subsampled beyond this
FALLBACK_INTERVAL_SEC = 30      # sampling interval when scene detect finds too little
SEGMENT_WINDOW_SEC = 60.0       # one VLM call covers ~this much video
MAX_FRAMES_PER_SEGMENT = 4

_FAMILY_SUFFIXES = ("COMP", "CHOP", "DAT", "MAT", "POP", "SOP", "TOP")


# ---------------------------------------------------------------------------
# VLM output schema — validated, never trusted raw
# ---------------------------------------------------------------------------

class ParameterSetting(BaseModel):
    operator: str
    parameter: str
    value: str


class Connection(BaseModel):
    source: str
    target: str


class TechniqueExtraction(BaseModel):
    technique: str
    summary: str
    operators: list[str] = []
    families: list[str] = []
    parameters: list[ParameterSetting] = []
    connections: list[Connection] = []
    uses_glsl: bool = False
    uses_python: bool = False
    confidence: Literal["high", "medium", "low"] = "medium"
    nothing_technical: bool = False


@dataclass
class Segment:
    index: int
    start: float
    end: float
    text: str
    frames: list[Path] = field(default_factory=list)


def family_from_op(op_name: str) -> str | None:
    """noiseTOP → TOP, audiodeviceinCHOP → CHOP. None if no known suffix."""
    for fam in _FAMILY_SUFFIXES:
        if op_name.endswith(fam):
            return fam
    return None


def normalize_operators(ops: list[str]) -> tuple[list[str], list[str]]:
    """Validate raw VLM operator names against the operators catalog.

    The VLM invents class-name variants (pointgenPOP / pointgeneratePOP /
    pointgeneratorPOP for the same op, camelCase like mathCombineCHOP).
    Case-insensitive match against the catalog canonicalizes them.
    Returns (canonical, unknown) — unknowns are NOT silently dropped, the
    caller keeps them visible in the chunk text but out of the filterable
    operators field.
    """
    deduped = list(dict.fromkeys(ops))
    try:
        from td_mcp.kb.operators import get_catalog

        catalog = {e.python_class.lower(): e.python_class for e in get_catalog().list()}
    except Exception:
        catalog = {}
    if not catalog:
        return deduped, []
    valid: list[str] = []
    unknown: list[str] = []
    for op in deduped:
        canon = catalog.get(op.lower())
        if canon:
            if canon not in valid:
                valid.append(canon)
        else:
            unknown.append(op)
    return valid, unknown


# ---------------------------------------------------------------------------
# Step 1 — video download (cached)
# ---------------------------------------------------------------------------

def download_video(
    meta: VideoMeta,
    channel_handle: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    max_height: int = 1080,
) -> Path | None:
    """Download video-only stream (audio is already cached separately).
    ≤1080p keeps UI text legible without blowing up disk. Skip if cached."""
    vdir = _video_dir(channel_handle, meta.video_id, cache_dir)
    vdir.mkdir(parents=True, exist_ok=True)
    video_path = vdir / "video.mp4"
    if video_path.exists():
        return video_path

    # web_safari client + HLS preference: YouTube 403s DASH data for
    # anonymous/web clients (SABR gating, observed 2026-07), but the HLS
    # manifests still stream fine with browser cookies.
    cmd = ["yt-dlp", "--quiet"]
    if YTDLP_COOKIES_BROWSER:
        cmd += ["--cookies-from-browser", YTDLP_COOKIES_BROWSER]
    cmd += [
        "--extractor-args", "youtube:player_client=web_safari",
        "-f",
        f"bestvideo[height<={max_height}]/best[height<={max_height}]/best",
        "-S", "proto:m3u8",
        "-o",
        str(video_path),
        meta.url,
    ]
    try:
        subprocess.check_call(cmd, stderr=subprocess.DEVNULL, timeout=1800)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("video download failed for %s: %s", meta.url, e)
        return None
    return video_path if video_path.exists() else None


# ---------------------------------------------------------------------------
# Step 2 — keyframe extraction (scene detection, cached)
# ---------------------------------------------------------------------------

_PTS_RE = re.compile(r"pts_time:\s*([0-9]+\.?[0-9]*)")


def parse_showinfo_times(stderr: str) -> list[float]:
    """Pull pts_time values out of ffmpeg showinfo stderr, in order."""
    return [float(m) for m in _PTS_RE.findall(stderr)]


def filter_keyframes(
    times: list[float],
    min_gap: float = MIN_KEYFRAME_GAP_SEC,
    max_frames: int = MAX_KEYFRAMES_PER_VIDEO,
) -> list[int]:
    """Indices of frames to keep: enforce a minimum time gap, then evenly
    subsample down to max_frames. Returns indices into `times`."""
    kept: list[int] = []
    last = -math.inf
    for i, t in enumerate(times):
        if t - last >= min_gap:
            kept.append(i)
            last = t
    if len(kept) > max_frames:
        step = len(kept) / max_frames
        kept = [kept[int(i * step)] for i in range(max_frames)]
    return kept


def extract_keyframes(
    video: Path,
    out_dir: Path,
    scene_threshold: float = SCENE_THRESHOLD,
    duration_sec: float = 0.0,
) -> list[tuple[float, Path]]:
    """Scene-detected keyframes as [(pts_sec, png_path)], cached on disk.

    Single ffmpeg pass: select scene changes, showinfo logs each selected
    frame's pts_time to stderr, frames land numbered in out_dir. If scene
    detection yields too few frames (static screencast), falls back to
    fixed-interval sampling.
    """
    manifest_path = out_dir / "keyframes.json"
    if manifest_path.exists():
        entries = json.loads(manifest_path.read_text())
        return [(e["t"], Path(e["path"])) for e in entries]

    out_dir.mkdir(parents=True, exist_ok=True)
    raw_pattern = out_dir / "raw_%05d.png"
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(video),
                "-vf", f"select='gt(scene,{scene_threshold})',showinfo",
                "-vsync", "vfr", str(raw_pattern),
            ],
            capture_output=True, text=True, timeout=1800,
        )
        times = parse_showinfo_times(proc.stderr)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("scene detection failed for %s: %s", video, e)
        return []

    raw_frames = sorted(out_dir.glob("raw_*.png"))
    # showinfo lines and written frames correspond 1:1 in order
    pairs = list(zip(times, raw_frames))

    min_expected = max(4, int(duration_sec // 120)) if duration_sec else 4
    if len(pairs) < min_expected:
        logger.info("scene detect found %d frames — falling back to 1/%ds sampling",
                    len(pairs), FALLBACK_INTERVAL_SEC)
        for f in raw_frames:
            f.unlink()
        try:
            proc = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(video),
                    "-vf", f"fps=1/{FALLBACK_INTERVAL_SEC},showinfo",
                    "-vsync", "vfr", str(raw_pattern),
                ],
                capture_output=True, text=True, timeout=1800,
            )
            times = parse_showinfo_times(proc.stderr)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning("interval sampling failed for %s: %s", video, e)
            return []
        raw_frames = sorted(out_dir.glob("raw_*.png"))
        pairs = list(zip(times, raw_frames))

    keep = filter_keyframes([t for t, _ in pairs])
    result: list[tuple[float, Path]] = []
    keep_set = set(keep)
    for i, (t, raw) in enumerate(pairs):
        if i in keep_set:
            final = out_dir / f"kf_{int(t * 1000):09d}.png"
            raw.rename(final)
            result.append((t, final))
        else:
            raw.unlink()

    write_json_atomic(manifest_path, [{"t": t, "path": str(p)} for t, p in result], indent=2)
    return result


# ---------------------------------------------------------------------------
# Step 3 — segment building (pure logic)
# ---------------------------------------------------------------------------

def build_segments(
    transcript: dict,
    keyframes: list[tuple[float, Path]],
    window_sec: float = SEGMENT_WINDOW_SEC,
    max_frames: int = MAX_FRAMES_PER_SEGMENT,
) -> list[Segment]:
    """Group Whisper segments into ~window_sec windows and attach the
    keyframes that fall inside each window (evenly subsampled to
    max_frames). Windows with zero keyframes are still produced — the
    caller decides whether to skip them (no visuals = nothing to see)."""
    segments: list[Segment] = []
    current: list[dict] = []

    def flush() -> None:
        if not current:
            return
        start = current[0]["start"]
        end = current[-1]["end"]
        text = " ".join(s["text"].strip() for s in current)
        segments.append(Segment(index=len(segments), start=start, end=end, text=text))

    for seg in transcript.get("segments", []):
        if current and seg["end"] - current[0]["start"] > window_sec:
            flush()
            current = []
        current.append(seg)
    flush()

    for s in segments:
        inside = [(t, p) for t, p in keyframes if s.start <= t < s.end]
        if len(inside) > max_frames:
            step = len(inside) / max_frames
            inside = [inside[int(i * step)] for i in range(max_frames)]
        s.frames = [p for _, p in inside]

    return segments


# ---------------------------------------------------------------------------
# Step 4 — VLM extraction (Sonnet 5, structured, explicit errors)
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT = """\
You are analyzing a TouchDesigner tutorial video segment. The images are \
keyframes from the screen recording (in chronological order); the transcript \
of what the narrator says during this segment follows.

Video: {title}
Segment: {t_start} - {t_end}
Transcript:
{transcript}

Extract what is actually being DONE in TouchDesigner in this segment by \
reading the network editor and parameter panels in the frames. Reply with \
strict JSON only, no markdown fences:

{{"technique": "short name of the technique being demonstrated",
 "summary": "2-4 sentences: what is built on screen and why",
 "operators": ["exact TD python class names visible/created, e.g. noiseTOP, feedbackTOP"],
 "families": ["TOP","CHOP",...],
 "parameters": [{{"operator": "noise1", "parameter": "Roughness", "value": "0.35"}}],
 "connections": [{{"source": "noise1", "target": "feedback1"}}],
 "uses_glsl": false,
 "uses_python": false,
 "confidence": "high"|"medium"|"low",
 "nothing_technical": false}}

Rules:
- Only report operators/parameters/wiring you can actually see in the frames \
or that the transcript names explicitly. Do not guess plausible values.
- "operators" MUST contain python CLASS names (moviefileinTOP, displaceTOP), \
never node instance names (moviefilein1, displace1). Instance names belong \
only in "parameters" and "connections".
- If the segment is intro/outro/talking-head with no TD work on screen, set \
"nothing_technical": true and leave the lists empty.
- confidence reflects how legible the UI was in the frames."""


def extract_segment(
    segment: Segment,
    video_title: str,
    model: str = DEFAULT_VISION_MODEL,
) -> dict:
    """One VLM call for one segment. Returns
    {"status": "ok", "extraction": {...}} or {"status": "error", "error": str}.
    Never fabricates a default extraction."""
    import base64

    def _mmss(t: float) -> str:
        return f"{int(t // 60):02d}:{int(t % 60):02d}"

    content: list[dict] = []
    for i, frame in enumerate(segment.frames, 1):
        content.append({"type": "text", "text": f"Image {i}:"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(frame.read_bytes()).decode(),
            },
        })
    content.append({
        "type": "text",
        "text": _EXTRACTION_PROMPT.format(
            title=video_title,
            t_start=_mmss(segment.start),
            t_end=_mmss(segment.end),
            transcript=segment.text,
        ),
    })

    try:
        from anthropic import Anthropic

        client = Anthropic()
        msg = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": content}],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
        extraction = TechniqueExtraction.model_validate_json(text)
        return {"status": "ok", "extraction": extraction.model_dump()}
    except (ValidationError, json.JSONDecodeError) as e:
        logger.warning("extraction JSON invalid for segment %d: %s", segment.index, e)
        return {"status": "error", "error": f"invalid JSON: {e}"}
    except Exception as e:
        logger.warning("extraction failed for segment %d: %s", segment.index, e)
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Step 5 — per-video orchestration (resumable via techniques.json)
# ---------------------------------------------------------------------------

def process_video(
    meta: VideoMeta,
    channel_handle: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    model: str = DEFAULT_VISION_MODEL,
    max_workers: int | None = None,
) -> dict:
    """Full vision pass on one cached video. Requires transcript.json
    (run the audio pipeline first). Resumes: segments already extracted
    ok are skipped, errored ones retried.

    Segment extractions run concurrently (the wall clock is API round
    trips, not local compute — local ffmpeg is minutes for a whole batch).
    max_workers defaults to TD_MCP_VISION_CONCURRENCY or 6; results are
    written from the main thread only, so techniques.json stays consistent."""
    vdir = _video_dir(channel_handle, meta.video_id, cache_dir)
    transcript_path = vdir / "transcript.json"
    if not transcript_path.exists():
        return {"ok": False, "error": "no transcript.json — run kb_ingest_youtube_channel first"}

    video = download_video(meta, channel_handle, cache_dir)
    if video is None:
        return {"ok": False, "error": "video download failed"}

    keyframes = extract_keyframes(video, vdir / "keyframes", duration_sec=meta.duration_sec)
    if not keyframes:
        return {"ok": False, "error": "keyframe extraction produced no frames"}

    transcript = json.loads(transcript_path.read_text())
    segments = build_segments(transcript, keyframes)

    tech_path = vdir / "techniques.json"
    existing: dict = {"video_id": meta.video_id, "model": model, "segments": {}}
    if tech_path.exists():
        existing = json.loads(tech_path.read_text())

    report = {"ok": True, "video_id": meta.video_id, "segments_total": len(segments),
              "extracted": 0, "skipped_cached": 0, "skipped_no_frames": 0, "errors": 0}

    pending: list[Segment] = []
    for seg in segments:
        key = str(seg.index)
        prior = existing["segments"].get(key)
        if prior and prior.get("status") == "ok":
            report["skipped_cached"] += 1
            continue
        if not seg.frames:
            existing["segments"][key] = {
                "status": "skipped_no_frames",
                "start": seg.start, "end": seg.end,
            }
            report["skipped_no_frames"] += 1
            continue
        pending.append(seg)

    if pending:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        workers = max_workers or int(os.environ.get("TD_MCP_VISION_CONCURRENCY", "6"))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {
                pool.submit(extract_segment, seg, meta.title, model): seg
                for seg in pending
            }
            for fut in as_completed(futures):
                seg = futures[fut]
                result = fut.result()
                existing["segments"][str(seg.index)] = {
                    **result,
                    "start": seg.start,
                    "end": seg.end,
                    "transcript": seg.text,
                    "frame_count": len(seg.frames),
                }
                if result["status"] == "ok":
                    report["extracted"] += 1
                else:
                    report["errors"] += 1
                # write after every completion — a crash loses at most
                # the in-flight calls, never completed ones
                write_json_atomic(tech_path, existing, indent=2)

    write_json_atomic(tech_path, existing, indent=2)
    return report


# ---------------------------------------------------------------------------
# Step 6 — techniques.json → Chunks
# ---------------------------------------------------------------------------

def build_chunks_from_techniques(
    meta: VideoMeta,
    channel_handle: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> list[Chunk]:
    """Vision chunks for one video. Complements (does not replace) the
    transcript chunks: ids are ytv_* vs the audio pipeline's yt_*."""
    vdir = _video_dir(channel_handle, meta.video_id, cache_dir)
    tech_path = vdir / "techniques.json"
    if not tech_path.exists():
        return []

    data = json.loads(tech_path.read_text())
    chunks: list[Chunk] = []
    for key in sorted(data.get("segments", {}), key=int):
        seg = data["segments"][key]
        if seg.get("status") != "ok":
            continue
        ext = seg.get("extraction", {})
        if ext.get("nothing_technical"):
            continue

        operators, unknown_ops = normalize_operators(ext.get("operators", []))
        families = sorted(
            set(ext.get("families", []))
            | {f for f in (family_from_op(o) for o in operators + unknown_ops) if f}
        )
        start, end = seg["start"], seg["end"]
        timestamp = f"[{int(start // 60):02d}:{int(start % 60):02d}-{int(end // 60):02d}:{int(end % 60):02d}]"

        params_txt = "; ".join(
            f"{p['operator']}.{p['parameter']} = {p['value']}" for p in ext.get("parameters", [])
        )
        conn_txt = ", ".join(
            f"{c['source']} → {c['target']}" for c in ext.get("connections", [])
        )
        body = [
            f"{timestamp} (from {meta.channel} — {meta.title})",
            f"Technique: {ext.get('technique', '')}",
            ext.get("summary", ""),
        ]
        if operators:
            body.append(f"Operators: {', '.join(operators)}")
        if unknown_ops:
            body.append(f"Unverified operators (not in catalog): {', '.join(unknown_ops)}")
        if params_txt:
            body.append(f"Parameters: {params_txt}")
        if conn_txt:
            body.append(f"Connections: {conn_txt}")
        if seg.get("transcript"):
            body.append(f"\nTranscript: {seg['transcript']}")

        chunks.append(
            Chunk(
                id=f"ytv_{meta.video_id}_{int(key):02d}",
                source="tutorial",
                source_url=f"{meta.url}&t={int(start)}s",
                title=f"{ext.get('technique', meta.title)} {timestamp}",
                text="\n".join(body),
                operators=operators,
                families=families,
                is_glsl=bool(ext.get("uses_glsl")),
                is_python=bool(ext.get("uses_python")),
            )
        )
    return chunks


def build_chunks_from_cache(cache_dir: Path = DEFAULT_CACHE_DIR) -> list[Chunk]:
    """Walk the cache, build vision chunks for every video with techniques."""
    if not cache_dir.exists():
        return []
    chunks: list[Chunk] = []
    for channel_dir in cache_dir.iterdir():
        if not channel_dir.is_dir():
            continue
        for video_dir in channel_dir.iterdir():
            meta_path = video_dir / "meta.json"
            if not meta_path.exists() or not (video_dir / "techniques.json").exists():
                continue
            meta = VideoMeta.from_dict(json.loads(meta_path.read_text()))
            chunks.extend(build_chunks_from_techniques(meta, channel_dir.name, cache_dir))
    return chunks


def manifest(cache_dir: Path = DEFAULT_CACHE_DIR) -> dict:
    """Vision-pass coverage: which cached videos have techniques.json."""
    if not cache_dir.exists():
        return {"cache_dir": str(cache_dir), "exists": False, "videos_with_vision": 0}
    with_vision = 0
    total = 0
    for channel_dir in cache_dir.iterdir():
        if not channel_dir.is_dir():
            continue
        for video_dir in channel_dir.iterdir():
            if not (video_dir / "meta.json").exists():
                continue
            total += 1
            if (video_dir / "techniques.json").exists():
                with_vision += 1
    return {
        "cache_dir": str(cache_dir),
        "exists": True,
        "total_videos": total,
        "videos_with_vision": with_vision,
    }
