import asyncio
import base64
import json
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, get_args

from mcp.server.fastmcp import FastMCP, Image
from pydantic import ValidationError

from td_mcp import __version__
from td_mcp.bridge import bridge
from td_mcp.ingest.vj_loops import ingest_corpus as ingest_vj_corpus
from td_mcp.kb.cinematic import get_cinematic_kb
from td_mcp.kb.vj_loops import get_vj_loops_kb
from td_mcp.kb.glsl import get_glsl_kb
from td_mcp.kb.operators import OperatorEntry, OperatorsCatalog, get_catalog, reload_catalog
from td_mcp.kb.pop_patterns import get_pop_kb
from td_mcp.kb.top_patterns import get_top_kb
from td_mcp.kb.vector import ChunkSource, build_seed_chunks, get_vector_kb
from td_mcp.protocol import AnnotationSpec, LayoutDiff, OperatorPosition, OperatorRename, TDError
from td_mcp.tools.layout import (
    assign_columns_by_depth,
    detect_clusters,
    geometric_layout,
    propose_rename,
)
from td_mcp.tools.palette import filter_palette, resolve_tox, scan_palette

mcp = FastMCP("td-mcp")

VALID_FAMILIES = ("CHOP", "TOP", "SOP", "DAT", "COMP", "MAT", "POP")
VALID_SOURCES = get_args(ChunkSource)

# Per-process checkpoint registry. FIFO at MAX_CHECKPOINTS. .tox files survive
# server restarts on disk; the in-memory list does not — restart-safe listing
# would require a manifest file, deferred until cross-session resume matters.
_checkpoints: list[dict] = []
MAX_CHECKPOINTS = 20
_project_folder_cache: str | None = None

# Session plan registered via td_plan. The soft gate in td_create_op nudges
# agents to ground builds in the KB before mutating the project — prompt-only
# protocols get rationalized away; a mechanical warning does not.
_session_plan: dict | None = None
_plan_gaps_warned: bool = False


async def _get_project_folder() -> str:
    global _project_folder_cache
    if _project_folder_cache is None:
        result = await bridge.send("get_project_folder")
        _project_folder_cache = result["folder"]
    return _project_folder_cache


async def _call(action: str, **data: Any) -> dict:
    """Bridge call wrapper. Returns {ok: True, **result} or {ok: False, error}.

    When the bridge's rolling latency says TD's cook thread is starving
    (sustained slow roundtrips), every response carries a
    cook_pressure_warning — the mechanical version of the cook-budget
    protocol, before calls start timing out entirely."""
    try:
        result = await bridge.send(action, {k: v for k, v in data.items() if v is not None})
        response = {"ok": True, **result}
    except TDError as e:
        response = {"ok": False, "error": e.message}
    pressure = bridge.cook_pressure()
    if pressure:
        response["cook_pressure_warning"] = {
            **pressure,
            "hint": (
                "TD's cook thread is starving the bridge (median roundtrip "
                f"{pressure['median_ms']}ms). Run td_perf to find the heavy "
                "ops, disable/downscale them or pause the timeline — do NOT "
                "keep hammering calls into a wedging graph."
            ),
        }
    return response


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
    on the matching port (default 9988). Auth: unless an explicit `token`
    is given, the same-machine token file (~/.cache/td-mcp/bridge_token)
    is created/read and sent with every message — the TD-side callbacks
    require it once the file exists.
    """
    from td_mcp.util import get_bridge_token

    global _project_folder_cache
    try:
        await bridge.connect(url, token=token or get_bridge_token(), timeout=timeout)
        # New connection may be a different project (or the same project
        # reopened elsewhere) — a stale folder would send checkpoints to
        # the old project's directory.
        _project_folder_cache = None
        sync = await _sync_bridge_script()
        return {"ok": True, "url": url, "connected": True, "bridge_sync": sync}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Source checkout first, then the copy hatch force-includes into the wheel
# (see pyproject) so bridge sync also works on pip-installed servers.
_BRIDGE_SCRIPT_CANDIDATES = [
    Path(__file__).parent.parent / "td_bridge_tox" / "webserver_callbacks.py",
    Path(__file__).parent / "_bridge" / "webserver_callbacks.py",
]


def _bridge_script() -> tuple[str, str, str] | None:
    """(text, sha256, path) of the bridge script, or None if no copy is
    found (neither checkout nor package data)."""
    import hashlib

    for path in _BRIDGE_SCRIPT_CANDIDATES:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            return text, hashlib.sha256(text.strip().encode("utf-8")).hexdigest(), str(path)
    return None


async def _sync_bridge_script() -> dict:
    """Detect and repair bridge-script drift right after connecting.

    A TD project reload silently reverts the callbacks DAT to its saved
    version — every symptom downstream (missing actions, legacy payload
    shapes) looks like a plain bug. Compare hashes and, on drift, have TD
    reload the DAT text from the repo file (same machine by design —
    localhost trust model). run_script is used for the repair so it also
    works on old bridges that don't know the bridge_version action.
    """
    local = _bridge_script()
    if local is None:
        return {"status": "skipped", "reason": "bridge script not found (wheel install?)"}
    _text, local_hash, script_path = local

    try:
        remote = await _call("bridge_version")
        if remote.get("script_hash") == local_hash:
            return {"status": "synced"}
    except Exception:
        pass  # old bridge without the action — repair below

    # encoding='utf-8' on BOTH file ops: TD's embedded Python defaults to
    # ASCII, and the script contains em-dashes — the backup write crashed
    # the whole repair the first time a non-ASCII script shipped.
    backup = str(Path(tempfile.gettempdir()) / "td_mcp_bridge_backup.py")
    repair = (
        "from pathlib import Path\n"
        "_updated = []\n"
        "for _ws in root.findChildren(type=webserverDAT, maxDepth=10):\n"
        "    _cb = _ws.par.callbacks.eval()\n"
        "    if _cb and 'def onWebSocketReceiveText' in _cb.text:\n"
        f"        Path({backup!r}).write_text(_cb.text, encoding='utf-8')\n"
        f"        _cb.text = Path({script_path!r}).read_text(encoding='utf-8')\n"
        "        _updated.append(_cb.path)\n"
        "print(','.join(_updated))\n"
    )
    try:
        result = await _call("run_script", code=repair)
        if not result.get("ok"):
            return {"status": "failed", "reason": result.get("error", "repair script failed")}
        updated = (result.get("output") or "").strip()
        if not updated:
            return {"status": "failed", "reason": "no callbacks DAT found to update"}
        verify = await _call("bridge_version")
        if verify.get("script_hash") == local_hash:
            return {"status": "updated", "dat": updated, "backup": backup}
        return {"status": "failed", "reason": "hash mismatch after update"}
    except Exception as e:
        return {"status": "failed", "reason": str(e)}


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
async def td_plan(
    intention: str,
    stages: list[dict],
    gaps: list[str] | None = None,
    success_criteria: str = "",
) -> dict:
    """Register the build plan for this session BEFORE creating operators.

    Call this after decomposing the request and searching the KB, before the
    first td_create_op. Until a plan is registered, every td_create_op
    response carries a warning (hard refusal with TD_MCP_REQUIRE_PLAN=1).

    `intention`: one sentence — what is being built and for whom.
    `stages`: one entry per build stage, each a dict like
        {"name": "sim loop", "ops": ["particlePOP", "forceradialPOP"],
         "kb_source": "ytv_4bIQXKJaWlA_04", "confidence": "vision-chunk"}
      confidence tiers: curated-pattern > vision-chunk > wiki > transcript
      > improvised.
    `gaps`: what the KB did NOT tell you. A gap is NOT a license to
      improvise — resolve it by escalating retrieval (kb_get_tutorial, raw
      transcript, wiki, web) BEFORE building the stage that depends on it.
      Re-call td_plan with the updated stages once resolved.
    `success_criteria`: what the final snapshot must show to call it done.

    Re-calling replaces the previous plan (normal when scope evolves —
    silent drift is the thing to avoid, not revision).
    """
    global _session_plan, _plan_gaps_warned
    if not intention.strip():
        return {"ok": False, "error": "intention is required"}
    if not stages:
        return {"ok": False, "error": "at least one stage is required"}
    _session_plan = {
        "intention": intention,
        "stages": stages,
        "gaps": gaps or [],
        "success_criteria": success_criteria,
        "registered_at": time.time(),
    }
    _plan_gaps_warned = False
    response = {"ok": True, "registered": True, "stage_count": len(stages)}
    improvised = [
        s.get("name", f"stage {i}")
        for i, s in enumerate(stages)
        if "improvis" in str(s.get("confidence", "")).lower()
    ]
    if gaps:
        response["gap_protocol"] = (
            f"{len(gaps)} unresolved gap(s). Escalate retrieval NOW — "
            "kb_get_tutorial for the full video, raw transcript over vision "
            "on conflicts, wiki, then web — and re-register the plan. "
            "Building through a gap costs a full build-diagnose-rebuild cycle."
        )
    if improvised:
        response["improvised_stages"] = improvised
        response["improvised_note"] = (
            "These stages rest on improvisation — flag them to the user "
            "BEFORE building, and validate them first in the visual loop."
        )
    return response


def _plan_gate() -> dict:
    """Warning fields to merge into td_create_op responses (soft gate).

    Pure computation — the caller flips the one-shot flag via
    _mark_gap_warning_delivered() ONLY when the warning is actually
    attached to a response, otherwise an early return (catalog rejection)
    burns the flag and the warning is never seen."""
    if _session_plan is None:
        return {
            "plan_warning": (
                "No plan registered for this session. Call td_plan first "
                "(intention + KB-grounded stages + gaps). Set "
                "TD_MCP_REQUIRE_PLAN=1 to make this a hard error."
            )
        }
    if _session_plan["gaps"] and not _plan_gaps_warned:
        return {
            "plan_warning": (
                f"Plan has {len(_session_plan['gaps'])} unresolved gap(s): "
                f"{_session_plan['gaps']}. Resolve via retrieval before "
                "building the dependent stages."
            )
        }
    return {}


def _mark_gap_warning_delivered() -> None:
    global _plan_gaps_warned
    if _session_plan is not None and _session_plan["gaps"]:
        _plan_gaps_warned = True


def _require_plan() -> bool:
    import os

    return os.environ.get("TD_MCP_REQUIRE_PLAN", "").strip().lower() in ("1", "true", "yes")


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
    KB-validated: unknown op_type returns {ok: false} with close-match
    suggestions instead of hitting TD. Use kb_list_operators or
    kb_get_operator to browse the catalog.

    Expects a session plan registered via td_plan — creates without one
    succeed but carry a `plan_warning` (hard error if TD_MCP_REQUIRE_PLAN=1).
    """
    gate = _plan_gate()
    if gate and _session_plan is None and _require_plan():
        return {"ok": False, "error": gate["plan_warning"]}

    catalog = get_catalog()
    if not catalog.is_empty:
        if catalog.get(op_type) is None:
            if gate:
                _mark_gap_warning_delivered()
            return {
                "ok": False,
                "error": f"Unknown operator class: {op_type!r}",
                "suggestions": catalog.suggest(op_type, n=5),
                "hint": (
                    "Use kb_list_operators(family=...) to browse, or "
                    "kb_refresh_operators_catalog if the catalog is stale."
                ),
                **gate,
            }
    result = await _call("create_op", type=op_type, parent=parent, name=name, x=x, y=y)
    if gate:
        _mark_gap_warning_delivered()
        return {**result, **gate}
    return result


@mcp.tool()
async def td_delete_op(path: str) -> dict:
    """Destroy an operator by absolute path. Irreversible — no undo."""
    return await _call("delete_op", path=path)


# Palette roots don't move during a TD run; fetched once per server process.
_palette_roots_cache: dict[str, str] | None = None


async def _palette_roots() -> dict[str, str] | None:
    """Palette folder paths from the live TD, or None when the bridge is
    down (callers turn that into a 'call td_connect first' error)."""
    global _palette_roots_cache
    if _palette_roots_cache is None:
        probe = await _call(
            "eval",
            expression="__import__('json').dumps("
                       "{'builtin': app.paletteFolder, 'user': app.userPaletteFolder})",
        )
        if not probe.get("ok") or "value" not in probe:
            return None
        _palette_roots_cache = json.loads(probe["value"])
    return _palette_roots_cache


_NO_BRIDGE = {"ok": False,
              "error": "Bridge not connected — call td_connect first."}


def _scan_palettes(roots: dict[str, str], source: str) -> list[dict]:
    entries: list[dict] = []
    for src, root in roots.items():
        if source in ("all", src):
            entries.extend(scan_palette(root, src))
    return entries


@mcp.tool()
async def td_palette_list(query: str = "", source: str = "all",
                          limit: int = 60, offset: int = 0) -> dict:
    """Browse installable .tox components: TD's built-in Palette
    (app.paletteFolder — Tools, UI, Techniques, POPs, ...) AND the user's
    own palette (app.userPaletteFolder — RayTK, downloaded packs, saved
    COMPs...).

    `query` substring-filters name and relative path (e.g. 'blur',
    'Tools/', 'raytk'). `source` is 'all' | 'builtin' | 'user'. Instantiate
    a result with td_palette_load. Before building a common utility from
    scratch (movie player, kinect pipeline, LUT, mixer...), check whether
    the Palette already ships it.
    """
    if source not in ("all", "builtin", "user"):
        return {"ok": False, "error": f"unknown source '{source}' (all|builtin|user)"}
    roots = await _palette_roots()
    if roots is None:
        return dict(_NO_BRIDGE)
    entries = filter_palette(_scan_palettes(roots, source), query)
    return {
        "ok": True,
        "total": len(entries),
        "entries": entries[offset:offset + limit],
        "roots": roots,
        **({"note": f"{len(entries) - offset - limit} more — raise offset/limit or narrow the query."}
           if len(entries) > offset + limit else {}),
    }


@mcp.tool()
async def td_palette_load(
    tox: str,
    parent: str = "/project1",
    name: str = "",
    x: int = 0,
    y: int = 0,
) -> dict:
    """Instantiate a Palette component (built-in or user) inside a parent
    COMP via loadTox.

    `tox` accepts a bare name ('moviePlayer'), a relative path
    ('Tools/moviePlayer.tox'), or a source-qualified form
    ('user:Grid', 'builtin:Tools/moviePlayer') when the same name exists
    in both palettes. Unknown or ambiguous identifiers return close-match
    suggestions instead of hitting TD.

    Derivative palette .tox files wrap the real component one level down
    (icon + inner COMP); the bridge extracts the inner component
    automatically, mirroring native palette drag & drop (`extracted: true`
    in the result).

    Palette COMPs can be heavy (UI panels, engines, GPU pipelines) — after
    loading, td_op_info the result and snapshot before wiring it into the
    main graph, per the cook budget protocol.
    """
    gate = _plan_gate()
    if gate and _session_plan is None and _require_plan():
        return {"ok": False, "error": gate["plan_warning"]}

    roots = await _palette_roots()
    if roots is None:
        return dict(_NO_BRIDGE)
    entries = _scan_palettes(roots, "all")
    entry, suggestions = resolve_tox(tox, entries)
    if entry is None:
        if gate:
            _mark_gap_warning_delivered()
        return {
            "ok": False,
            "error": f"No unique palette component matches {tox!r}",
            "suggestions": suggestions,
            "hint": "td_palette_list(query=...) to browse; qualify with "
                    "'user:' or 'builtin:' on name collisions.",
            **gate,
        }
    file = str(Path(roots[entry["source"]]) / entry["relpath"])
    result = await _call("load_tox", parent=parent, file=file, name=name, x=x, y=y)
    if result.get("ok"):
        result["source"] = entry["source"]
        result["relpath"] = entry["relpath"]
    if gate:
        _mark_gap_warning_delivered()
        return {**result, **gate}
    return result


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
    NOT the display label. Case sensitive. On an unknown param, the error
    carries close-match suggestions from the enriched operators catalog.
    """
    result = await _call("set_param", path=path, param=param, value=value)
    if not result.get("ok"):
        # One extra roundtrip only on the failure path: resolve the op's
        # class so the catalog can suggest what the caller probably meant.
        try:
            probe = await _call("eval", expression=f"type(op({path!r})).__name__")
            op_class = str(probe.get("value", "")).strip()
            suggestions = get_catalog().suggest_params(op_class, param)
            if suggestions:
                result["suggestions"] = suggestions
                result["hint"] = f"Param names on {op_class} close to {param!r}."
        except Exception:
            pass
    return result


@mcp.tool()
async def td_pulse(path: str, param: str) -> dict:
    """Trigger a pulse-type parameter (e.g. a 'Reset' button)."""
    return await _call("pulse", path=path, param=param)


@mcp.tool()
async def td_set_flags(
    path: str,
    display: bool | None = None,
    render: bool | None = None,
    bypass: bool | None = None,
    viewer: bool | None = None,
    lock: bool | None = None,
) -> dict:
    """Set operator FLAGS (not parameters): display, render, bypass, viewer,
    lock. Only the flags you pass are touched.

    Key use: an op inside a geoCOMP needs display+render on to be drawn by
    a renderTOP — geoCOMPs take no wired data inputs, so the idiom is a
    selectPOP/inSOP inside the geo pointing at the source, flagged here.
    """
    return await _call("set_flags", path=path, display=display, render=render,
                       bypass=bypass, viewer=viewer, lock=lock)


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


@mcp.tool()
async def td_perf(path: str = "/", top: int = 15) -> dict:
    """Report the heaviest operators by last measured cook time (CPU + GPU
    ms, per op, sorted). Run this BEFORE the graph wedges the bridge — TD's
    WebServer DAT shares the main cook thread, so a heavy graph starves
    every bridge call.

    Returns `frame_budget_ms` (1000/cookRate) for context and, when any op
    eats more than half the budget, a `budget_eaters` list — those are the
    ops to disable, downscale, or isolate first (environmentlightCOMP IBL,
    full-res renders, big blurs are the usual suspects).
    """
    result = await _call("perf_report", path=path, top=top)
    if not result.get("ok"):
        return result
    budget = result.get("frame_budget_ms") or 0.0
    if budget:
        eaters = [
            r["path"] for r in result.get("top", [])
            if r["cpu_ms"] + r["gpu_ms"] > budget * 0.5
        ]
        if eaters:
            result["budget_eaters"] = eaters
            result["hint"] = (
                f"These ops each eat >50% of the {budget:.1f}ms frame budget. "
                "Disable cooking on what you are not judging, iterate at "
                "smaller resolution, or isolate the scene in its own COMP."
            )
    return result


# ─────────────────────────── visual feedback ───────────────────────────────


@mcp.tool()
async def td_snapshot(op_path: str, max_size: int = 0) -> Image:
    """Force-cook a TOP and return its current frame as a PNG.

    Returns an MCP Image (visible directly to the agent). For the vibe loop,
    this closes the feedback cycle: mutate → snapshot → judge → iterate.
    `max_size` > 0 downscales so the longest side fits (e.g. 512) — smaller
    context cost while iterating; leave 0 for the native resolution.

    Raises TDError if the bridge is disconnected or the path is not a TOP.
    """
    result = await bridge.send("snapshot", {"op": op_path})
    png_bytes = base64.b64decode(result["base64"])
    if max_size > 0:
        from td_mcp.tools.visual import downscale_png

        png_bytes = downscale_png(png_bytes, max_size)
    return Image(data=png_bytes, format="png")


@mcp.tool()
async def td_visual_diff(op_path: str, reference_path: str) -> dict:
    """Compare a TOP's current frame against a reference image on disk.

    The vibe loop, quantified: returns `similarity` (0-1, pixel-based),
    signed deltas (luminance, contrast, RGB — current minus reference),
    verbal `notes` describing the biggest gaps ("current is darker",
    "bottom-left zone much brighter"), and `clip_similarity` when the [vj]
    extra is installed (semantic, robust to layout shifts).

    Iterate mutate → td_visual_diff → adjust until similarity stops
    improving, then judge the final frame with td_snapshot.
    """
    from td_mcp.tools.visual import compare_images

    ref = Path(reference_path)
    if not ref.exists():
        return {"ok": False, "error": f"reference image not found: {reference_path}"}
    try:
        result = await bridge.send("snapshot", {"op": op_path})
    except TDError as e:
        return {"ok": False, "error": e.message}
    png_bytes = base64.b64decode(result["base64"])
    metrics = await asyncio.to_thread(compare_images, png_bytes, ref.read_bytes())
    return {"ok": True, "op": op_path, "reference": str(ref), **metrics}


# ─────────────────────────── checkpoint / rollback ─────────────────────────


async def _take_checkpoint(comp_path: str, label: str) -> dict:
    """Comp-scoped .tox checkpoint, registered in _checkpoints so
    td_rollback can restore it. Shared by td_checkpoint and
    td_layout_network."""
    folder = Path(await _get_project_folder())
    cp_dir = folder / ".td_mcp_snapshots"
    cp_dir.mkdir(parents=True, exist_ok=True)
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
    return await _take_checkpoint(comp_path, label)


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


# ─────────────────────────── knowledge base (Phase 3) ──────────────────────


@mcp.tool()
async def kb_list_operators(family: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    """List operators known to the catalog, optionally filtered by family.

    family ∈ {CHOP, TOP, SOP, POP, DAT, COMP, MAT}. limit caps the response
    size, offset paginates (next page: offset += returned); full counts are
    always reported in `total` and `total_in_family`.
    """
    if family is not None:
        family = family.upper()
        if family not in VALID_FAMILIES:
            return {
                "ok": False,
                "error": f"Unknown family: {family!r}",
                "valid_families": list(VALID_FAMILIES),
            }
    catalog = get_catalog()
    if catalog.is_empty:
        return {
            "ok": False,
            "error": "Catalog is empty. Run kb_refresh_operators_catalog with TD connected.",
        }
    entries = catalog.list(family) if family else catalog.list()
    page = entries[offset:offset + limit]
    return {
        "ok": True,
        "total": catalog.count,
        "by_family": catalog.family_counts(),
        "filter_family": family,
        "total_in_family": len(entries),
        "offset": offset,
        "returned": len(page),
        "has_more": offset + len(page) < len(entries),
        "operators": [{"python_class": e.python_class, "family": e.family} for e in page],
    }


@mcp.tool()
async def kb_get_operator(query: str, include_params: bool = True) -> dict:
    """Lookup an operator by python_class name. If not found, returns suggestions.

    With an enriched catalog (kb_refresh_operators_catalog include_params=True),
    the entry carries every settable parameter: internal name (what
    td_set_param wants), display label (what tutorials say aloud), style,
    and menu tokens. Use this INSTEAD of creating the op + td_op_info just
    to discover param names.
    """
    catalog = get_catalog()
    if catalog.is_empty:
        return {"ok": False, "error": "Catalog is empty."}
    entry = catalog.get(query)
    if entry is not None:
        dump = entry.model_dump(exclude_defaults=True)
        dump.setdefault("family", entry.family)
        dump.setdefault("python_class", entry.python_class)
        if not include_params:
            dump.pop("params", None)
        if include_params and not entry.params:
            dump["params_note"] = (
                "Catalog predates param enrichment — run "
                "kb_refresh_operators_catalog with TD connected."
            )
        return {"ok": True, "found": True, **dump}
    return {
        "ok": True,
        "found": False,
        "query": query,
        "suggestions": catalog.suggest(query, n=5),
    }


@mcp.tool()
async def kb_refresh_operators_catalog(include_params: bool = True) -> dict:
    """Introspect the connected TD instance, regenerate the operators catalog,
    and persist it to td_mcp/kb/data/operators.json.

    Requires an active bridge. Uses the convention that creatable op classes
    start with a lowercase letter — abstract bases (ObjectCOMP, PanelCOMP, ...)
    are filtered out.

    include_params (default True) instantiates each class once inside a
    scratch COMP to capture its parameter schema (name, label, style, menu
    tokens) — this is what lets kb_get_operator answer param questions and
    td_set_param suggest fixes without live roundtrips. Takes ~10-60s and
    briefly creates/destroys one op per class; the scratch COMP is removed
    even on failure. Pass include_params=False for the fast name-only pass.

    The result is written to a temp file TD-side and read back from disk
    (same-machine trust model, like checkpoints) — the full param schema is
    too large to push through the WS bridge comfortably.
    """
    introspect = f"""
import json, tempfile, os
INCLUDE_PARAMS = {include_params!r}
suffixes = ['CHOP', 'TOP', 'SOP', 'POP', 'DAT', 'COMP', 'MAT']
ops = []
scratch = None
if INCLUDE_PARAMS:
    scratch = root.create(baseCOMP, 'td_mcp_introspect_tmp')
try:
    for clsname in dir(td):
        if clsname.startswith('_') or not clsname[0].islower():
            continue
        for suffix in suffixes:
            if clsname.endswith(suffix) and clsname != suffix and len(clsname) > len(suffix):
                entry = {{
                    'python_class': clsname,
                    'family': suffix,
                    'subtype': clsname[:-len(suffix)],
                }}
                if INCLUDE_PARAMS:
                    try:
                        inst = scratch.create(getattr(td, clsname))
                        params = []
                        for p in inst.pars():
                            d = {{'name': p.name, 'label': p.label or '', 'style': p.style or ''}}
                            try:
                                menu = list(p.menuNames)
                                if menu:
                                    d['menu_names'] = menu
                            except Exception:
                                pass
                            params.append(d)
                        entry['params'] = params
                        inst.destroy()
                    except Exception:
                        pass  # some classes refuse creation (license, context)
                ops.append(entry)
                break
finally:
    if scratch is not None:
        scratch.destroy()
fd, path = tempfile.mkstemp(suffix='.json', prefix='td_mcp_catalog_')
with os.fdopen(fd, 'w') as f:
    json.dump({{'build': getattr(app, 'build', ''), 'operators': ops}}, f)
print(path)
"""
    # Instantiating ~700 op classes outruns the 5s wedge guard by design.
    result = await bridge.send("run_script", {"code": introspect}, timeout=180.0)
    raw = result.get("output", "").strip()
    tmp_path = Path(raw.splitlines()[-1]) if raw else None
    if tmp_path is None or not tmp_path.exists():
        return {"ok": False, "error": "introspection did not return a readable temp file", "raw": raw[:500]}
    try:
        data = json.loads(tmp_path.read_text())
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Parse error: {e}"}
    finally:
        tmp_path.unlink(missing_ok=True)

    entries, skipped = [], []
    for e in data["operators"]:
        try:
            entries.append(OperatorEntry.model_validate(e))
        except ValidationError as exc:
            # One malformed op must not kill the whole refresh.
            skipped.append({"python_class": e.get("python_class", "?"),
                            "error": str(exc).splitlines()[0]})
    entries.sort(key=lambda e: (e.family, e.python_class))
    catalog = OperatorsCatalog(entries, td_build=data.get("build", ""))
    catalog.save()
    reload_catalog()
    with_params = sum(1 for e in entries if e.params)
    return {
        "ok": True,
        "count": len(entries),
        "with_params": with_params,
        "td_build": data.get("build", ""),
        "by_family": catalog.family_counts(),
        **({"skipped": skipped} if skipped else {}),
        **({"next_step": "kb_index_update folds enriched op chunks into the vector index."}
           if with_params else {}),
    }


@mcp.tool()
async def kb_glsl_template(template_id: str = "") -> dict:
    """Return a vetted GLSL TOP skeleton — or the full index if template_id is empty.

    Without template_id: returns the catalog of available templates plus the
    full uniforms reference and antipatterns list. Use this first to discover
    what's available.

    With template_id: returns the template's code + which uniforms it uses +
    the antipatterns relevant to this shader type. Copy the code into the
    GLSL TOP's pixeldat/computedat target DAT.
    """
    kb = get_glsl_kb()
    if not template_id:
        return {
            "ok": True,
            "templates": kb.index(),
            "uniforms_reference": [u.model_dump() for u in kb.uniforms],
            "antipatterns": [a.model_dump() for a in kb.antipatterns],
        }
    tpl = kb.get(template_id)
    if tpl is None:
        return {
            "ok": False,
            "error": f"Unknown template id: {template_id!r}",
            "available": [t.id for t in kb.templates],
        }
    # Surface antipatterns that match this shader type
    relevant = [
        a.model_dump()
        for a in kb.antipatterns
        if (tpl.shader_type == "compute" and a.id.startswith("compute_"))
        or (tpl.shader_type != "compute" and not a.id.startswith("compute_"))
    ]
    return {
        "ok": True,
        "template": tpl.model_dump(),
        "applicable_antipatterns": relevant,
    }


@mcp.tool()
async def kb_pop_pattern(pattern_id: str = "", tag: str = "") -> dict:
    """Curated POP workflow recipes. Phase 3.6 scaffold — small seed library
    that the user enriches over time as they encounter real POP needs.

    No args: returns the index of available patterns.
    pattern_id: returns the full pattern (ops + connections + notes + pitfalls).
    tag: returns all patterns matching the tag.

    Each pattern's ops/connections use LOCAL names — when executing, create
    a wrapper baseCOMP first and resolve each `name` to <wrapper>/<name>.
    Patterns are verified live against the build noted in `verified_on_build`.
    """
    kb = get_pop_kb()
    if pattern_id:
        pat = kb.get(pattern_id)
        if pat is None:
            return {
                "ok": False,
                "error": f"Unknown pattern id: {pattern_id!r}",
                "available": [p.id for p in kb.patterns],
            }
        return {"ok": True, "pattern": pat.model_dump()}
    if tag:
        matches = kb.by_tag(tag)
        return {
            "ok": True,
            "tag": tag,
            "count": len(matches),
            "patterns": [p.model_dump() for p in matches],
        }
    return {
        "ok": True,
        "total": len(kb.patterns),
        "index": kb.index(),
        "note": "Phase 3.6 scaffold — author new patterns by editing td_mcp/kb/data/pop_patterns.json.",
    }


# ─────────────────────────── vector KB (Phase 4) ───────────────────────────


@mcp.tool()
async def kb_search(
    query: str,
    k: int = 10,
    source: str | None = None,
    family: str | None = None,
    is_glsl: bool | None = None,
    rerank: bool | None = None,
) -> dict:
    """Semantic search across the indexed KB corpus (operators + GLSL templates + POP patterns).

    Returns top-k chunks, with optional pre-filtering by
    source ∈ {operators, glsl_template, pop_pattern, ...},
    family ∈ {CHOP, TOP, SOP, POP, ...}, or is_glsl.

    Ranked by vector similarity. `rerank=True` adds a cross-encoder second
    stage (bge-reranker-v2-m3) that rescores each (query, chunk) pair and
    keeps the best k — worth trying when vector order looks off, but OFF by
    default: measured on this corpus it cut practical tutorial chunks in the
    top-3 from 10/30 to 7/30 and cost 47ms -> 1694ms per search, because it
    favours encyclopedic wiki passages over conversational tutorial
    transcripts. TD_MCP_RERANK=1 enables it globally. Results carry
    `_distance`, plus `_rerank_score` when reranked.

    Requires the vector index to be built — run kb_reindex first if you get
    an empty result. The embedding model loads lazily on first call (BGE-M3
    default ~2GB download once; override via TD_MCP_EMBEDDING_MODEL).
    """
    if source is not None and source not in VALID_SOURCES:
        return {
            "ok": False,
            "error": f"Unknown source: {source!r}",
            "valid_sources": list(VALID_SOURCES),
        }
    if family is not None:
        family = family.upper()
        if family not in VALID_FAMILIES:
            return {
                "ok": False,
                "error": f"Unknown family: {family!r}",
                "valid_families": list(VALID_FAMILIES),
            }
    kb = get_vector_kb()
    if not kb.has_index():
        return {
            "ok": False,
            "error": "Vector index is empty. Run kb_reindex to build it from the seed sources.",
        }
    # to_thread: query embedding is seconds of synchronous CPU — running it
    # on the event loop freezes every other tool AND the bridge reader.
    results = await asyncio.to_thread(
        kb.search,
        query,
        k=k,
        source=source,
        family=family,
        is_glsl=is_glsl,
        rerank=rerank,
    )
    return {
        "ok": True,
        "query": query,
        "count": len(results),
        "results": results,
    }


@mcp.tool()
async def kb_get_tutorial(video_id: str = "", query: str = "") -> dict:
    """Fetch EVERY indexed chunk of one tutorial video, ordered by segment —
    the deterministic complement to kb_search.

    Use this whenever the task is "reproduce this tutorial": semantic search
    returns the best-matching segments, not all of them, and one missing
    segment silently breaks a step-by-step rebuild. Returns two ordered
    lists: `transcript` (raw whisper text, coarse 4-way split — the ground
    truth for spoken parameter values) and `vision` (per-segment technique
    extractions with operators/params/connections — richer but occasionally
    misreads the params panel). On conflict, trust the transcript.

    `video_id`: the 11-char YouTube id (e.g. '4bIQXKJaWlA').
    `query`: if you don't know the id, a natural-language lookup — returns
    candidate videos (id + title) to recall this tool with.
    """
    import re

    kb = get_vector_kb()
    if not kb.has_index():
        return {"ok": False, "error": "Vector index is empty. Run kb_reindex first."}

    if video_id:
        result = kb.get_video_chunks(video_id)
        found = bool(result["transcript"] or result["vision"])
        return {
            "ok": True,
            "found": found,
            **result,
            **({} if found else {"hint": "No chunks for this id — check kb_youtube_status, or lookup by query."}),
        }

    if query:
        hits = await asyncio.to_thread(kb.search, query, k=15, source="tutorial")
        candidates: dict[str, dict] = {}
        for h in hits:
            m = re.match(r"^(ytv?)_(.+)_(\d+)$", h["id"])
            if m is None:
                continue
            vid = m.group(2)
            entry = candidates.setdefault(vid, {"video_id": vid, "title": "", "hits": 0})
            entry["hits"] += 1
            if not entry["title"]:
                entry["title"] = h["title"]
        return {
            "ok": True,
            "query": query,
            "candidates": sorted(candidates.values(), key=lambda c: -c["hits"]),
            "hint": "Recall kb_get_tutorial with the matching video_id for the full chunk set.",
        }

    return {"ok": False, "error": "Provide video_id or query."}


@mcp.tool()
async def kb_reindex() -> dict:
    """Rebuild the vector index from all structured KBs (operators + GLSL
    templates + POP patterns). Drops the existing index. Triggers the
    embedding model download on first run.
    """
    kb = get_vector_kb()
    chunks = build_seed_chunks()
    if not chunks:
        return {"ok": False, "error": "No chunks to index — KBs appear empty."}
    # to_thread: ~15 min of synchronous embedding would otherwise freeze
    # the whole MCP server, including the TD bridge reader.
    result = await asyncio.to_thread(kb.reindex, chunks)
    return {"ok": True, **result}


@mcp.tool()
async def kb_index_update() -> dict:
    """Incrementally fold new/changed chunks into the vector index (upsert).
    Embeds ONLY what changed — seconds instead of the ~15 min full reindex.
    Use after any ingestion (wiki, youtube, vision) instead of kb_reindex;
    kb_reindex remains for full rebuilds (model change, corrupted index).
    """
    kb = get_vector_kb()
    chunks = build_seed_chunks()
    if not chunks:
        return {"ok": False, "error": "No chunks to index — KBs appear empty."}
    return {"ok": True, **(await asyncio.to_thread(kb.upsert, chunks))}


@mcp.tool()
async def kb_ingest_wiki_full(limit: int = 300) -> dict:
    """Fetch the COMPLETE docs.derivative.ca wiki (~2100 articles: operators,
    Python classes, concepts, guides) into the local cache — not just the
    operator pages. Polite (1 req/sec): `limit` bounds NEW fetches per call
    (default 300 ≈ 5 min); already-cached pages are free. Re-run until
    `remaining` hits 0, then call kb_index_update.
    """
    from td_mcp.ingest.wiki import WikiClient, ingest_all_pages

    with WikiClient() as client:
        report = ingest_all_pages(client, limit=limit)
    return {
        "ok": True,
        **report,
        "next_step": (
            "Re-run until remaining=0, then kb_index_update to fold chunks in."
            if report.get("remaining", 0) > 0
            else "Call kb_index_update to fold the wiki chunks into the index."
        ),
    }


@mcp.tool()
async def td_compile_technique(
    video_id: str,
    segment_index: int,
    parent: str = "/project1",
) -> dict:
    """Build a vision-extracted technique (techniques.json segment) in the
    live TD project — the curation loop, industrialized. Creates a wrapper
    baseCOMP `compile_<video>_<seg>`, instantiates the catalog-validated
    operators, applies parameters by label matching, wires connections, and
    reads back the params TD actually holds ("verified_ops").

    Everything unmatched/unresolved is REPORTED, never guessed. After a
    visual check (td_snapshot on a TOP, or wire the POP chain into a geo),
    promote the verified result with kb_promote_pop_pattern.
    """
    import json as _json

    from td_mcp.ingest.youtube import DEFAULT_CACHE_DIR
    from td_mcp.tools.technique_compiler import build_plan, compile_script

    tech_path = None
    for channel_dir in DEFAULT_CACHE_DIR.iterdir() if DEFAULT_CACHE_DIR.exists() else []:
        cand = channel_dir / video_id / "techniques.json"
        if cand.exists():
            tech_path = cand
            break
    if tech_path is None:
        return {"ok": False, "error": f"no techniques.json for video {video_id!r} in cache"}

    seg = _json.loads(tech_path.read_text()).get("segments", {}).get(str(segment_index))
    if seg is None or seg.get("status") != "ok":
        return {"ok": False, "error": f"segment {segment_index} missing or not status=ok"}
    extraction = seg.get("extraction", {})

    catalog = get_catalog()
    catalog_classes = {e.python_class for e in catalog.list()}
    plan = build_plan(extraction, catalog_classes)
    if not plan.creates:
        return {"ok": False, "error": "no catalog-validated operators in this segment",
                "unresolved": plan.unresolved_instances}

    comp_name = f"compile_{video_id.replace('-', '_')}_{segment_index:02d}"
    script = compile_script(plan, parent, comp_name)
    result = await _call("run_script", code=script)
    try:
        report = _json.loads(result.get("output", "").strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "error": "could not parse build report", "raw": result}

    return {
        "ok": True,
        "comp_path": f"{parent}/{comp_name}",
        "technique": extraction.get("technique", ""),
        "source_segment": {"video_id": video_id, "segment": segment_index,
                           "start": seg.get("start"), "end": seg.get("end")},
        "plan": {
            "unresolved_instances": plan.unresolved_instances,
            "dropped_connections": plan.dropped_connections,
        },
        **report,
        "next_step": "Inspect visually (td_snapshot), then kb_promote_pop_pattern "
                     "with the verified_ops params if the technique holds.",
    }


@mcp.tool()
async def kb_promote_pop_pattern(pattern: dict) -> dict:
    """Append a validated pattern to the curated POP patterns KB.

    The pattern dict must satisfy the POPPattern schema (id, name,
    description, ops[{name, op_type, params}], connections[{out, into}],
    notes, pitfalls, references, verified_on_build). All op_types must
    exist in the operators catalog and at least one must be a POP. Ids are
    unique — promotion of an existing id is rejected, edit the JSON
    directly for revisions.
    """
    import json as _json

    from td_mcp.kb.pop_patterns import _DATA_PATH, POPPattern, get_pop_kb, reset_pop_kb_singleton

    try:
        validated = POPPattern.model_validate(pattern)
    except Exception as e:
        return {"ok": False, "error": f"schema validation failed: {e}"}

    catalog_classes = {e.python_class for e in get_catalog().list()}
    unknown = [o.op_type for o in validated.ops if o.op_type not in catalog_classes]
    if unknown:
        return {"ok": False, "error": f"op_types not in catalog: {unknown}"}
    if not any(o.op_type.endswith("POP") for o in validated.ops):
        return {"ok": False, "error": "no POP operator — this KB curates POP workflows"}
    if get_pop_kb().get(validated.id) is not None:
        return {"ok": False, "error": f"pattern id {validated.id!r} already exists"}

    from td_mcp.util import write_json_atomic

    data = _json.loads(_DATA_PATH.read_text())
    data["patterns"].append(validated.model_dump())
    # Atomic: this file is the hand-curated source of truth — a crash
    # mid-write must not corrupt it.
    write_json_atomic(_DATA_PATH, data, indent=2)
    reset_pop_kb_singleton()
    return {"ok": True, "id": validated.id, "total_patterns": len(data["patterns"]),
            "next_step": "kb_index_update to fold the new pattern chunk into the index."}


@mcp.tool()
async def kb_top_pattern(pattern_id: str = "", tag: str = "") -> dict:
    """Curated TOP-family recipes: numerical solvers, feedback architectures and
    volumetric rendering built from node operators instead of GLSL.

    No args: returns the index of available patterns.
    pattern_id: returns the full pattern (ops + connections + notes + pitfalls).
    tag: returns all patterns matching the tag.

    Search this BEFORE reaching for a glslTOP. The operator semantics these
    patterns depend on (slopeTOP dividing by the sample step, overTOP wanting
    premultiplied alpha, mathTOP 'len' writing only red...) were measured live,
    not read from documentation — the pitfalls list is the expensive part.
    """
    kb = get_top_kb()
    if pattern_id:
        pat = kb.get(pattern_id)
        if pat is None:
            return {
                "ok": False,
                "error": f"Unknown pattern id: {pattern_id!r}",
                "available": [p.id for p in kb.patterns],
            }
        return {"ok": True, "pattern": pat.model_dump()}
    if tag:
        matches = kb.by_tag(tag)
        return {"ok": True, "tag": tag, "count": len(matches),
                "patterns": [m.model_dump() for m in matches]}
    return {
        "ok": True,
        "count": len(kb.patterns),
        "patterns": kb.index(),
        "note": "Author new patterns by editing td_mcp/kb/data/top_patterns.json "
                "or via kb_promote_top_pattern.",
    }


@mcp.tool()
async def kb_promote_top_pattern(pattern: dict) -> dict:
    """Append a validated pattern to the curated TOP patterns KB.

    Symmetric with kb_promote_pop_pattern. The pattern dict must satisfy the
    TOPPattern schema (id, name, description, ops[{name, op_type, params}],
    connections[{out, into}], notes, pitfalls, references, verified_on_build).
    All op_types must exist in the operators catalog and at least one must be a
    TOP. Ids are unique — promotion of an existing id is rejected, edit the JSON
    directly for revisions.
    """
    import json as _json

    from td_mcp.kb.top_patterns import (
        _DATA_PATH,
        TOPPattern,
        get_top_kb,
        reset_top_kb_singleton,
    )

    try:
        validated = TOPPattern.model_validate(pattern)
    except Exception as e:
        return {"ok": False, "error": f"schema validation failed: {e}"}

    catalog_classes = {e.python_class for e in get_catalog().list()}
    unknown = [o.op_type for o in validated.ops if o.op_type not in catalog_classes]
    if unknown:
        return {"ok": False, "error": f"op_types not in catalog: {unknown}"}
    if not any(o.op_type.endswith("TOP") for o in validated.ops):
        return {"ok": False, "error": "no TOP operator — this KB curates TOP workflows"}
    if get_top_kb().get(validated.id) is not None:
        return {"ok": False, "error": f"pattern id {validated.id!r} already exists"}

    from td_mcp.util import write_json_atomic

    data = _json.loads(_DATA_PATH.read_text())
    data["patterns"].append(validated.model_dump())
    write_json_atomic(_DATA_PATH, data, indent=2)
    reset_top_kb_singleton()
    return {"ok": True, "id": validated.id, "total_patterns": len(data["patterns"]),
            "next_step": "kb_index_update to fold the new pattern chunk into the index."}


@mcp.tool()
async def kb_vector_status() -> dict:
    """Report on the vector index: location, model, row count, whether built."""
    kb = get_vector_kb()
    return {
        "ok": True,
        "model": kb.model_name,
        "path": str(kb.db_path),
        "has_index": kb.has_index(),
        "count": kb.count(),
    }


@mcp.tool()
async def kb_ingest_wiki(family: str = "POP", limit: int | None = None) -> dict:
    """Scrape derivative.ca wiki pages for an op family (default POP) and
    cache them locally. Polite: 1 req/sec floor, identifies as td-mcp.

    Does NOT automatically reindex — call kb_reindex afterward to fold the
    new wiki chunks into the vector store. Repeat calls reuse the cache,
    so re-running is free.

    family ∈ {CHOP, TOP, SOP, POP, DAT, MAT}. limit caps the number of
    pages fetched per family (useful for smoke-testing without the full
    ~100-page run).
    """
    from td_mcp.ingest.wiki import WikiClient, ingest_family, manifest

    with WikiClient() as client:
        chunks = ingest_family(family, client, limit=limit)

    m = manifest()
    return {
        "ok": True,
        "family": family,
        "fetched": len(chunks),
        "cache": m,
        "next_step": "Call kb_reindex to fold wiki chunks into the vector store.",
    }


@mcp.tool()
async def kb_wiki_status() -> dict:
    """Report on the local wiki cache: location, file count, total bytes."""
    from td_mcp.ingest.wiki import manifest

    return {"ok": True, **manifest()}


@mcp.tool()
async def kb_list_youtube_sources() -> dict:
    """List configured YouTube channels and playlists for ingestion."""
    from td_mcp.ingest.youtube import load_sources

    return {"ok": True, **load_sources()}


@mcp.tool()
async def kb_ingest_youtube_channel(
    handle: str = "OkamirufuV",
    limit: int = 1,
    model: str | None = None,
) -> dict:
    """Download audio + transcribe up to `limit` videos from a YouTube channel.

    Resumes from cache: videos already downloaded skip download; videos
    already transcribed skip transcription. Defaults to 1 video to keep
    interactive calls bounded. Use limit=0 (or large N) for a full batch
    — that's a background job (CPU: ~5x realtime, GPU: ~30x realtime).

    `model` overrides TD_MCP_WHISPER_MODEL. Options: tiny | base | small |
    medium | large-v3. Bigger = slower + better transcription quality.
    Defaults to "base" (~140MB, balanced for iteration).

    Does NOT reindex — call kb_reindex afterward to fold the new chunks
    into the vector store.
    """
    from td_mcp.ingest.youtube import (
        DEFAULT_WHISPER_MODEL,
        download_audio,
        list_channel_videos,
        load_sources,
        manifest,
        transcribe,
    )

    sources = load_sources()
    channel = next((c for c in sources["channels"] if c["handle"] == handle), None)
    if channel is None:
        return {
            "ok": False,
            "error": f"Unknown channel handle: {handle!r}",
            "available": [c["handle"] for c in sources["channels"]],
        }

    videos = list_channel_videos(channel["url"], limit=limit if limit > 0 else None)
    if not videos:
        return {"ok": False, "error": "No videos returned from channel"}

    use_model = model or DEFAULT_WHISPER_MODEL
    processed = []
    failed = []
    for v in videos:
        # One private/deleted/region-locked video must not abort the batch.
        try:
            audio = download_audio(v, handle)
            transcribe(audio, model_name=use_model)
            processed.append(
                {"video_id": v.video_id, "title": v.title, "duration_sec": v.duration_sec}
            )
        except Exception as e:
            failed.append({"video_id": v.video_id, "title": v.title, "error": str(e)})

    return {
        "ok": True,
        "channel": handle,
        "model": use_model,
        "processed_count": len(processed),
        "processed": processed,
        "failed_count": len(failed),
        "failed": failed,
        "cache": manifest(),
        "next_step": "Call kb_reindex to fold tutorial chunks into the vector store.",
    }


@mcp.tool()
async def kb_ingest_tutorial_vision(
    handle: str | None = None,
    video_id: str | None = None,
    limit: int = 1,
    model: str | None = None,
) -> dict:
    """Vision pass over already-transcribed tutorials: watch the video,
    not just the transcript. Downloads video (≤1080p), scene-detects
    keyframes, sends each segment (frames + aligned transcript) to a
    vision model (default claude-sonnet-5), and caches structured
    technique extractions (operators, parameters, wiring) per video.

    Requires ANTHROPIC_API_KEY and the [vision] extra (pip install
    anthropic). Operates ONLY on videos already in the youtube cache
    with a transcript — run kb_ingest_youtube_channel first.

    `video_id` targets one video; otherwise attempts up to `limit`
    transcribed videos (of `handle`, or any channel) — failures count
    toward the limit, so a cache of broken videos is not re-billed
    endlessly. limit=0 means all (like kb_ingest_youtube_channel).
    Resumable: segments already extracted are skipped, errored ones
    retried.

    Does NOT reindex — call kb_reindex afterward.
    """
    import json as _json
    import os as _os

    from td_mcp.ingest.tutorial_vision import (
        DEFAULT_VISION_MODEL,
        manifest as vision_manifest,
        process_video,
    )
    from td_mcp.ingest.youtube import DEFAULT_CACHE_DIR, VideoMeta

    try:
        import anthropic  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "anthropic package not installed — pip install 'td-mcp[vision]'"}
    if not _os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set"}

    candidates: list[tuple[VideoMeta, str]] = []
    if DEFAULT_CACHE_DIR.exists():
        for channel_dir in sorted(DEFAULT_CACHE_DIR.iterdir()):
            if not channel_dir.is_dir():
                continue
            if handle and channel_dir.name != handle.lstrip("@"):
                continue
            for video_dir in sorted(channel_dir.iterdir()):
                meta_path = video_dir / "meta.json"
                if not meta_path.exists() or not (video_dir / "transcript.json").exists():
                    continue
                meta = VideoMeta.from_dict(_json.loads(meta_path.read_text()))
                if video_id and meta.video_id != video_id:
                    continue
                candidates.append((meta, channel_dir.name))

    if not candidates:
        return {
            "ok": False,
            "error": "No transcribed videos matching the filters in the cache.",
            "hint": "Run kb_ingest_youtube_channel first.",
        }

    use_model = model or DEFAULT_VISION_MODEL
    reports = []
    attempts = 0
    for meta, chan in candidates:
        # Attempts (not successes) count toward the limit: bounding on
        # successes makes a cache full of failing videos re-run — and
        # re-bill — every candidate on every call.
        if not video_id and limit > 0 and attempts >= limit:
            break
        attempts += 1
        report = process_video(meta, chan, model=use_model)
        reports.append({"video_id": meta.video_id, "title": meta.title, **report})

    return {
        "ok": True,
        "model": use_model,
        "reports": reports,
        "cache": vision_manifest(),
        "next_step": "Call kb_reindex to fold vision chunks into the vector store.",
    }


@mcp.tool()
async def kb_youtube_status() -> dict:
    """Report on the local YouTube cache: channels, total videos, transcribed count."""
    from td_mcp.ingest.youtube import manifest

    return {"ok": True, **manifest()}


@mcp.tool()
async def kb_get_cinematic_recipe(look: str) -> dict:
    """Return a typed cinematic look recipe (operator chain, params, pitfalls).

    Valid looks: dof_shallow, dof_rack_focus, lumablur_soft, lumablur_bloom,
    anamorphic_flare, filmic_grade, volumetric_god_rays, motion_blur_velocity,
    chromatic_aberration_subtle, film_grain_clean. Lookup is exact-match
    against the Literal — no fuzzy search.
    """
    kb = get_cinematic_kb()
    recipe = kb.get(look)
    if recipe is None:
        return {
            "ok": False,
            "error": f"unknown look '{look}'",
            "available": kb.list_looks(),
        }
    return {"ok": True, "recipe": recipe.model_dump()}


@mcp.tool()
async def kb_get_vj_loop_reference(query: str = "", top_k: int = 3) -> dict:
    """Return curated VJ loop patterns matching the natural-language query.

    Each pattern is a typed structure (tempo, energy, palette, key
    operators, description). When the VJ visual corpus is ingested via
    kb_ingest_vj_corpus, patterns also carry visual_refs to similar
    frames from the corpus. Use this to ground 'VJ loop' requests in
    concrete references instead of hallucinated operator names.
    """
    kb = get_vj_loops_kb()
    patterns = kb.search(query, top_k=top_k)
    return {
        "ok": True,
        "patterns": [p.model_dump() for p in patterns],
        "total_in_kb": len(kb.patterns),
    }


# ─────────────────────────── layout orchestration ──────────────────────────


@mcp.tool()
async def td_layout_network(parent: str, mode: str = "grid_annotated") -> dict:
    """Reorganize a TD network: topological grid, optional cluster annotations
    and semantic renaming of generic ops.

    `parent` is the COMP whose CHILDREN get laid out (same convention as
    td_get_network). Required on purpose: a defaulted target once made a
    silently-misnamed argument reorganize /project1 instead of the intended
    COMP.

    Modes:
    - "grid": geometric grid only (move ops on a column-by-family grid)
    - "grid_annotated": grid + cluster Annotate COMPs + generic-name renaming

    Takes a comp-scoped .tox checkpoint of `parent` before applying changes;
    the returned diff carries its checkpoint id for td_rollback. `parent`
    must therefore be a COMP below root (like td_checkpoint) — laying out
    "/" directly is not supported.
    """
    if mode not in ("grid", "grid_annotated"):
        return {"ok": False, "error": f"unknown mode '{mode}'"}

    network = await _call("get_network", path=parent)
    if not network.get("ok"):
        return network
    ops = network.get("ops", [])
    connections = network.get("connections", [])

    if not ops:
        return {"ok": True, "diff": LayoutDiff().model_dump()}

    # Wires + param references (material, camera, pop, top...) both count as
    # dependencies: without ref edges, mats/cams/geos have no wires at all and
    # pile up in column 0 as a tall vertical stack — TD networks read
    # left-to-right. Cycles (feedback target, particle targetpop) are broken
    # by assign_columns_by_depth.
    ref_connections = network.get("ref_connections", [])
    edges = [(c["src"], c["dst"]) for c in connections]
    edges += [(c["src"], c["dst"]) for c in ref_connections]
    op_paths = [o["path"] for o in ops]
    columns = assign_columns_by_depth(op_paths, edges)

    ops_meta_for_layout = [
        {"path": o["path"], "family": o["family"], "column": columns.get(o["path"], 0)}
        for o in ops
    ]
    positions = geometric_layout(ops_meta_for_layout)
    moved = [
        OperatorPosition(path=p, x=xy[0], y=xy[1])
        for p, xy in positions.items()
    ]

    annotations: list[AnnotationSpec] = []
    renames: list[OperatorRename] = []
    if mode == "grid_annotated":
        ops_meta_for_clusters = [
            {"path": o["path"], "op_type": o["op_type"]} for o in ops
        ]
        clusters = detect_clusters(ops_meta_for_clusters, edges)
        for c in clusters:
            member_positions = [positions[m] for m in c["members"] if m in positions]
            if not member_positions:
                continue
            xs = [p[0] for p in member_positions]
            ys = [p[1] for p in member_positions]
            annotations.append(AnnotationSpec(
                cluster_name=c["name"],
                member_paths=c["members"],
                bbox_x=min(xs) - 20,
                bbox_y=min(ys) - 20,
                bbox_w=(max(xs) - min(xs)) + 220,
                bbox_h=(max(ys) - min(ys)) + 170,
            ))

        upstream_types_by_op: dict[str, list[str]] = {o["path"]: [] for o in ops}
        type_by_path = {o["path"]: o["op_type"] for o in ops}
        for src, dst in edges:
            if src in type_by_path and dst in upstream_types_by_op:
                upstream_types_by_op[dst].append(type_by_path[src])
        for o in ops:
            new_name = propose_rename(o, upstream_types_by_op[o["path"]])
            if new_name:
                parent_dir = o["path"].rsplit("/", 1)[0]
                renames.append(OperatorRename(
                    old_path=o["path"],
                    new_path=f"{parent_dir}/{new_name}",
                    reason=f"generic name + upstream {upstream_types_by_op[o['path']]}",
                ))

    ckpt = await _take_checkpoint(parent, label=f"pre-layout {parent}")
    if not ckpt.get("ok"):
        return ckpt
    checkpoint_id = ckpt.get("id", "")

    apply_payload = {
        "moves": [m.model_dump() for m in moved],
        "renames": [r.model_dump() for r in renames],
        "annotations": [a.model_dump() for a in annotations],
    }
    apply_result = await _call("apply_layout", path=parent, **apply_payload)
    if not apply_result.get("ok"):
        return apply_result

    diff = LayoutDiff(
        moved=moved,
        renamed=renames,
        annotations_added=annotations,
        checkpoint_id=checkpoint_id,
    )
    return {"ok": True, "diff": diff.model_dump()}


@mcp.tool()
async def kb_ingest_geeks3d_shaders(limit: int | None = None) -> dict:
    """Scrape and cache shader articles from geeks3d.com/shader-library/.

    Polite scraper (1 req/sec, cached on disk). `limit` caps how many
    articles to fetch in one run — useful for incremental ingestion.
    After ingestion, call kb_reindex to push cached chunks into the
    vector KB.
    """
    from td_mcp.ingest.shaders_geeks3d import ingest_geeks3d
    try:
        report = ingest_geeks3d(limit=limit)
        return {"ok": True, "report": report}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def kb_ingest_vj_corpus(url_list_path: str) -> dict:
    """Run the VJ loops ingestion pipeline against a JSON URL list.

    Long-running (downloads videos, extracts frames, embeds with CLIP,
    classifies with Haiku). Designed to be resumable: a cache file next
    to the URL list (`<name>.cache.json`) stores Haiku results keyed by
    frame hash so re-runs skip already-classified frames.
    """
    from pathlib import Path
    p = Path(url_list_path)
    if not p.exists():
        return {"ok": False, "error": f"url list not found: {url_list_path}"}
    cache_path = p.with_suffix(".cache.json")
    try:
        report = ingest_vj_corpus(p, cache_path=cache_path)
        return {"ok": True, "report": report}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def kb_ingest_shadertoy_shaders(queries: list[str], num_per_query: int = 24) -> dict:
    """Search Shadertoy and cache shader source for the given queries.

    Requires TD_MCP_SHADERTOY_API_KEY env var. Free key from
    shadertoy.com/myapps. Each query is searched, each result is
    fetched and cached on disk. After ingestion, call kb_reindex to
    push cached chunks into the vector KB.
    """
    from td_mcp.ingest.shaders_shadertoy import ingest_shadertoy
    try:
        report = ingest_shadertoy(queries=queries, num_per_query=num_per_query)
        return {"ok": True, "report": report}
    except Exception as e:
        return {"ok": False, "error": str(e)}
