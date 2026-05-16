import json
from pathlib import Path

from td_mcp.ingest.youtube import (
    VideoMeta,
    _safe_handle,
    _segment_into_chunks,
    build_chunks_from_cache,
    load_sources,
    manifest,
)


def test_safe_handle_strips_at_and_slashes():
    assert _safe_handle("@OkamirufuV") == "OkamirufuV"
    assert _safe_handle("foo/bar") == "foo_bar"


def test_load_sources_has_okamirufu():
    s = load_sources()
    handles = {c["handle"] for c in s["channels"]}
    assert "OkamirufuV" in handles


def test_video_meta_roundtrip():
    m = VideoMeta(video_id="abc", title="T", duration_sec=120.0, channel="ch", url="u")
    d = m.to_dict()
    assert VideoMeta.from_dict(d) == m


def test_segment_into_chunks_respects_word_budget():
    transcript = {
        "segments": [
            {"id": 0, "start": 0.0, "end": 10.0, "text": "hello " * 150},
            {"id": 1, "start": 10.0, "end": 20.0, "text": "world " * 150},
            {"id": 2, "start": 20.0, "end": 30.0, "text": "again " * 50},
        ]
    }
    chunks = _segment_into_chunks(transcript, max_words=200)
    # First chunk holds seg 0 (150 words). Adding seg 1 (150) exceeds 200, so seg 1 starts new chunk.
    assert len(chunks) >= 2
    # First chunk should start at 0.0
    assert chunks[0][0] == 0.0


def test_segment_into_chunks_empty():
    assert _segment_into_chunks({"segments": []}) == []


def test_manifest_on_missing_cache(tmp_path: Path):
    m = manifest(cache_dir=tmp_path / "nope")
    assert m["exists"] is False
    assert m["total_videos"] == 0


def test_manifest_counts_videos_and_transcripts(tmp_path: Path):
    # Simulate cache: 1 channel, 2 videos, 1 transcribed
    cache = tmp_path / "yt"
    ch = cache / "TestChannel"
    v1 = ch / "vid1"
    v2 = ch / "vid2"
    v1.mkdir(parents=True)
    v2.mkdir(parents=True)
    (v1 / "meta.json").write_text("{}")
    (v1 / "transcript.txt").write_text("hi")
    (v2 / "meta.json").write_text("{}")
    # v2 has no transcript
    m = manifest(cache_dir=cache)
    assert m["total_videos"] == 2
    assert m["transcribed"] == 1
    assert m["channels"][0]["transcribed"] == 1


def test_build_chunks_from_cache_with_fake_transcript(tmp_path: Path):
    cache = tmp_path / "yt"
    vdir = cache / "TestChannel" / "abc123"
    vdir.mkdir(parents=True)
    meta = {
        "video_id": "abc123",
        "title": "Test POP Tutorial",
        "duration_sec": 60.0,
        "channel": "TestChannel",
        "url": "https://youtube.com/watch?v=abc123",
    }
    (vdir / "meta.json").write_text(json.dumps(meta))
    transcript = {
        "segments": [
            {"id": 0, "start": 0.0, "end": 30.0, "text": "first half about gridPOP"},
            {"id": 1, "start": 30.0, "end": 60.0, "text": "second half about noisePOP"},
        ]
    }
    (vdir / "transcript.json").write_text(json.dumps(transcript))
    (vdir / "transcript.txt").write_text("ignored")

    chunks = build_chunks_from_cache(cache_dir=cache)
    assert len(chunks) >= 1
    assert chunks[0].source == "tutorial"
    assert "abc123" in chunks[0].source_url
    assert "00:00" in chunks[0].title
    assert "gridPOP" in chunks[0].text
