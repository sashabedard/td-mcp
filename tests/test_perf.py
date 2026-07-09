"""td_perf — cook-cost report + cook-pressure watchdog."""
from unittest.mock import AsyncMock, patch

import pytest


def _fake_report(top_rows):
    return {
        "ok": True,
        "path": "/project1",
        "measured_ops": len(top_rows),
        "frame_budget_ms": 16.67,
        "total_cpu_ms": sum(r["cpu_ms"] for r in top_rows),
        "total_gpu_ms": sum(r["gpu_ms"] for r in top_rows),
        "top": top_rows,
    }


@pytest.mark.asyncio
async def test_td_perf_flags_budget_eaters():
    """Ops eating more than half the frame budget get called out — that's
    the early warning before the graph wedges the bridge."""
    from td_mcp import server

    rows = [
        {"path": "/project1/env1", "op_type": "environmentlightCOMP",
         "cpu_ms": 2.0, "gpu_ms": 12.0, "children_cpu_ms": 0, "children_gpu_ms": 0,
         "total_cooks": 900},
        {"path": "/project1/noise1", "op_type": "noiseTOP",
         "cpu_ms": 0.1, "gpu_ms": 0.4, "children_cpu_ms": 0, "children_gpu_ms": 0,
         "total_cooks": 900},
    ]
    with patch.object(server, "_call", new=AsyncMock(return_value=_fake_report(rows))):
        result = await server.td_perf()
    assert result["ok"] is True
    assert "budget_eaters" in result
    assert result["budget_eaters"] == ["/project1/env1"]


@pytest.mark.asyncio
async def test_td_perf_quiet_when_graph_is_light():
    from td_mcp import server

    rows = [
        {"path": "/project1/noise1", "op_type": "noiseTOP",
         "cpu_ms": 0.1, "gpu_ms": 0.2, "children_cpu_ms": 0, "children_gpu_ms": 0,
         "total_cooks": 900},
    ]
    with patch.object(server, "_call", new=AsyncMock(return_value=_fake_report(rows))):
        result = await server.td_perf()
    assert result["ok"] is True
    assert "budget_eaters" not in result


# ─────────────────────────── cook-pressure watchdog ──────────────────────────


def test_cook_pressure_none_below_threshold():
    from td_mcp.bridge import TDBridge

    b = TDBridge()
    b._latencies.extend([12.0, 30.0, 25.0, 18.0])
    assert b.cook_pressure() is None


def test_cook_pressure_flags_sustained_slowness():
    from td_mcp.bridge import TDBridge

    b = TDBridge()
    b._latencies.extend([900.0, 1200.0, 4800.0, 1500.0])
    pressure = b.cook_pressure()
    assert pressure is not None
    assert pressure["median_ms"] > 750


def test_cook_pressure_needs_enough_samples():
    """One slow call (a legit 180s catalog refresh) must not trip it."""
    from td_mcp.bridge import TDBridge

    b = TDBridge()
    b._latencies.extend([180_000.0])
    assert b.cook_pressure() is None


def test_cook_pressure_median_robust_to_one_outlier():
    """A single long catalog refresh among fast calls must not trip it."""
    from td_mcp.bridge import TDBridge

    b = TDBridge()
    b._latencies.extend([20.0, 15.0, 180_000.0, 25.0, 30.0])
    assert b.cook_pressure() is None


@pytest.mark.asyncio
async def test_call_attaches_cook_pressure_warning():
    from td_mcp import server

    server.bridge._latencies.clear()
    server.bridge._latencies.extend([2000.0, 3000.0, 2500.0, 4000.0])
    try:
        with patch.object(server.bridge, "send", new=AsyncMock(return_value={"frame": 1})):
            result = await server._call("get_status")
    finally:
        server.bridge._latencies.clear()
    assert result["ok"] is True
    assert "cook_pressure_warning" in result
    assert "td_perf" in result["cook_pressure_warning"]["hint"]
