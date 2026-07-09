"""td_plan registration + the soft gate on td_create_op.

The gate exists because prompt-only protocols get rationalized away mid-task
(observed live: an identified KB gap was flagged, then built through anyway,
costing a full build-diagnose-rebuild cycle on the point-vortex tutorial).
"""
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_plan_state():
    from td_mcp import server

    server._session_plan = None
    server._plan_gaps_warned = False
    yield
    server._session_plan = None
    server._plan_gaps_warned = False


STAGES = [
    {"name": "sim", "ops": ["particlePOP"], "kb_source": "ytv_x_04", "confidence": "vision-chunk"},
]


@pytest.mark.asyncio
async def test_td_plan_requires_intention_and_stages():
    from td_mcp import server

    assert (await server.td_plan("", STAGES))["ok"] is False
    assert (await server.td_plan("build a vortex", []))["ok"] is False


@pytest.mark.asyncio
async def test_td_plan_registers_and_reports_gaps():
    from td_mcp import server

    result = await server.td_plan(
        "rebuild tutorial", STAGES, gaps=["pointgen config unknown (chunk 03 missing)"]
    )
    assert result["ok"] and result["registered"]
    assert "gap_protocol" in result
    assert server._session_plan["gaps"]


@pytest.mark.asyncio
async def test_td_plan_flags_improvised_stages():
    from td_mcp import server

    stages = STAGES + [{"name": "emitter", "confidence": "improvised"}]
    result = await server.td_plan("rebuild", stages)
    assert result["improvised_stages"] == ["emitter"]


@pytest.mark.asyncio
async def test_create_without_plan_warns_but_succeeds():
    from td_mcp import server

    with patch.object(server, "_call", new=AsyncMock(return_value={"ok": True, "path": "/p/n1"})):
        result = await server.td_create_op("noiseCHOP")
    assert result["ok"] is True
    assert "plan_warning" in result


@pytest.mark.asyncio
async def test_create_without_plan_hard_fails_when_env_set(monkeypatch):
    from td_mcp import server

    monkeypatch.setenv("TD_MCP_REQUIRE_PLAN", "1")
    with patch.object(server, "_call", new=AsyncMock(return_value={"ok": True})):
        result = await server.td_create_op("noiseCHOP")
    assert result["ok"] is False
    assert "td_plan" in result["error"]


@pytest.mark.asyncio
async def test_create_with_plan_no_warning():
    from td_mcp import server

    await server.td_plan("build", STAGES)
    with patch.object(server, "_call", new=AsyncMock(return_value={"ok": True, "path": "/p/n1"})):
        result = await server.td_create_op("noiseCHOP")
    assert result["ok"] is True
    assert "plan_warning" not in result


@pytest.mark.asyncio
async def test_gap_warning_survives_a_catalog_rejected_create():
    """A create rejected by the catalog gate must still SHOW the one-shot
    gaps warning — computing it and silently discarding it burns the flag
    and the warning is never seen by anyone."""
    from td_mcp import server

    await server.td_plan("build", STAGES, gaps=["missing chunk"])
    with patch.object(server, "_call", new=AsyncMock(return_value={"ok": True, "path": "/p/n1"})):
        rejected = await server.td_create_op("noiseCHOPP")  # typo → catalog rejection
        assert rejected["ok"] is False
        assert "plan_warning" in rejected, "gap warning silently discarded"


@pytest.mark.asyncio
async def test_require_plan_env_zero_means_disabled(monkeypatch):
    from td_mcp import server

    monkeypatch.setenv("TD_MCP_REQUIRE_PLAN", "0")
    with patch.object(server, "_call", new=AsyncMock(return_value={"ok": True, "path": "/p/n1"})):
        result = await server.td_create_op("noiseCHOP")
    assert result["ok"] is True, "TD_MCP_REQUIRE_PLAN=0 must not enable the hard gate"
    assert "plan_warning" in result


@pytest.mark.asyncio
async def test_unresolved_gaps_warn_once_then_stay_quiet():
    from td_mcp import server

    await server.td_plan("build", STAGES, gaps=["missing chunk"])
    fresh = AsyncMock(side_effect=lambda *a, **k: {"ok": True, "path": "/p/n1"})
    with patch.object(server, "_call", new=fresh):
        first = await server.td_create_op("noiseCHOP")
        second = await server.td_create_op("noiseCHOP")
    assert "plan_warning" in first
    assert "plan_warning" not in second
