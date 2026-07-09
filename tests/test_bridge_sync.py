"""Bridge auto-sync: hash comparison and repair-path decision logic."""
import hashlib
from unittest.mock import AsyncMock, patch

import pytest


def test_bridge_script_hash_matches_bridge_side_convention():
    """Server hashes file.strip(); bridge hashes me.text.strip() — the two
    must agree on the same content."""
    from td_mcp.server import _bridge_script

    result = _bridge_script()
    assert result is not None
    text, digest, path = result
    assert digest == hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
    assert path.endswith("webserver_callbacks.py")


@pytest.mark.asyncio
async def test_sync_reports_synced_when_hashes_match():
    from td_mcp import server

    _text, local_hash, _p = server._bridge_script()
    with patch.object(server, "_call", new=AsyncMock(return_value={"script_hash": local_hash})):
        result = await server._sync_bridge_script()
    assert result["status"] == "synced"


@pytest.mark.asyncio
async def test_sync_repairs_on_drift_and_verifies():
    from td_mcp import server

    _text, local_hash, _p = server._bridge_script()
    calls = []

    async def fake_call(action, **kw):
        calls.append(action)
        if action == "bridge_version":
            # première réponse: dérive; après réparation: à jour
            if calls.count("bridge_version") == 1:
                return {"ok": True, "script_hash": "stale"}
            return {"ok": True, "script_hash": local_hash}
        if action == "run_script":
            return {"ok": True, "output": "/MCP/webserver1_callbacks\n"}
        raise AssertionError(action)

    with patch.object(server, "_call", new=fake_call):
        result = await server._sync_bridge_script()
    assert result["status"] == "updated"
    assert "run_script" in calls


@pytest.mark.asyncio
async def test_sync_fails_gracefully_when_no_dat_found():
    from td_mcp import server

    async def fake_call(action, **kw):
        if action == "bridge_version":
            raise RuntimeError("Unknown action")  # vieux bridge
        return {"output": ""}

    with patch.object(server, "_call", new=fake_call):
        result = await server._sync_bridge_script()
    assert result["status"] == "failed"


def test_bridge_script_falls_back_to_packaged_copy(tmp_path, monkeypatch):
    """Wheel installs don't ship td_bridge_tox/ — the script must resolve
    from the package-data copy so bridge sync works outside a checkout."""
    from td_mcp import server

    repo_copy = tmp_path / "repo" / "webserver_callbacks.py"
    packaged_copy = tmp_path / "pkg" / "webserver_callbacks.py"
    packaged_copy.parent.mkdir(parents=True)
    packaged_copy.write_text("def onWebSocketReceiveText(): pass\n")

    monkeypatch.setattr(server, "_BRIDGE_SCRIPT_CANDIDATES", [repo_copy, packaged_copy])
    result = server._bridge_script()
    assert result is not None
    text, sha, path = result
    assert path == str(packaged_copy)
    assert "onWebSocketReceiveText" in text


@pytest.mark.asyncio
async def test_sync_surfaces_repair_script_error(monkeypatch):
    """A repair that errors TD-side (e.g. ascii-encode crash on em-dashes)
    must surface the real error — not masquerade as 'no callbacks DAT'."""
    from unittest.mock import AsyncMock

    from td_mcp import server

    async def fake_call(action, **data):
        if action == "bridge_version":
            return {"ok": True, "script_hash": "not-matching"}
        if action == "run_script":
            return {"ok": False, "error": "'ascii' codec can't encode character '\\u2014'"}
        raise AssertionError(action)

    monkeypatch.setattr(server, "_call", AsyncMock(side_effect=fake_call))
    result = await server._sync_bridge_script()
    assert result["status"] == "failed"
    assert "ascii" in result["reason"]
