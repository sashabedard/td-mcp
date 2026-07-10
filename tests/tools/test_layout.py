from td_mcp.tools.layout import (
    assign_columns_by_depth,
    detect_clusters,
    geometric_layout,
    propose_rename,
)


def test_assign_columns_simple_chain():
    ops = ["A", "B", "C"]
    edges = [("A", "B"), ("B", "C")]
    cols = assign_columns_by_depth(ops, edges)
    assert cols == {"A": 0, "B": 1, "C": 2}


def test_assign_columns_diamond():
    ops = ["A", "B", "C", "D"]
    edges = [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")]
    cols = assign_columns_by_depth(ops, edges)
    assert cols["A"] == 0
    assert cols["D"] == 2
    assert cols["B"] == 1 and cols["C"] == 1


def test_geometric_layout_returns_grid_positions():
    ops_meta = [
        {"path": "/p/A", "family": "TOP", "column": 0},
        {"path": "/p/B", "family": "TOP", "column": 1},
        {"path": "/p/audio1", "family": "CHOP", "column": 0},
    ]
    positions = geometric_layout(ops_meta, col_width=200, row_height=150)
    assert positions["/p/A"][0] == 0
    assert positions["/p/B"][0] == 200
    assert positions["/p/A"][1] != positions["/p/audio1"][1]


def test_detect_cluster_audio_reactive():
    ops_meta = [
        {"path": "/p/audiofilein1", "op_type": "audiofileinCHOP"},
        {"path": "/p/analyze1", "op_type": "analyzeCHOP"},
        {"path": "/p/other", "op_type": "constantTOP"},
    ]
    edges = [("/p/audiofilein1", "/p/analyze1")]
    clusters = detect_clusters(ops_meta, edges)
    audio = [c for c in clusters if c["name"] == "Audio reactive"]
    assert len(audio) == 1
    assert "/p/audiofilein1" in audio[0]["members"]


def test_detect_cluster_feedback_loop():
    ops_meta = [{"path": "/p/feedback1", "op_type": "feedbackTOP"}]
    edges = []
    clusters = detect_clusters(ops_meta, edges)
    assert any(c["name"] == "Feedback loop" for c in clusters)


def test_propose_rename_audio_downstream():
    op = {"path": "/p/null1", "op_type": "nullCHOP"}
    upstream_types = ["audiofileinCHOP", "analyzeCHOP"]
    new_name = propose_rename(op, upstream_types)
    assert new_name == "null_audioRMS"


def test_propose_rename_keeps_non_generic_names():
    op = {"path": "/p/myImportantThing", "op_type": "nullCHOP"}
    assert propose_rename(op, []) is None


from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_td_layout_network_empty_returns_empty_diff():
    fake_network = {"ops": [], "connections": []}
    with patch("td_mcp.server.bridge") as fake_bridge:
        fake_bridge.send = AsyncMock(side_effect=[fake_network])
        from td_mcp.server import td_layout_network
        result = await td_layout_network(parent="/", mode="grid_annotated")
        assert result["ok"] is True
        assert result["diff"]["moved"] == []


@pytest.mark.asyncio
async def test_td_layout_network_simple_chain_moves_ops(tmp_path):
    import td_mcp.server as server

    fake_network = {
        "ops": [
            {"path": "/p/a", "op_type": "constantTOP", "family": "TOP", "x": 999, "y": 999, "name": "a"},
            {"path": "/p/b", "op_type": "blurTOP", "family": "TOP", "x": 999, "y": 999, "name": "b"},
        ],
        "connections": [{"src": "/p/a", "dst": "/p/b"}],
    }

    async def fake_send(action, data=None, timeout=None):
        if action == "get_network":
            return fake_network
        if action == "get_project_folder":
            return {"folder": str(tmp_path)}
        if action == "checkpoint":
            return {"comp_path": data["comp_path"], "file_path": data["file_path"]}
        if action == "apply_layout":
            return {"ok": True, "applied": {}}
        raise AssertionError(f"unexpected action {action}")

    server._project_folder_cache = None
    server._checkpoints.clear()
    with patch.object(server.bridge, "send", new=AsyncMock(side_effect=fake_send)):
        result = await server.td_layout_network(parent="/p", mode="grid")
        assert result["ok"] is True
        assert len(result["diff"]["moved"]) == 2
        assert result["diff"]["checkpoint_id"].startswith("cp_")


@pytest.mark.asyncio
async def test_td_layout_network_checkpoint_is_rollbackable(tmp_path):
    """The checkpoint taken by td_layout_network must be registered so
    td_rollback(diff.checkpoint_id) actually restores — the docstring
    promises it."""
    import td_mcp.server as server

    fake_network = {
        "ops": [
            {"path": "/p/a", "op_type": "constantTOP", "family": "TOP", "x": 0, "y": 0, "name": "a"},
        ],
        "connections": [],
    }
    actions = []

    async def fake_send(action, data=None, timeout=None):
        actions.append(action)
        if action == "get_network":
            return fake_network
        if action == "get_project_folder":
            return {"folder": str(tmp_path)}
        if action == "checkpoint":
            return {"comp_path": data["comp_path"], "file_path": data["file_path"]}
        if action == "apply_layout":
            return {"ok": True, "applied": {}}
        if action == "rollback":
            return {"restored_path": data["comp_path"], "file_path": data["file_path"]}
        raise AssertionError(f"unexpected action {action}")

    server._project_folder_cache = None
    server._checkpoints.clear()
    with patch.object(server.bridge, "send", new=AsyncMock(side_effect=fake_send)):
        result = await server.td_layout_network(parent="/p", mode="grid")
        assert result["ok"] is True
        cid = result["diff"]["checkpoint_id"]
        assert cid, "layout must return a usable checkpoint id"

        rollback = await server.td_rollback(cid)
        assert rollback["ok"] is True, rollback.get("error", "")
        assert "rollback" in actions


@pytest.mark.asyncio
async def test_td_connect_resets_project_folder_cache():
    """Reconnecting (possibly to another project) must not reuse the old
    project's folder for checkpoints."""
    import td_mcp.server as server

    server._project_folder_cache = "/stale/old-project"
    with patch.object(server.bridge, "connect", new=AsyncMock()), \
         patch.object(server, "_sync_bridge_script", new=AsyncMock(return_value={"status": "synced"})):
        await server.td_connect("ws://127.0.0.1:9988")
    assert server._project_folder_cache is None


def test_detect_clusters_audio_chain_multihop():
    """Real audio chains have several hops between input and analysis —
    the BFS must collect the whole downstream CHOP subgraph."""
    from td_mcp.tools.layout import detect_clusters

    ops = [
        {"path": "/b/audioin", "op_type": "audiodeviceinCHOP"},
        {"path": "/b/sel", "op_type": "selectCHOP"},
        {"path": "/b/spec", "op_type": "audiospectrumCHOP"},
        {"path": "/b/energy", "op_type": "analyzeCHOP"},
        {"path": "/b/kick", "op_type": "nullCHOP"},
        {"path": "/b/tex", "op_type": "noiseTOP"},  # non-CHOP: excluded
    ]
    edges = [
        ("/b/audioin", "/b/sel"), ("/b/sel", "/b/spec"),
        ("/b/spec", "/b/energy"), ("/b/energy", "/b/kick"),
        ("/b/kick", "/b/tex"),
    ]
    clusters = detect_clusters(ops, edges)
    audio = [c for c in clusters if c["name"] == "Audio reactive"]
    assert len(audio) == 1
    assert set(audio[0]["members"]) == {"/b/audioin", "/b/sel", "/b/spec", "/b/energy", "/b/kick"}
