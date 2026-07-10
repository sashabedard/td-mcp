"""Palette scan/resolution — the pure logic behind td_palette_list/load."""
from pathlib import Path

from td_mcp.tools.palette import filter_palette, resolve_tox, scan_palette


def _mk(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"tox")


def test_scan_recurses_and_skips_non_tox(tmp_path):
    _mk(tmp_path, "Tools/moviePlayer.tox")
    _mk(tmp_path, "UI/deep/slider.tox")
    _mk(tmp_path, "readme.md")
    _mk(tmp_path, "archive.zip")
    entries = scan_palette(tmp_path, "builtin")
    assert [(e["relpath"], e["source"]) for e in entries] == [
        ("Tools/moviePlayer.tox", "builtin"),
        ("UI/deep/slider.tox", "builtin"),
    ]


def test_scan_missing_root_is_empty_not_error(tmp_path):
    assert scan_palette(tmp_path / "nope", "user") == []


def test_filter_matches_name_and_relpath():
    entries = [
        {"name": "moviePlayer", "relpath": "Tools/moviePlayer.tox", "source": "builtin"},
        {"name": "kantanMapper", "relpath": "Mapping/kantanMapper.tox", "source": "builtin"},
    ]
    assert filter_palette(entries, "movie") == entries[:1]
    assert filter_palette(entries, "mapping/") == entries[1:]
    assert filter_palette(entries, "") == entries


_ENTRIES = [
    {"name": "Grid", "relpath": "Grid.tox", "source": "user"},
    {"name": "grid", "relpath": "Generators/grid.tox", "source": "builtin"},
    {"name": "moviePlayer", "relpath": "Tools/moviePlayer.tox", "source": "builtin"},
]


def test_resolve_unique_bare_name():
    entry, sugg = resolve_tox("moviePlayer", _ENTRIES)
    assert entry["relpath"] == "Tools/moviePlayer.tox"
    assert sugg == []


def test_resolve_relpath_with_or_without_extension():
    for ident in ("Tools/moviePlayer.tox", "tools/movieplayer"):
        entry, _ = resolve_tox(ident, _ENTRIES)
        assert entry["relpath"] == "Tools/moviePlayer.tox"


def test_resolve_name_collision_lists_qualified_candidates():
    entry, sugg = resolve_tox("grid", _ENTRIES)
    assert entry is None
    assert sugg == ["builtin:Generators/grid.tox", "user:Grid.tox"]


def test_resolve_source_prefix_disambiguates():
    entry, _ = resolve_tox("user:Grid", _ENTRIES)
    assert entry["source"] == "user"
    entry, _ = resolve_tox("builtin:grid", _ENTRIES)
    assert entry["relpath"] == "Generators/grid.tox"


def test_resolve_typo_gets_close_matches():
    entry, sugg = resolve_tox("moviPlayer", _ENTRIES)
    assert entry is None
    assert "builtin:Tools/moviePlayer.tox" in sugg


# ─────────────────────── server tools without a bridge ────────────────────────

from unittest.mock import AsyncMock, patch

from td_mcp import server
from td_mcp.protocol import TDError


async def test_palette_list_without_bridge_says_connect_first():
    """A dead bridge raised KeyError 'value' (observed live after /mcp
    reconnect); it must say what to do instead."""
    server._palette_roots_cache = None
    with patch.object(server.bridge, "send",
                      new=AsyncMock(side_effect=TDError("not connected"))):
        result = await server.td_palette_list()
    assert result["ok"] is False
    assert "td_connect" in result["error"]


async def test_palette_load_without_bridge_says_connect_first():
    server._palette_roots_cache = None
    with patch.object(server.bridge, "send",
                      new=AsyncMock(side_effect=TDError("not connected"))):
        result = await server.td_palette_load("bloom")
    assert result["ok"] is False
    assert "td_connect" in result["error"]
