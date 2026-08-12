# td-mcp

TouchDesigner MCP server — a live bridge into a running TD instance, a typed knowledge base (683-operator catalog with full param schemas, POP patterns, GLSL templates, cinematic recipes), semantic search over ingested tutorials/wiki/shaders, and a build loop with plans, snapshots and checkpoints.

## Install

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"     # extras: .[kb] vector search (~3GB torch) · .[ingest] scrapers+whisper
                            #         .[vision] tutorial vision pass · .[vj] VJ corpus (CLIP)
claude mcp add td-mcp -- td-mcp          # or {"mcpServers": {"td-mcp": {"command": "td-mcp"}}}
python scripts/install_skills.py         # copies 3 SKILL.md into ~/.claude/skills/, re-run after pulls
```

**TouchDesigner side** — drag `td_bridge_tox/td_mcp_bridge.tox` into `/project1/` (port 9988). It wraps a WebServer DAT whose callbacks are `td_bridge_tox/webserver_callbacks.py`; every `td_connect` compares script hashes and re-syncs the DAT if the project reverted it.

**Auth** — the DAT listens on the network and the bridge exposes `eval`/`exec`, so on first `td_connect` the server writes a same-machine token (`~/.cache/td-mcp/bridge_token`) and sends it with every message; once the file exists, TD rejects unauthenticated messages. A LAN client can reach the port but not the file. To force a fixed secret instead, set `SHARED_SECRET` in the DAT callbacks.

## Use cases

- **Build a POP system from a prompt** — 8 vetted chains: particle feedback sim core, `rayPOP` collisions, noise-displaced grids and spheres, point-cloud starter, CHOP→POP and TOP→POP bridges. The agent starts from a network that cooks, not from guesses.
- **Reproduce a visual reference** — the vibe loop: build → `td_snapshot` → `td_visual_diff` against the reference (luminance/contrast/RGB deltas + CLIP similarity) → adjust. Converges on a look instead of arguing about it.
- **Cinematic post chains** — 10 recipes with operator chain, param values and pitfalls: shallow DOF, rack focus, lumablur bloom, anamorphic flare, filmic grade, god rays, velocity motion blur, chromatic aberration, film grain.
- **Audio-reactive VJ loops** — 22 patterns tagged by BPM range, energy, palette and key operators, for tempo-locked show content.
- **GLSL TOPs** — 4 vetted templates (procedural, 1-input, 2-input, compute) with the uniform reference and TD-specific antipatterns.
- **Read and refactor an existing project** — `td_get_network` + `td_perf` to find what's eating the frame, `td_layout_network` to make an inherited spaghetti network legible.

## Tools

- **Bridge / project** — `ping`, `td_connect`, `td_disconnect`, `td_status`, `td_get_network` (wired edges **and** `ref_connections`: OP-style params resolved to sibling edges), `td_op_info`, `td_create_op`, `td_delete_op`, `td_connect_ops`, `td_set_param` (typo suggestions matched on internal names *and* display labels), `td_set_flags` (display/render/bypass/viewer/lock), `td_pulse`, `td_expr`, `td_run_script`, `td_timeline_play/stop`, `td_save_project`
- **Palette (reuse before build)** — `td_palette_list` (TD's built-in palette *and* `app.userPaletteFolder`: RayTK, downloaded packs, saved COMPs; `builtin:`/`user:` qualifiers), `td_palette_load` (unknown names return close matches instead of hitting TD; Derivative's icon-wrapper `.tox` files are unwrapped to the real inner component, like native drag & drop)
- **Plan & safety** — `td_plan` (KB-grounded staging, gap protocol), `td_checkpoint` / `td_rollback` / `td_list_checkpoints` (comp-scoped `.tox` snapshots, FIFO 20), `td_layout_network` (topological grid over wire **and** param-reference edges, so networks read left-to-right like hand-built projects; cluster annotations + semantic renames, checkpointed)
- **Visual loop & perf** — `td_snapshot` (optional `max_size` downscale), `td_visual_diff`, `td_perf` (heaviest ops by cook time + `budget_eaters`); every bridge response carries a `cook_pressure_warning` when sustained roundtrip latency says the graph is starving the bridge (`TD_MCP_COOK_PRESSURE_MS`, default 750)
- **KB** — `kb_list_operators`, `kb_get_operator` (param schemas: internal name, label, style, menu tokens), `kb_refresh_operators_catalog`, `kb_pop_pattern`, `kb_promote_pop_pattern`, `kb_glsl_template`, `kb_get_cinematic_recipe`, `kb_get_vj_loop_reference`
- **Search & corpus** — `kb_search` (filters: source/family/is_glsl), `kb_get_tutorial` (EVERY chunk of one video, ordered — transcript + vision), `kb_reindex`, `kb_index_update` (incremental upsert with orphan purge), `kb_vector_status`, `kb_wiki_status`, `kb_youtube_status`, `kb_list_youtube_sources`

## Training the knowledge base

Nothing here fine-tunes a model — "training" means growing the corpus this server retrieves from, then re-indexing. Ingestion is resumable and the `*_status` tools tell you where a run stopped, so you never pay for the same pass twice.

```bash
pip install -e ".[kb,ingest]"     # + .[vision] for the keyframe pass, .[vj] for the CLIP corpus
```

Then, from the agent:

1. **Operators** — `kb_refresh_operators_catalog` introspects the *running* TD build, so the catalog matches your version (currently 683 ops from 2025.32820) instead of stale docs.
2. **Wiki** — `kb_ingest_wiki` / `kb_ingest_wiki_full` crawl docs.derivative.ca politely and resumably.
3. **Tutorials** — `kb_ingest_youtube_channel` (yt-dlp + whisper) over the 7 curated channels, then `kb_ingest_tutorial_vision`: scene-detected keyframes → vision model → structured technique extractions. Keyframes beat transcripts for the *look* — a tutorial says "add some noise", the frame says which noise, at what scale.
4. **Shaders** — `kb_ingest_geeks3d_shaders`, `kb_ingest_shadertoy_shaders`; `kb_ingest_vj_corpus` classifies reference loops with CLIP + Haiku.
5. **Index** — `kb_reindex` once, `kb_index_update` after every ingestion (incremental upsert, purges orphans). Default embedding: `BAAI/bge-m3` (~2GB, multilingual); index lives in `~/.cache/td-mcp/lancedb/`.

The loop closes the other way too: `td_compile_technique` builds an extracted technique live in TD to check it actually cooks, and `kb_promote_pop_pattern` writes a validated network back into the typed KB — successful builds become tomorrow's starting points.

## Architecture

1. **Live bridge** — async WebSocket client to a WebServer DAT inside TD. JSON protocol: `{id, action, data, token?}` ↔ `{id, ok, result|error}`, with bridge-script drift detection on connect.
2. **Knowledge** — typed sub-KBs (operators, POP patterns, GLSL templates, cinematic looks, VJ loops) plus a LanceDB vector index (BGE-M3) over operators, wiki, transcripts, vision extractions and shaders.
3. **Build protocol** — `td_plan` gates creation (soft; hard with `TD_MCP_REQUIRE_PLAN=1`), palette lookup precedes building from scratch, checkpoints bound experiments, snapshot + diff close the visual loop, layout makes the result readable.

## Known TouchDesigner-side limits

Things the bridge cannot fix — worth knowing before debugging the wrong layer:

- **Non-Commercial licenses silently clamp every TOP to 1280×1280.** Params still read `1920×1080`, `.width` says `1280`, nothing errors. Check `td.licenses.type` first when a resolution won't take. (The project res multiplier also scales TOPs down; `resmult=False` opts a node out.)
- **Sequential parameter blocks can't be grown from the bridge** (`linePOP.pt`, `mathcombinePOP.comb`…): `seq.X.numBlocks`, `par.X = n` and `insertBlock()` all fail. Work around structurally — chain two `mathcombinePOP`s, fix the default `linePOP` run with a `transformPOP`.
- **`geoCOMP`s take no wired data inputs.** The idiom is a `selectPOP`/`inSOP` *inside* the geo, display+render flagged via `td_set_flags`; `td_connect_ops` says so instead of failing opaquely.

## Environment variables

| Variable | Effect |
|---|---|
| `TD_MCP_REQUIRE_PLAN=1` | `td_create_op` hard-fails without a registered `td_plan` |
| `TD_MCP_COOK_PRESSURE_MS` | Median-latency threshold for `cook_pressure_warning` (default 750) |
| `TD_MCP_TOKEN_FILE` | Bridge token file (default `~/.cache/td-mcp/bridge_token`) |
| `TD_MCP_EMBEDDING_MODEL` / `TD_MCP_VECTOR_DB` | Embedding model override / LanceDB index location |
| `TD_MCP_WHISPER_MODEL` | Whisper model (`tiny`…`large-v3`, default `base`) |
| `TD_MCP_YTDLP_COOKIES_BROWSER` | Browser to read YouTube cookies from (SABR workaround) |
| `TD_MCP_SHADERTOY_API_KEY` / `ANTHROPIC_API_KEY` | Shader ingestion / vision + VJ classification |

## Development

```bash
pytest -q        # 213 passed, 1 skipped — no TD or network needed
ruff check .     # TD builtins ignored for the DAT script
```

CI runs both on every push/PR plus a wheel build check. Bridge, checkpoints, layout, typed KBs, vector search, ingestion, palette loading, cook-cost instrumentation and the visual diff loop are live and validated against TD 2025.32820. Open workstreams: eval harness for KB curation, session-persistent checkpoints, wiki-enriched catalog descriptions + hybrid FTS search.

## License

MIT
