from pathlib import Path
from unittest.mock import patch


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
