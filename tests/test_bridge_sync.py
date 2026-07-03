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
            return {"script_hash": "stale"} if calls.count("bridge_version") == 1 else {"script_hash": local_hash}
        if action == "run_script":
            return {"output": "/MCP/webserver1_callbacks\n"}
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
