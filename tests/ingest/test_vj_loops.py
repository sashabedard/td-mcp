import hashlib
import json
from unittest.mock import patch

import pytest


def _wire_fake_pipeline(monkeypatch, tmp_path):
    """Mock the network/GPU stages of ingest_corpus; keep LanceDB real.

    Each fake video's bytes derive from its URL so frame hashes are
    distinct per video and stable across runs.
    """
    from td_mcp.ingest import vj_loops
    from td_mcp.kb import vj_corpus

    db_dir = tmp_path / "db"
    real_open = vj_corpus.open_table
    monkeypatch.setattr(vj_corpus, "open_table", lambda: real_open(db_path=db_dir))

    download_dirs: list = []

    def fake_download(url, out_dir):
        download_dirs.append(out_dir)
        video = out_dir / "vid.mp4"
        video.write_bytes(url.encode())
        return video

    def fake_extract(video, out_dir, interval=2):
        frames = []
        for i in range(2):
            f = out_dir / f"{video.stem}_{i:05d}.png"
            f.write_bytes(video.read_bytes() + bytes([i]))
            frames.append(f)
        return frames

    def fake_classify(frame, cache):
        out = {"energy": "calm", "palette_hex": ["#000000"]}
        cache[hashlib.sha256(frame.read_bytes()).hexdigest()] = out
        return out

    monkeypatch.setattr(vj_loops, "download_video", fake_download)
    monkeypatch.setattr(vj_loops, "extract_frames", fake_extract)
    monkeypatch.setattr(vj_loops, "clip_embed_frames", lambda frames: [[0.0] * 512 for _ in frames])
    monkeypatch.setattr(vj_loops, "classify_frame_haiku", fake_classify)
    return download_dirs


def test_ingest_corpus_isolates_downloads_per_entry(monkeypatch, tmp_path):
    """Sharing one download dir means glob('*.mp4') can return a previous
    entry's video — each entry must get its own directory."""
    from td_mcp.ingest.vj_loops import ingest_corpus

    download_dirs = _wire_fake_pipeline(monkeypatch, tmp_path)
    urls = tmp_path / "urls.json"
    urls.write_text(json.dumps([
        {"url": "http://x/video-a", "artist": "a"},
        {"url": "http://x/video-b", "artist": "b"},
    ]))

    ingest_corpus(urls, frames_dir=tmp_path / "frames")
    assert len(download_dirs) == 2
    assert download_dirs[0] != download_dirs[1], "entries share a download dir"


def test_ingest_corpus_persists_frames_and_dedupes_on_rerun(monkeypatch, tmp_path):
    """frame_path must survive the temp dir, and re-running the same URL
    list must not duplicate rows."""
    from td_mcp.ingest.vj_loops import ingest_corpus
    from td_mcp.kb import vj_corpus

    _wire_fake_pipeline(monkeypatch, tmp_path)
    urls = tmp_path / "urls.json"
    urls.write_text(json.dumps([{"url": "http://x/video-a", "artist": "a"}]))

    report1 = ingest_corpus(urls, frames_dir=tmp_path / "frames")
    assert report1["frames_added"] == 2

    table = vj_corpus.open_table()
    rows = table.search().select(["id", "frame_path"]).limit(100).to_list()
    for row in rows:
        from pathlib import Path
        assert Path(row["frame_path"]).exists(), f"dangling frame_path {row['frame_path']}"

    report2 = ingest_corpus(urls, frames_dir=tmp_path / "frames")
    assert report2["frames_added"] == 0, "re-run duplicated rows"
    assert table.count_rows() == 2


def test_ingest_corpus_writes_cache_incrementally(monkeypatch, tmp_path):
    """A crash on video N must not discard the paid classifications of
    videos 1..N-1 — the cache is written per video, not once at the end."""
    from td_mcp.ingest import vj_loops
    from td_mcp.ingest.vj_loops import ingest_corpus

    _wire_fake_pipeline(monkeypatch, tmp_path)

    calls = {"n": 0}

    def exploding_embed(frames):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("CUDA OOM on video 2")
        return [[0.0] * 512 for _ in frames]

    monkeypatch.setattr(vj_loops, "clip_embed_frames", exploding_embed)

    urls = tmp_path / "urls.json"
    urls.write_text(json.dumps([
        {"url": "http://x/video-a", "artist": "a"},
        {"url": "http://x/video-b", "artist": "b"},
    ]))
    cache_path = tmp_path / "urls.cache.json"

    with pytest.raises(RuntimeError):
        ingest_corpus(urls, cache_path=cache_path, frames_dir=tmp_path / "frames")

    assert cache_path.exists(), "cache lost on crash"
    assert len(json.loads(cache_path.read_text())) == 2, "video 1 classifications lost"


def test_download_video_returns_none_on_failure(tmp_path):
    from td_mcp.ingest.vj_loops import download_video
    with patch("subprocess.run") as run:
        run.return_value.returncode = 1
        run.return_value.stderr = "video unavailable"
        assert download_video("http://bad", tmp_path) is None


def test_extract_frames_returns_empty_on_ffmpeg_fail(tmp_path):
    import subprocess
    from td_mcp.ingest.vj_loops import extract_frames
    fake_video = tmp_path / "x.mp4"
    fake_video.write_bytes(b"not real")
    with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
        assert extract_frames(fake_video, tmp_path) == []


def test_classify_frame_haiku_uses_cache(tmp_path):
    from td_mcp.ingest.vj_loops import classify_frame_haiku
    frame = tmp_path / "f.png"
    frame.write_bytes(b"fake png bytes")
    import hashlib
    cache = {hashlib.sha256(b"fake png bytes").hexdigest(): {"energy": "calm", "palette_hex": ["#000000"]}}
    result = classify_frame_haiku(frame, cache)
    assert result["energy"] == "calm"


def test_classify_frame_haiku_api_error_is_explicit_and_uncached(tmp_path):
    """A failed API call must not masquerade as a real classification
    and must not poison the cache."""
    import sys
    import types
    from td_mcp.ingest.vj_loops import classify_frame_haiku

    frame = tmp_path / "f.png"
    frame.write_bytes(b"other png bytes")
    cache = {}

    fake_anthropic = types.ModuleType("anthropic")

    class _Boom:
        def __init__(self):
            raise RuntimeError("no api key")

    fake_anthropic.Anthropic = _Boom
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        result = classify_frame_haiku(frame, cache)

    assert result["energy"] == "unknown"
    assert "error" in result
    assert cache == {}
