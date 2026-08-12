# td-mcp

TouchDesigner MCP server — live bridge to a running TD instance, typed knowledge base (operators catalog, POP patterns, GLSL templates, cinematic recipes), semantic search over ingested tutorials/wiki/shaders, and a build loop with plans, screenshots and checkpoints.

## Architecture

Three layers:

1. **Live Bridge** — async WebSocket client to a `WebServer DAT` running inside TouchDesigner (companion `.tox` in `td_bridge_tox/`). JSON protocol: `{id, action, data, token?}` ↔ `{id, ok, result|error}`. The server detects bridge-script drift on connect and repairs the DAT automatically (`bridge_version` hash check).
2. **Knowledge** — typed sub-KBs (operators catalog introspected from TD with full param schemas, curated POP patterns, GLSL TOP templates, cinematic look recipes, VJ loop patterns) plus a LanceDB vector index (BGE-M3 embeddings) over operators, wiki, YouTube transcripts, vision-pass technique extractions and shader libraries.
3. **Build protocol** — `td_plan` registers a KB-grounded plan before creation (soft gate on `td_create_op`, hard with `TD_MCP_REQUIRE_PLAN=1`); `td_palette_list`/`td_palette_load` check whether TD or the user's palette already ships the component before it gets rebuilt from scratch; `td_checkpoint`/`td_rollback` bound experiments; `td_snapshot` + `td_visual_diff` close the visual loop; `td_layout_network` reorganizes and annotates the result.

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

- **Bridge / project**: `ping`, `td_connect`, `td_disconnect`, `td_status`, `td_get_network` (wired edges **and** `ref_connections` — OP-style params resolved to sibling edges), `td_op_info`, `td_create_op`, `td_delete_op`, `td_connect_ops` (a COMP with no data inputs gets the select-inside-the-COMP idiom explained instead of an IndexError), `td_set_param` (typo suggestions from the enriched catalog, matched on internal names *and* display labels), `td_set_flags` (display / render / bypass / viewer / lock — the geoCOMP render idiom without dropping to `run_script`), `td_pulse`, `td_expr`, `td_run_script`, `td_timeline_play/stop`, `td_save_project`
- **Palette (reuse before build)**: `td_palette_list` (TD's built-in palette *and* `app.userPaletteFolder` — RayTK, downloaded packs, saved COMPs; substring filter, `builtin:` / `user:` qualifiers), `td_palette_load` (`loadTox` into any parent; unknown or ambiguous names return close matches instead of hitting TD, and Derivative's icon-wrapper `.tox` files are unwrapped to the real inner component like native drag & drop)
- **Plan & safety**: `td_plan` (KB-grounded staging, gap protocol), `td_checkpoint` / `td_rollback` / `td_list_checkpoints` (comp-scoped `.tox` snapshots, FIFO 20), `td_layout_network` (topological grid over wire **and** param-reference edges — so networks read left-to-right like hand-built TD projects — plus cluster annotations and semantic renames, checkpointed)
- **Visual loop & perf**: `td_snapshot` (optional `max_size` downscale), `td_visual_diff` (snapshot vs reference image: similarity, luminance/contrast/RGB deltas, verbal notes, CLIP similarity with the [vj] extra), `td_perf` (heaviest ops by cook time + `budget_eaters`); every bridge response carries a `cook_pressure_warning` when sustained roundtrip latency says the graph is starving the bridge (`TD_MCP_COOK_PRESSURE_MS`, default 750)
- **KB**: `kb_list_operators`, `kb_get_operator` (param schemas: internal name, label, style, menu tokens), `kb_refresh_operators_catalog`, `kb_pop_pattern`, `kb_promote_pop_pattern`, `kb_glsl_template`, `kb_get_cinematic_recipe`, `kb_get_vj_loop_reference`
- **Vector search**: `kb_search` (filters: source/family/is_glsl), `kb_get_tutorial` (EVERY chunk of one video, ordered — transcript + vision), `kb_reindex`, `kb_index_update` (incremental upsert with orphan purge)
- **Ingestion**: `kb_ingest_wiki` / `kb_ingest_wiki_full` (docs.derivative.ca, polite + resumable), `kb_ingest_youtube_channel` (yt-dlp + whisper), `kb_ingest_tutorial_vision` (scene-detected keyframes → vision model → technique extractions), `td_compile_technique` (build an extraction live in TD), `kb_ingest_geeks3d_shaders`, `kb_ingest_shadertoy_shaders`, `kb_ingest_vj_corpus` (CLIP + Haiku)
- **Corpus state**: `kb_wiki_status`, `kb_youtube_status`, `kb_list_youtube_sources`, `kb_vector_status` — what's already ingested and where a resumable run left off, before paying for another pass

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
| `TD_MCP_COOK_PRESSURE_MS` | Median-latency threshold for `cook_pressure_warning` (default 750) |
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

CI runs both on every push/PR, plus a wheel build check. Current suite: 213 passed, 1 skipped.

## Known TouchDesigner-side limits

Things the bridge cannot fix, learned the hard way during live builds — worth knowing before debugging the wrong layer:

- **Non-Commercial licenses silently clamp every TOP to 1280×1280.** Params still read `1920×1080`, `.width` says `1280`, and nothing errors. Check `td.licenses.type` first when a resolution won't take. (The project's global res multiplier scales TOPs down too; `resmult=False` opts a node out.)
- **Sequential parameter blocks cannot be grown from the bridge** (`linePOP.pt` points, `mathcombinePOP.comb` operations…): `seq.X.numBlocks`, `par.X = n` and `insertBlock()` all fail. Work around structurally — chain two `mathcombinePOP`s instead of one with two operations, correct the default `linePOP` run with a `transformPOP`.
- **`geoCOMP`s take no wired data inputs.** The idiom is a `selectPOP`/`inSOP` *inside* the geo pointing at the source, with display+render flagged on via `td_set_flags`; `td_connect_ops` now says so instead of failing opaquely.

## Status

Bridge, checkpoints, layout, typed KBs, vector search, ingestion pipelines, palette loading, cook-cost instrumentation (`td_perf` + cook-pressure watchdog) and the visual diff loop (`td_visual_diff`) are live and tested against TD 2025.32820. Open workstreams: eval harness for KB curation, session-persistent checkpoints, wiki-enriched catalog descriptions + hybrid FTS search.

## License

MIT
