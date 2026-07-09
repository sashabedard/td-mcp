import json
from pathlib import Path
from unittest.mock import patch


def _transcript(n_segs: int = 6, seg_len: float = 20.0) -> dict:
    return {
        "segments": [
            {"id": i, "start": i * seg_len, "end": (i + 1) * seg_len, "text": f"segment {i}"}
            for i in range(n_segs)
        ]
    }


def test_family_from_op():
    from td_mcp.ingest.tutorial_vision import family_from_op
    assert family_from_op("noiseTOP") == "TOP"
    assert family_from_op("audiodeviceinCHOP") == "CHOP"
    assert family_from_op("spherePOP") == "POP"
    assert family_from_op("banana") is None


def test_normalize_operators_canonicalizes_against_catalog():
    from td_mcp.ingest.tutorial_vision import normalize_operators
    # camelCase and case variants of real classes → canonical; inventions → unknown
    valid, unknown = normalize_operators(
        ["noiseTOP", "NoiseTOP", "mathCombineCHOP", "pointgeneratePOP_fake"]
    )
    assert "noiseTOP" in valid
    assert valid.count("noiseTOP") == 1  # deduped across case variants
    assert "pointgeneratePOP_fake" in unknown


def test_parse_showinfo_times():
    from td_mcp.ingest.tutorial_vision import parse_showinfo_times
    stderr = (
        "[Parsed_showinfo_1] n:0 pts:123 pts_time:1.5 duration:0\n"
        "noise line\n"
        "[Parsed_showinfo_1] n:1 pts:456 pts_time:42.08 duration:0\n"
    )
    assert parse_showinfo_times(stderr) == [1.5, 42.08]


def test_filter_keyframes_enforces_gap_and_cap():
    from td_mcp.ingest.tutorial_vision import filter_keyframes
    # 0, 1, 2 sec apart — gap 3s keeps only every 3rd
    times = [float(t) for t in range(0, 30)]
    kept = filter_keyframes(times, min_gap=3.0, max_frames=100)
    assert kept == [0, 3, 6, 9, 12, 15, 18, 21, 24, 27]
    # cap subsamples evenly
    capped = filter_keyframes(times, min_gap=1.0, max_frames=5)
    assert len(capped) == 5
    assert capped[0] == 0


def test_build_segments_windows_and_frame_assignment(tmp_path):
    from td_mcp.ingest.tutorial_vision import build_segments
    frames = [(t, tmp_path / f"kf_{t}.png") for t in (5.0, 25.0, 30.0, 35.0, 38.0, 41.0, 100.0)]
    segs = build_segments(_transcript(6, 20.0), frames, window_sec=60.0, max_frames=3)
    # 6 x 20s transcript segments → windows of ≤60s → 2 windows
    assert len(segs) == 2
    assert segs[0].start == 0.0 and segs[0].end == 60.0
    # 6 frames fall in [0, 60) — capped to 3
    assert len(segs[0].frames) == 3
    # 100.0 falls in [60, 120)
    assert len(segs[1].frames) == 1
    assert segs[0].text.startswith("segment 0")


def test_build_segments_empty_window_keeps_no_frames():
    from td_mcp.ingest.tutorial_vision import build_segments
    segs = build_segments(_transcript(3, 20.0), [], window_sec=60.0)
    assert len(segs) == 1
    assert segs[0].frames == []


def test_extract_segment_error_is_explicit(tmp_path):
    """API failure must yield status=error, never a fabricated extraction."""
    import sys
    import types
    from td_mcp.ingest.tutorial_vision import Segment, extract_segment

    frame = tmp_path / "kf.png"
    frame.write_bytes(b"png")
    seg = Segment(index=0, start=0.0, end=60.0, text="hello", frames=[frame])

    fake_anthropic = types.ModuleType("anthropic")

    class _Boom:
        def __init__(self):
            raise RuntimeError("no api key")

    fake_anthropic.Anthropic = _Boom
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        result = extract_segment(seg, "Test video")

    assert result["status"] == "error"
    assert "extraction" not in result


def test_extract_segment_invalid_json_is_error(tmp_path):
    import sys
    import types
    from td_mcp.ingest.tutorial_vision import Segment, extract_segment

    frame = tmp_path / "kf.png"
    frame.write_bytes(b"png")
    seg = Segment(index=0, start=0.0, end=60.0, text="hello", frames=[frame])

    class _Msg:
        class _Block:
            text = "this is not json at all"
        content = [_Block()]

    class _Messages:
        def create(self, **kwargs):
            return _Msg()

    class _Client:
        def __init__(self):
            self.messages = _Messages()

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = _Client
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        result = extract_segment(seg, "Test video")

    assert result["status"] == "error"


def _write_fake_cache(tmp_path: Path) -> Path:
    """A cache dir with one video carrying a techniques.json."""
    vdir = tmp_path / "somechannel" / "vid01"
    vdir.mkdir(parents=True)
    (vdir / "meta.json").write_text(json.dumps({
        "video_id": "vid01", "title": "Feedback loops in TD",
        "duration_sec": 120.0, "channel": "somechannel",
        "url": "https://www.youtube.com/watch?v=vid01",
    }))
    (vdir / "techniques.json").write_text(json.dumps({
        "video_id": "vid01", "model": "claude-sonnet-5",
        "segments": {
            "0": {
                "status": "ok", "start": 0.0, "end": 60.0,
                "transcript": "we create a noise and a feedback",
                "extraction": {
                    "technique": "Feedback trail",
                    "summary": "Noise into feedback loop.",
                    "operators": ["noiseTOP", "feedbackTOP"],
                    "families": [],
                    "parameters": [{"operator": "noise1", "parameter": "Roughness", "value": "0.35"}],
                    "connections": [{"source": "noise1", "target": "feedback1"}],
                    "uses_glsl": False, "uses_python": False,
                    "confidence": "high", "nothing_technical": False,
                },
            },
            "1": {"status": "error", "error": "boom", "start": 60.0, "end": 120.0},
            "2": {
                "status": "ok", "start": 120.0, "end": 180.0,
                "extraction": {"technique": "", "summary": "", "nothing_technical": True},
            },
        },
    }))
    return tmp_path


def test_build_chunks_from_techniques(tmp_path):
    from td_mcp.ingest.tutorial_vision import build_chunks_from_cache
    cache = _write_fake_cache(tmp_path)
    chunks = build_chunks_from_cache(cache)
    # error segment and nothing_technical segment are excluded
    assert len(chunks) == 1
    c = chunks[0]
    assert c.id == "ytv_vid01_00"
    assert c.source == "tutorial"
    assert c.operators == ["noiseTOP", "feedbackTOP"]
    assert c.families == ["TOP"]  # inferred from operator suffixes
    assert "noise1.Roughness = 0.35" in c.text
    assert "noise1 → feedback1" in c.text
    assert c.source_url.endswith("&t=0s")


def test_process_video_requires_transcript(tmp_path):
    from td_mcp.ingest.tutorial_vision import process_video
    from td_mcp.ingest.youtube import VideoMeta
    meta = VideoMeta("nope", "No transcript", 10.0, "chan", "https://x")
    result = process_video(meta, "chan", cache_dir=tmp_path)
    assert result["ok"] is False
    assert "transcript" in result["error"]


def test_process_video_resumes_from_techniques(tmp_path):
    """Segments already extracted ok are not re-sent to the API."""
    from td_mcp.ingest import tutorial_vision as tv
    from td_mcp.ingest.youtube import VideoMeta

    vdir = tmp_path / "chan" / "vid02"
    vdir.mkdir(parents=True)
    (vdir / "transcript.json").write_text(json.dumps(_transcript(3, 20.0)))
    frame = vdir / "keyframes" / "kf_000005000.png"
    frame.parent.mkdir()
    frame.write_bytes(b"png")
    (vdir / "keyframes" / "keyframes.json").write_text(
        json.dumps([{"t": 5.0, "path": str(frame)}])
    )
    (vdir / "video.mp4").write_bytes(b"fake")
    (vdir / "techniques.json").write_text(json.dumps({
        "video_id": "vid02", "model": "claude-sonnet-5",
        "segments": {"0": {"status": "ok", "start": 0.0, "end": 60.0,
                           "extraction": {"technique": "t", "summary": "s"}}},
    }))

    meta = VideoMeta("vid02", "Cached", 60.0, "chan", "https://x")
    with patch.object(tv, "extract_segment") as ex:
        report = tv.process_video(meta, "chan", cache_dir=tmp_path)

    ex.assert_not_called()
    assert report["ok"] is True
    assert report["skipped_cached"] == 1


# ─────────────────────────── keyframe extraction resilience ─────────────────


import pytest  # noqa: E402


def _failing_ffmpeg(cmd, **kwargs):
    class R:
        returncode = 1
        stderr = "moov atom not found"
        stdout = ""
    return R()


def test_extract_keyframes_failed_ffmpeg_is_not_cached(tmp_path):
    """An ffmpeg failure (corrupt video) must not persist an empty manifest
    — otherwise every later run short-circuits on it and the video is
    never retried."""
    from td_mcp.ingest.tutorial_vision import extract_keyframes

    video = tmp_path / "video.mp4"
    video.write_bytes(b"truncated")
    out_dir = tmp_path / "frames"

    with patch("td_mcp.ingest.tutorial_vision.subprocess.run", side_effect=_failing_ffmpeg):
        result = extract_keyframes(video, out_dir)
    assert result == []
    assert not (out_dir / "keyframes.json").exists(), "empty result cached permanently"


def test_extract_keyframes_ignores_cached_empty_manifest(tmp_path):
    """Legacy caches may already hold an empty manifest — treat it as
    absent and retry the extraction."""
    import json as _json

    from td_mcp.ingest.tutorial_vision import extract_keyframes

    video = tmp_path / "video.mp4"
    video.write_bytes(b"truncated")
    out_dir = tmp_path / "frames"
    out_dir.mkdir()
    (out_dir / "keyframes.json").write_text(_json.dumps([]))

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _failing_ffmpeg(cmd)

    with patch("td_mcp.ingest.tutorial_vision.subprocess.run", side_effect=fake_run):
        extract_keyframes(video, out_dir)
    assert calls, "empty cached manifest short-circuited the retry"


@pytest.mark.asyncio
async def test_vision_tool_limit_caps_attempts_not_successes(tmp_path, monkeypatch):
    """limit must bound ATTEMPTS: a cache full of failing videos otherwise
    gets fully re-walked (and re-billed) on every call. limit=0 means all,
    consistent with kb_ingest_youtube_channel."""
    import json as _json
    import sys
    import types

    from td_mcp import server

    for vid in ("v1", "v2", "v3"):
        vdir = tmp_path / "chan" / vid
        vdir.mkdir(parents=True)
        (vdir / "meta.json").write_text(_json.dumps({
            "video_id": vid, "title": vid, "duration_sec": 10.0,
            "channel": "chan", "url": f"https://youtu.be/{vid}",
        }))
        (vdir / "transcript.json").write_text("{}")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setattr("td_mcp.ingest.youtube.DEFAULT_CACHE_DIR", tmp_path)
    monkeypatch.setitem(sys.modules, "anthropic", types.ModuleType("anthropic"))

    attempts = []

    def failing_process(meta, chan, model=None):
        attempts.append(meta.video_id)
        return {"ok": False, "error": "no keyframes"}

    with patch("td_mcp.ingest.tutorial_vision.process_video", side_effect=failing_process), \
         patch("td_mcp.ingest.tutorial_vision.manifest", return_value={}):
        await server.kb_ingest_tutorial_vision(limit=1)

    assert len(attempts) == 1, f"limit=1 attempted {len(attempts)} videos"
