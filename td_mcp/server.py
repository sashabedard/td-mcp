import base64
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from td_mcp import __version__
from td_mcp.bridge import bridge
from td_mcp.protocol import TDError

mcp = FastMCP("td-mcp")

# Per-process checkpoint registry. FIFO at MAX_CHECKPOINTS. .tox files survive
# server restarts on disk; the in-memory list does not — restart-safe listing
# would require a manifest file, deferred until cross-session resume matters.
_checkpoints: list[dict] = []
MAX_CHECKPOINTS = 20
_project_folder_cache: str | None = None


async def _get_project_folder() -> str:
    global _project_folder_cache
    if _project_folder_cache is None:
        result = await bridge.send("get_project_folder")
        _project_folder_cache = result["folder"]
    return _project_folder_cache


async def _call(action: str, **data: Any) -> dict:
    """Bridge call wrapper. Returns {ok: True, **result} or {ok: False, error}."""
    try:
        result = await bridge.send(action, {k: v for k, v in data.items() if v is not None})
        return {"ok": True, **result}
    except TDError as e:
        return {"ok": False, "error": e.message}


# ─────────────────────────── system / connection ───────────────────────────


@mcp.tool()
async def ping() -> dict:
    """Health check. Returns server metadata. Does not require TD connection."""
    return {
        "ok": True,
        "server": "td-mcp",
        "version": __version__,
        "bridge_connected": bridge.connected,
    }


@mcp.tool()
async def td_connect(
    url: str = "ws://127.0.0.1:9988",
    token: str | None = None,
    timeout: float = 5.0,
) -> dict:
    """Open the WebSocket bridge to a running TouchDesigner instance.

    The TD side must have the td_mcp_bridge .tox loaded with a WebServer DAT
    on the matching port (default 9988), and the matching token if set.
    """
    try:
        await bridge.connect(url, token=token, timeout=timeout)
        return {"ok": True, "url": url, "connected": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def td_disconnect() -> dict:
    """Close the WebSocket bridge."""
    await bridge.disconnect()
    return {"ok": True, "connected": False}


@mcp.tool()
async def td_status() -> dict:
    """Get TD app version, project name, FPS, and current frame."""
    return await _call("get_status")


# ─────────────────────────── network introspection ─────────────────────────


@mcp.tool()
async def td_get_network(parent: str = "/project1") -> dict:
    """List all operators under a parent COMP (one level deep).

    Returns path, name, type, and node x/y for each child.
    """
    return await _call("get_network", parent=parent)


@mcp.tool()
async def td_op_info(path: str) -> dict:
    """Get full operator details: type, position, input/output counts, all parameters."""
    return await _call("op_info", path=path)


# ─────────────────────────── network mutation ──────────────────────────────


@mcp.tool()
async def td_create_op(
    op_type: str,
    parent: str = "/project1",
    name: str = "",
    x: int = 0,
    y: int = 0,
) -> dict:
    """Create an operator inside a parent COMP.

    op_type is the TD class name (e.g. 'noiseCHOP', 'rectangleTOP', 'spherePOP').
    NOTE: op_type is currently NOT validated against the operators KB — typos
    will raise a TD-side AttributeError. KB-backed validation lands in Phase 3.
    """
    return await _call("create_op", type=op_type, parent=parent, name=name, x=x, y=y)


@mcp.tool()
async def td_delete_op(path: str) -> dict:
    """Destroy an operator by absolute path. Irreversible — no undo."""
    return await _call("delete_op", path=path)


@mcp.tool()
async def td_connect_ops(
    out: str,
    into: str,
    out_index: int = 0,
    in_index: int = 0,
) -> dict:
    """Wire one operator's output into another's input.

    `out` and `into` are absolute paths. Port indices default to 0.
    Raises if port indices are out of range or the connection is type-incompatible.
    """
    return await _call(
        "connect_ops", out=out, into=into, out_index=out_index, in_index=in_index
    )


# ─────────────────────────── parameters ────────────────────────────────────


@mcp.tool()
async def td_set_param(path: str, param: str, value: int | float | str | bool) -> dict:
    """Set a single parameter value on an operator.

    `param` is the parameter's internal name (e.g. 'period', 'rx', 'amp') —
    NOT the display label. Case sensitive.
    """
    return await _call("set_param", path=path, param=param, value=value)


@mcp.tool()
async def td_pulse(path: str, param: str) -> dict:
    """Trigger a pulse-type parameter (e.g. a 'Reset' button)."""
    return await _call("pulse", path=path, param=param)


# ─────────────────────────── python escape hatches ─────────────────────────


@mcp.tool()
async def td_expr(expression: str) -> dict:
    """Evaluate a single Python expression inside TD. Returns the stringified value.

    Use for read-only introspection (e.g. 'op("/project1").children'). For
    multi-statement code, use td_run_script.
    """
    return await _call("eval", expression=expression)


@mcp.tool()
async def td_run_script(code: str) -> dict:
    """Execute arbitrary Python inside TD. Captures stdout into `output`.

    ESCAPE HATCH — prefer typed tools (td_create_op, td_set_param, etc.) when possible.
    No sandboxing: this can mutate or destroy your project. KB validation does not apply.
    """
    return await _call("run_script", code=code)


# ─────────────────────────── timeline / project ────────────────────────────


@mcp.tool()
async def td_timeline_play() -> dict:
    """Start the project timeline (sets /perform.par.play to 1)."""
    return await _call("timeline_play")


@mcp.tool()
async def td_timeline_stop() -> dict:
    """Stop the project timeline (sets /perform.par.play to 0)."""
    return await _call("timeline_stop")


@mcp.tool()
async def td_save_project(file: str | None = None) -> dict:
    """Save the .toe project. If `file` is given, save-as to that path."""
    return await _call("save_project", file=file)


# ─────────────────────────── visual feedback ───────────────────────────────


@mcp.tool()
async def td_snapshot(op_path: str) -> Image:
    """Force-cook a TOP and return its current frame as a PNG.

    Returns an MCP Image (visible directly to the agent). For the vibe loop,
    this closes the feedback cycle: mutate → snapshot → judge → iterate.

    Raises TDError if the bridge is disconnected or the path is not a TOP.
    """
    result = await bridge.send("snapshot", {"op": op_path})
    png_bytes = base64.b64decode(result["base64"])
    return Image(data=png_bytes, format="png")


# ─────────────────────────── checkpoint / rollback ─────────────────────────


@mcp.tool()
async def td_checkpoint(comp_path: str, label: str = "") -> dict:
    """Export a COMP to a timestamped .tox snapshot for later rollback.

    The .tox file is written to <td_project_folder>/.td_mcp_snapshots/<id>.tox.
    FIFO at 20 checkpoints per session: older snapshots are deleted from disk.

    LIMITATIONS:
    - Target must be a COMP (any family), not a leaf op.
    - Cannot checkpoint root (/project1) — wrap your experiment in a COMP first.
    - External wires entering or leaving the COMP are NOT preserved on rollback,
      because TD's loadTox replaces the operator entirely.
    """
    folder = Path(await _get_project_folder())
    cp_dir = folder / ".td_mcp_snapshots"
    cp_dir.mkdir(exist_ok=True)
    cp_id = f"cp_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    file_path = str(cp_dir / f"{cp_id}.tox")

    result = await _call("checkpoint", comp_path=comp_path, file_path=file_path)
    if not result.get("ok"):
        return result

    entry = {
        "id": cp_id,
        "label": label,
        "comp_path": comp_path,
        "file_path": file_path,
        "timestamp": time.time(),
    }
    _checkpoints.append(entry)

    # FIFO eviction: drop oldest, also remove its .tox from disk
    while len(_checkpoints) > MAX_CHECKPOINTS:
        old = _checkpoints.pop(0)
        try:
            Path(old["file_path"]).unlink()
        except FileNotFoundError:
            pass

    return {"ok": True, **entry}


@mcp.tool()
async def td_rollback(checkpoint_id: str) -> dict:
    """Restore a previously checkpointed COMP from its .tox snapshot.

    The current COMP at the checkpointed path is destroyed and re-imported
    from disk. Name and node position are preserved. External wires are lost.
    """
    entry = next((c for c in _checkpoints if c["id"] == checkpoint_id), None)
    if entry is None:
        return {"ok": False, "error": f"Unknown checkpoint id: {checkpoint_id}"}
    return await _call(
        "rollback", comp_path=entry["comp_path"], file_path=entry["file_path"]
    )


@mcp.tool()
async def td_list_checkpoints() -> dict:
    """List active checkpoints in this MCP session (newest last)."""
    return {"ok": True, "count": len(_checkpoints), "checkpoints": _checkpoints}
