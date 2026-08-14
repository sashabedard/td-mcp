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


# ─────────────────────────── batch resilience ───────────────────────────────

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402

from td_mcp.ingest.youtube import download_audio  # noqa: E402


def _meta(vid="abc123", title="t"):
    return VideoMeta(vid, title, 60.0, "chan", f"https://youtu.be/{vid}")


def test_download_audio_resumes_from_non_m4a(tmp_path: Path):
    """bestaudio may land as .webm — a cached .webm must short-circuit,
    not trigger a fresh network download on every run."""
    vdir = tmp_path / "chan" / "abc123"
    vdir.mkdir(parents=True)
    (vdir / "audio.webm").write_bytes(b"audio")

    with patch("td_mcp.ingest.youtube.subprocess") as sp:
        sp.run.side_effect = AssertionError("network hit for cached audio")
        sp.check_call.side_effect = AssertionError("network hit for cached audio")
        result = download_audio(_meta(), "chan", cache_dir=tmp_path)
    assert result.name == "audio.webm"


def test_download_audio_ignores_partial_files(tmp_path: Path):
    """A leftover audio.m4a.part must NOT count as cached audio."""
    vdir = tmp_path / "chan" / "abc123"
    vdir.mkdir(parents=True)
    (vdir / "audio.m4a.part").write_bytes(b"partial")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        (vdir / "audio.m4a").write_bytes(b"audio")

        class R:
            returncode = 0
        return R()

    with patch("td_mcp.ingest.youtube.subprocess.run", side_effect=fake_run), \
         patch("td_mcp.ingest.youtube.subprocess.check_call", side_effect=lambda cmd, **k: fake_run(cmd)):
        result = download_audio(_meta(), "chan", cache_dir=tmp_path)
    assert calls, "partial file wrongly treated as cached audio"
    assert result.name == "audio.m4a"


def test_list_channel_videos_reads_playlist_channel_not_channel():
    """--flat-playlist never fills the per-entry channel field; it prints
    "NA" for every video. Reading it attributed 133 of 152 cached videos to
    a channel called "NA", and that string is embedded into chunk text.
    playlist_channel is the field that actually carries the name here."""
    from td_mcp.ingest.youtube import list_channel_videos

    SEP = "\x1f"
    line = SEP.join(["vid1", "612.0", "Okamirufu Vizualizer", "https://y/vid1", "A | B"])
    with patch("td_mcp.ingest.youtube.subprocess.check_output", return_value=line) as co:
        videos = list_channel_videos("https://youtube.com/@x/videos")

    template = co.call_args[0][0][co.call_args[0][0].index("--print") + 1]
    assert "%(playlist_channel)s" in template
    assert "%(channel)s" not in template
    # Title last: it is the only free-text field, so its content cannot
    # shift channel or url out of position.
    assert template.rindex("%(title)s") > template.rindex("%(webpage_url)s")
    assert videos[0].channel == "Okamirufu Vizualizer"
    assert videos[0].url == "https://y/vid1"
    assert videos[0].title == "A | B"
    assert videos[0].duration_sec == 612.0


def test_attribution_pairs_display_name_with_handle():
    """The attribution string is embedded, so it is what channel-name
    search matches. Neither half suffices alone: legacy records carry "NA"
    as the display name, and Derivative's channel is literally named
    "TouchDesigner", which discriminates nothing."""
    from td_mcp.ingest.youtube import _attribution

    def meta_with(channel):
        return VideoMeta("v", "t", 1.0, channel, "https://y/v")

    assert _attribution(meta_with("TouchDesigner"), "Derivative") == "TouchDesigner (Derivative)"
    # Legacy "NA" and empties fall back to the handle rather than embedding junk.
    assert _attribution(meta_with("NA"), "OkamirufuV") == "OkamirufuV"
    assert _attribution(meta_with(""), "@paketa12") == "paketa12"
    # No redundant "paketa12 (paketa12)".
    assert _attribution(meta_with("paketa12"), "paketa12") == "paketa12"
    # Some channels put the handle inside the display name already —
    # elekktronaut's is literally "bileam tschepe (elekktronaut)". Appending
    # it again would embed "... (elekktronaut) (elekktronaut)".
    assert (
        _attribution(meta_with("bileam tschepe (elekktronaut)"), "elekktronaut")
        == "bileam tschepe (elekktronaut)"
    )


def test_download_cmd_bakes_in_no_volatile_workaround():
    """YouTube workarounds expire. The 2026-07 pair (web_safari client +
    proto:m3u8 sort) had both gone stale by 2026-08, web_safari returning
    zero formats. Volatile choices belong in the attempt chain, never
    welded into the command builder. A video id starting with '-' must also
    survive as an argument, not be read as a flag."""
    from td_mcp.ingest.youtube import _ytdlp_cmd

    url = "https://youtu.be/-aCBi1r3AaI"
    cmd = _ytdlp_cmd(
        url,
        "/tmp/audio.%(ext)s",
        "bestaudio[ext=m4a]/bestaudio/best",
        cookies_browser="",
        player_client="",
    )
    assert "--extractor-args" not in cmd
    assert "-S" not in cmd
    assert "--cookies-from-browser" not in cmd
    assert "bestaudio[ext=m4a]/bestaudio/best" in cmd
    assert cmd[cmd.index("--") + 1] == url


def test_download_audio_walks_client_chain_until_one_delivers(tmp_path: Path, monkeypatch):
    """Some player clients advertise a format and then 403 on the bytes
    (android_vr, observed 2026-08), so yt-dlp's own rotation fails
    intermittently. The chain must keep going and stop at the first client
    that actually writes audio."""
    monkeypatch.setattr(
        "td_mcp.ingest.youtube.YTDLP_PLAYER_CLIENTS", ["bad", "alsobad", "good"]
    )
    monkeypatch.setattr("td_mcp.ingest.youtube.YTDLP_COOKIES_BROWSER", "")
    vdir = tmp_path / "chan" / "abc123"
    tried = []

    def fake_run(cmd, **kwargs):
        client = cmd[cmd.index("--extractor-args") + 1]
        tried.append(client)

        class R:
            returncode = 0 if client.endswith("=good") else 1
            stderr = "HTTP Error 403: Forbidden"

        if R.returncode == 0:
            (vdir / "audio.m4a").write_bytes(b"audio")
        return R()

    with patch("td_mcp.ingest.youtube.subprocess.run", side_effect=fake_run):
        result = download_audio(_meta(), "chan", cache_dir=tmp_path)

    assert tried == [
        "youtube:player_client=bad",
        "youtube:player_client=alsobad",
        "youtube:player_client=good",
    ]
    assert result.name == "audio.m4a"


def test_download_audio_tries_cookies_last(tmp_path: Path, monkeypatch):
    """A stale browser jar is worse than none — YouTube degrades the
    session to images-only instead of refusing it. Cookies are therefore a
    last resort, never the opening move."""
    monkeypatch.setattr("td_mcp.ingest.youtube.YTDLP_PLAYER_CLIENTS", ["one", "two"])
    monkeypatch.setattr("td_mcp.ingest.youtube.YTDLP_COOKIES_BROWSER", "chrome")
    used_cookies = []

    def fake_run(cmd, **kwargs):
        used_cookies.append("--cookies-from-browser" in cmd)

        class R:
            returncode = 1
            stderr = "nope"

        return R()

    with patch("td_mcp.ingest.youtube.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError):
            download_audio(_meta(), "chan", cache_dir=tmp_path)

    assert used_cookies == [False, False, True]


def test_download_audio_surfaces_ytdlp_stderr(tmp_path: Path, monkeypatch):
    """A bare CalledProcessError hides why yt-dlp failed, which is the one
    thing needed to tell an expired workaround from a private video."""
    monkeypatch.setattr("td_mcp.ingest.youtube.YTDLP_PLAYER_CLIENTS", ["only"])
    monkeypatch.setattr("td_mcp.ingest.youtube.YTDLP_COOKIES_BROWSER", "")

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 1
            stderr = "ERROR: Requested format is not available"

        return R()

    with patch("td_mcp.ingest.youtube.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="Requested format is not available"):
            download_audio(_meta(), "chan", cache_dir=tmp_path)


@pytest.mark.asyncio
async def test_ingest_channel_batch_survives_one_bad_video(monkeypatch):
    """One private/deleted video must not abort the whole batch."""
    import subprocess

    from td_mcp import server

    videos = [_meta("good1", "ok"), _meta("dead2", "private"), _meta("good3", "ok")]

    def fake_download(meta, handle, cache_dir=None):
        if meta.video_id == "dead2":
            raise subprocess.CalledProcessError(1, "yt-dlp")
        return Path(f"/tmp/{meta.video_id}/audio.m4a")

    sources = {"channels": [{"handle": "chan", "url": "https://youtube.com/@chan"}]}
    with patch("td_mcp.ingest.youtube.load_sources", return_value=sources), \
         patch("td_mcp.ingest.youtube.list_channel_videos", return_value=videos), \
         patch("td_mcp.ingest.youtube.download_audio", side_effect=fake_download), \
         patch("td_mcp.ingest.youtube.transcribe", return_value={"text": ""}), \
         patch("td_mcp.ingest.youtube.manifest", return_value={}):
        result = await server.kb_ingest_youtube_channel(handle="chan", limit=0)

    assert result["ok"] is True
    assert result["processed_count"] == 2
    assert result["failed_count"] == 1
    assert result["failed"][0]["video_id"] == "dead2"
