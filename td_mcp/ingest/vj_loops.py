"""VJ loops ingestion pipeline.

yt-dlp → ffmpeg frame extract → CLIP embed → Haiku classify → LanceDB.

Designed to be resumable and tolerant: individual video failures are
logged and skipped, the batch continues.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from td_mcp.util import write_json_atomic

logger = logging.getLogger(__name__)

FRAME_INTERVAL_SEC = 2

# Frames referenced by the vj_references table must outlive ingestion —
# extracting into the run's TemporaryDirectory leaves every persisted
# frame_path dangling the moment ingest_corpus returns.
DEFAULT_FRAMES_DIR = Path(
    os.environ.get(
        "TD_MCP_VJ_FRAMES",
        str(Path.home() / ".cache" / "td-mcp" / "vj_frames"),
    )
)


def download_video(url: str, out_dir: Path) -> Path | None:
    """yt-dlp to out_dir. Returns mp4 path or None on failure."""
    try:
        result = subprocess.run(
            ["yt-dlp", "-f", "mp4", "-o", str(out_dir / "%(id)s.%(ext)s"), url],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            logger.warning("yt-dlp failed for %s: %s", url, result.stderr[:200])
            return None
        mp4s = list(out_dir.glob("*.mp4"))
        return mp4s[0] if mp4s else None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("download_video error for %s: %s", url, e)
        return None


def extract_frames(video: Path, out_dir: Path, interval: int = FRAME_INTERVAL_SEC) -> list[Path]:
    """ffmpeg -vf fps=1/interval. Returns list of frame PNG paths."""
    out_pattern = out_dir / f"{video.stem}_%05d.png"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video), "-vf", f"fps=1/{interval}", str(out_pattern)],
            capture_output=True, timeout=600, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("extract_frames error for %s: %s", video, e)
        return []
    return sorted(out_dir.glob(f"{video.stem}_*.png"))


def clip_embed_frames(frames: list[Path]) -> list[list[float]]:
    """Load open_clip ViT-B-32 once and embed all frames. Returns 512-d vectors."""
    import open_clip
    import torch
    from PIL import Image

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-B-32", pretrained="openai", device=device
    )
    # Switch to inference mode (PyTorch convention — disables dropout/batchnorm-train)
    model = model.requires_grad_(False)
    model.train(False)

    embeddings: list[list[float]] = []
    batch_size = 16
    i = 0
    while i < len(frames):
        batch_paths = frames[i:i + batch_size]
        try:
            tensors = torch.stack([
                preprocess(Image.open(p).convert("RGB")) for p in batch_paths
            ]).to(device)
            with torch.no_grad():
                feats = model.encode_image(tensors)
                feats = feats / feats.norm(dim=-1, keepdim=True)
            embeddings.extend(feats.cpu().tolist())
            i += batch_size
        except torch.cuda.OutOfMemoryError:
            logger.warning("CLIP OOM at index %d, halving batch_size from %d", i, batch_size)
            torch.cuda.empty_cache()
            batch_size = max(1, batch_size // 2)
            if batch_size == 1 and len(batch_paths) == 1:
                raise
    return embeddings


def classify_frame_haiku(frame_path: Path, cache: dict) -> dict:
    """One Haiku call per frame. Cached by SHA256(frame bytes).

    Returns {"energy": "calm|medium|high|frantic", "palette_hex": [...]}.
    On API error returns {"energy": "unknown", "palette_hex": [], "error": str}
    — never a plausible default, so failed classifications stay
    distinguishable from real ones and are NOT written to the cache.
    """
    data = frame_path.read_bytes()
    key = hashlib.sha256(data).hexdigest()
    if key in cache:
        return cache[key]

    import base64

    try:
        from anthropic import Anthropic

        client = Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png",
                        "data": base64.b64encode(data).decode(),
                    }},
                    {"type": "text", "text": (
                        "Classify this VJ loop frame. Reply with strict JSON only: "
                        '{"energy": "calm"|"medium"|"high"|"frantic", '
                        '"palette_hex": ["#rrggbb", ... up to 5 dominants]}'
                    )},
                ],
            }],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
        out = json.loads(text)
        cache[key] = out
        return out
    except Exception as e:
        logger.warning("Haiku classify failed for %s: %s", frame_path, e)
        return {"energy": "unknown", "palette_hex": [], "error": str(e)}


def _existing_frame_ids(table) -> set[str]:
    """Ids already in the vj_references table (empty table → empty set)."""
    try:
        rows = table.search().select(["id"]).limit(1_000_000).to_list()
        return {r["id"] for r in rows}
    except Exception:
        return set()


def ingest_corpus(
    url_list_path: Path,
    cache_path: Path | None = None,
    frames_dir: Path | None = None,
) -> dict:
    """End-to-end: download → frames → embed → classify → write LanceDB.

    Idempotent: rows are keyed by SHA256 of the frame bytes, so re-running
    the same URL list skips frames already in the table instead of
    duplicating them. Frames are copied to `frames_dir` (default
    ~/.cache/td-mcp/vj_frames) so persisted frame_path values survive the
    run. The Haiku cache is flushed after every video — a crash keeps the
    classifications already paid for.

    Returns a report dict with counts.
    """
    from td_mcp.kb.vj_corpus import open_table

    entries = json.loads(Path(url_list_path).read_text())
    cache = json.loads(cache_path.read_text()) if cache_path and cache_path.exists() else {}

    table = open_table()
    existing = _existing_frame_ids(table)
    frames_root = Path(frames_dir) if frames_dir else DEFAULT_FRAMES_DIR
    frames_root.mkdir(parents=True, exist_ok=True)

    report = {
        "videos_processed": 0,
        "frames_added": 0,
        "frames_skipped": 0,
        "frames_failed": 0,
        "videos_failed": 0,
    }

    def _flush_cache() -> None:
        if cache_path:
            write_json_atomic(cache_path, cache)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for i, entry in enumerate(entries):
                # One directory per entry: a shared dir accumulates every
                # previous entry's mp4 and download_video's glob would
                # return an arbitrary one.
                entry_dir = tmp_path / f"entry_{i:03d}"
                entry_dir.mkdir()
                video = download_video(entry["url"], entry_dir)
                if video is None:
                    report["videos_failed"] += 1
                    continue
                frames = extract_frames(video, entry_dir)
                if not frames:
                    report["videos_failed"] += 1
                    continue
                embeddings = clip_embed_frames(frames)

                records = []
                for frame, emb in zip(frames, embeddings):
                    data = frame.read_bytes()
                    frame_id = hashlib.sha256(data).hexdigest()
                    if frame_id in existing:
                        report["frames_skipped"] += 1
                        continue
                    cls = classify_frame_haiku(frame, cache)
                    if cls.get("error"):
                        report["frames_failed"] += 1
                        continue
                    persisted = frames_root / f"{frame_id}.png"
                    if not persisted.exists():
                        persisted.write_bytes(data)
                    records.append({
                        "id": frame_id,
                        "artist": entry["artist"],
                        "frame_path": str(persisted),
                        "energy": cls["energy"],
                        "palette_hex": ",".join(cls["palette_hex"]),
                        "tempo_estimate": 0.0,
                        "embedding": emb,
                    })
                    existing.add(frame_id)
                if records:
                    table.add(records)
                    report["frames_added"] += len(records)
                report["videos_processed"] += 1
                _flush_cache()
    finally:
        _flush_cache()
    return report
