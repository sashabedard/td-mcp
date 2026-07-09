# td-mcp

TouchDesigner MCP server — live bridge to a running TD instance, typed knowledge base (operators catalog, POP patterns, GLSL templates, cinematic recipes), semantic search over ingested tutorials/wiki/shaders, and a build loop with plans, screenshots and checkpoints.

## Architecture

Three layers:

1. **Live Bridge** — async WebSocket client to a `WebServer DAT` running inside TouchDesigner (companion `.tox` in `td_bridge_tox/`). JSON protocol: `{id, action, data, token?}` ↔ `{id, ok, result|error}`. The server detects bridge-script drift on connect and repairs the DAT automatically (`bridge_version` hash check).
2. **Knowledge** — typed sub-KBs (operators catalog introspected from TD with full param schemas, curated POP patterns, GLSL TOP templates, cinematic look recipes, VJ loop patterns) plus a LanceDB vector index (BGE-M3 embeddings) over operators, wiki, YouTube transcripts, vision-pass technique extractions and shader libraries.
3. **Build protocol** — `td_plan` registers a KB-grounded plan before creation (soft gate on `td_create_op`, hard with `TD_MCP_REQUIRE_PLAN=1`); `td_checkpoint`/`td_rollback` bound experiments; `td_snapshot` closes the visual loop; `td_layout_network` reorganizes and annotates the result.

## Install (dev)

```bash
cd td-mcp
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # tests + lint
# extras: .[kb] vector search (~3GB torch), .[ingest] scrapers/whisper,
#         .[vision] tutorial vision pass, .[vj] VJ corpus (CLIP)
```

## Run

```bash
td-mcp                  # stdio MCP server
```

Wire into Claude Code from the repo directory:

```bash
claude mcp add td-mcp -- td-mcp
```

(or add it to your project's `.mcp.json`):

```json
{
  "mcpServers": {
    "td-mcp": { "command": "td-mcp" }
  }
}
```

## TouchDesigner side

Drag-drop `td_bridge_tox/td_mcp_bridge.tox` into `/project1/`. Defaults to port 9988. The `.tox` wraps a WebServer DAT whose callbacks are `td_bridge_tox/webserver_callbacks.py`; on every `td_connect` the server compares script hashes and re-syncs the DAT if the project reverted it.

### Auth

The WebServer DAT listens on the network, and the bridge exposes `eval`/`exec`. On first `td_connect` the server creates a same-machine token file (`~/.cache/td-mcp/bridge_token`, override with `TD_MCP_TOKEN_FILE`) and sends it with every message; once the file exists, the TD side rejects messages without it. A LAN client can reach the port but not the file. To force a fixed secret instead, set `SHARED_SECRET` in the DAT callbacks.

## Tools (overview)

- **Bridge / project**: `td_connect`, `td_disconnect`, `td_status`, `td_get_network`, `td_op_info`, `td_create_op`, `td_delete_op`, `td_connect_ops`, `td_set_param` (typo suggestions from the enriched catalog), `td_pulse`, `td_expr`, `td_run_script`, `td_timeline_play/stop`, `td_save_project`, `td_snapshot`
- **Plan & safety**: `td_plan` (KB-grounded staging, gap protocol), `td_checkpoint` / `td_rollback` / `td_list_checkpoints` (comp-scoped `.tox` snapshots, FIFO 20), `td_layout_network` (topological grid + cluster annotations + semantic renames, checkpointed)
- **KB**: `kb_list_operators`, `kb_get_operator` (param schemas: internal name, label, style, menu tokens), `kb_refresh_operators_catalog`, `kb_pop_pattern`, `kb_promote_pop_pattern`, `kb_glsl_template`, `kb_get_cinematic_recipe`, `kb_get_vj_loop_reference`
- **Vector search**: `kb_search` (filters: source/family/is_glsl), `kb_get_tutorial` (EVERY chunk of one video, ordered — transcript + vision), `kb_reindex`, `kb_index_update` (incremental upsert with orphan purge), `kb_vector_status`
- **Ingestion**: `kb_ingest_wiki` / `kb_ingest_wiki_full` (docs.derivative.ca, polite + resumable), `kb_ingest_youtube_channel` (yt-dlp + whisper), `kb_ingest_tutorial_vision` (scene-detected keyframes → vision model → technique extractions), `td_compile_technique` (build an extraction live in TD), `kb_ingest_geeks3d_shaders`, `kb_ingest_shadertoy_shaders`, `kb_ingest_vj_corpus` (CLIP + Haiku)

## Vector KB

```bash
pip install -e ".[kb]"    # heavy: ~3GB incl. PyTorch
```

Then from the agent: `kb_reindex` once, `kb_index_update` after any ingestion, `kb_search query="..."`.

Default embedding model: `BAAI/bge-m3` (~2GB, multilingual). Override with `TD_MCP_EMBEDDING_MODEL` (e.g. `sentence-transformers/all-MiniLM-L6-v2` for fast iteration). Index lives in `~/.cache/td-mcp/lancedb/` (`TD_MCP_VECTOR_DB` to move it).

## Environment variables

| Variable | Effect |
|---|---|
| `TD_MCP_REQUIRE_PLAN=1` | `td_create_op` hard-fails without a registered `td_plan` |
| `TD_MCP_TOKEN_FILE` | Bridge token file location (default `~/.cache/td-mcp/bridge_token`) |
| `TD_MCP_EMBEDDING_MODEL` | Embedding model override |
| `TD_MCP_VECTOR_DB` | LanceDB index location |
| `TD_MCP_WHISPER_MODEL` | Whisper model (`tiny`…`large-v3`, default `base`) |
| `TD_MCP_YTDLP_COOKIES_BROWSER` | Browser to read YouTube cookies from (SABR workaround) |
| `TD_MCP_SHADERTOY_API_KEY` | Shadertoy API key for shader ingestion |
| `ANTHROPIC_API_KEY` | Vision pass + VJ corpus classification |

## Skills (workflow forcing)

Three SKILL.md files under `skills/` get auto-loaded by Claude Code when TD work is detected (KB-grounded planning, cook-budget protocol, visual validation, POP specifics). Install with:

```bash
python scripts/install_skills.py
```

Copies (not symlinks) into `~/.claude/skills/`. Re-run after pulling changes.

## Development

```bash
pytest -q        # full suite, no TD or network needed
ruff check .     # lint (TD builtins ignored for the DAT script)
```

CI runs both on every push/PR, plus a wheel build check.

## Status

Bridge, checkpoints, layout, typed KBs, vector search and all ingestion pipelines are live and tested. Open workstreams: visual diff loop (reference compare), cook-cost instrumentation (`td_perf`), eval harness for KB curation, session-persistent checkpoints.

## License

MIT
