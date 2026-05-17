import base64
import json
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from td_mcp import __version__
from td_mcp.bridge import bridge
from td_mcp.ingest.vj_loops import ingest_corpus as ingest_vj_corpus
from td_mcp.kb.cinematic import get_cinematic_kb
from td_mcp.kb.vj_loops import get_vj_loops_kb
from td_mcp.kb.glsl import get_glsl_kb
from td_mcp.kb.operators import OperatorEntry, OperatorsCatalog, get_catalog, reload_catalog
from td_mcp.kb.pop_patterns import get_pop_kb
from td_mcp.kb.vector import build_seed_chunks, get_vector_kb
from td_mcp.protocol import AnnotationSpec, LayoutDiff, OperatorPosition, OperatorRename, TDError
from td_mcp.tools.layout import (
    assign_columns_by_depth,
    detect_clusters,
    geometric_layout,
    propose_rename,
)

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
    KB-validated: unknown op_type returns {ok: false} with close-match
    suggestions instead of hitting TD. Use kb_list_operators or
    kb_get_operator to browse the catalog.
    """
    catalog = get_catalog()
    if not catalog.is_empty:
        if catalog.get(op_type) is None:
            return {
                "ok": False,
                "error": f"Unknown operator class: {op_type!r}",
                "suggestions": catalog.suggest(op_type, n=5),
                "hint": (
                    "Use kb_list_operators(family=...) to browse, or "
                    "kb_refresh_operators_catalog if the catalog is stale."
                ),
            }
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


# ─────────────────────────── knowledge base (Phase 3) ──────────────────────


@mcp.tool()
async def kb_list_operators(family: str | None = None, limit: int = 50) -> dict:
    """List operators known to the catalog, optionally filtered by family.

    family ∈ {CHOP, TOP, SOP, POP, DAT, COMP, MAT}. limit caps the response
    size; full counts are always reported in `total` and `total_in_family`.
    """
    catalog = get_catalog()
    if catalog.is_empty:
        return {
            "ok": False,
            "error": "Catalog is empty. Run kb_refresh_operators_catalog with TD connected.",
        }
    entries = catalog.list(family) if family else catalog.list()
    truncated = entries[:limit]
    return {
        "ok": True,
        "total": catalog.count,
        "by_family": catalog.family_counts(),
        "filter_family": family,
        "total_in_family": len(entries),
        "returned": len(truncated),
        "operators": [{"python_class": e.python_class, "family": e.family} for e in truncated],
    }


@mcp.tool()
async def kb_get_operator(query: str) -> dict:
    """Lookup an operator by python_class name. If not found, returns suggestions."""
    catalog = get_catalog()
    if catalog.is_empty:
        return {"ok": False, "error": "Catalog is empty."}
    entry = catalog.get(query)
    if entry is not None:
        return {"ok": True, "found": True, **entry.model_dump()}
    return {
        "ok": True,
        "found": False,
        "query": query,
        "suggestions": catalog.suggest(query, n=5),
    }


@mcp.tool()
async def kb_refresh_operators_catalog() -> dict:
    """Introspect the connected TD instance, regenerate the operators catalog,
    and persist it to td_mcp/kb/data/operators.json.

    Requires an active bridge. Uses the convention that creatable op classes
    start with a lowercase letter — abstract bases (ObjectCOMP, PanelCOMP, ...)
    are filtered out.
    """
    introspect = """
import json
suffixes = ['CHOP', 'TOP', 'SOP', 'POP', 'DAT', 'COMP', 'MAT']
ops = []
for clsname in dir(td):
    if clsname.startswith('_') or not clsname[0].islower():
        continue
    for suffix in suffixes:
        if clsname.endswith(suffix) and clsname != suffix and len(clsname) > len(suffix):
            ops.append({
                'python_class': clsname,
                'family': suffix,
                'subtype': clsname[:-len(suffix)],
            })
            break
print(json.dumps({'build': getattr(app, 'build', ''), 'operators': ops}))
"""
    result = await bridge.send("run_script", {"code": introspect})
    raw = result.get("output", "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Parse error: {e}", "raw": raw[:500]}

    entries = [OperatorEntry.model_validate(e) for e in data["operators"]]
    entries.sort(key=lambda e: (e.family, e.python_class))
    catalog = OperatorsCatalog(entries, td_build=data.get("build", ""))
    catalog.save()
    reload_catalog()
    return {
        "ok": True,
        "count": len(entries),
        "td_build": data.get("build", ""),
        "by_family": catalog.family_counts(),
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
) -> dict:
    """Semantic search across the indexed KB corpus (operators + GLSL templates + POP patterns).

    Returns top-k chunks ranked by vector similarity to the query, with
    optional pre-filtering by source ∈ {operators, glsl_template, pop_pattern, ...},
    family ∈ {CHOP, TOP, SOP, POP, ...}, or is_glsl.

    Requires the vector index to be built — run kb_reindex first if you get
    an empty result. The embedding model loads lazily on first call (BGE-M3
    default ~2GB download once; override via TD_MCP_EMBEDDING_MODEL).
    """
    kb = get_vector_kb()
    if not kb.has_index():
        return {
            "ok": False,
            "error": "Vector index is empty. Run kb_reindex to build it from the seed sources.",
        }
    results = kb.search(query, k=k, source=source, family=family, is_glsl=is_glsl)
    return {
        "ok": True,
        "query": query,
        "count": len(results),
        "results": results,
    }


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
    result = kb.reindex(chunks)
    return {"ok": True, **result}


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
    for v in videos:
        audio = download_audio(v, handle)
        transcribe(audio, model_name=use_model)
        processed.append({"video_id": v.video_id, "title": v.title, "duration_sec": v.duration_sec})

    return {
        "ok": True,
        "channel": handle,
        "model": use_model,
        "processed_count": len(processed),
        "processed": processed,
        "cache": manifest(),
        "next_step": "Call kb_reindex to fold tutorial chunks into the vector store.",
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
async def td_layout_network(path: str = "/", mode: str = "grid_annotated") -> dict:
    """Reorganize a TD network: topological grid, optional cluster annotations
    and semantic renaming of generic ops.

    Modes:
    - "grid": geometric grid only (move ops on a column-by-family grid)
    - "grid_annotated": grid + cluster Annotate COMPs + generic-name renaming

    Creates a checkpoint before applying changes. Returns a diff with the
    checkpoint id so changes can be rolled back via td_rollback.
    """
    if mode not in ("grid", "grid_annotated"):
        return {"ok": False, "error": f"unknown mode '{mode}'"}

    network = await _call("get_network", path=path)
    if not network.get("ok"):
        return network
    ops = network.get("ops", [])
    connections = network.get("connections", [])

    if not ops:
        return {"ok": True, "diff": LayoutDiff().model_dump()}

    edges = [(c["src"], c["dst"]) for c in connections]
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
                parent = o["path"].rsplit("/", 1)[0]
                renames.append(OperatorRename(
                    old_path=o["path"],
                    new_path=f"{parent}/{new_name}",
                    reason=f"generic name + upstream {upstream_types_by_op[o['path']]}",
                ))

    ckpt = await _call("checkpoint", label=f"pre-layout {path}")
    if not ckpt.get("ok"):
        return ckpt
    checkpoint_id = ckpt.get("checkpoint_id", "")

    apply_payload = {
        "moves": [m.model_dump() for m in moved],
        "renames": [r.model_dump() for r in renames],
        "annotations": [a.model_dump() for a in annotations],
    }
    apply_result = await _call("apply_layout", path=path, **apply_payload)
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
