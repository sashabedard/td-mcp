# td-mcp

TouchDesigner MCP server — live bridge to a running TD instance, typed knowledge base, dedicated POP support, vibe-coding loop with screenshots and checkpoints.

Status: **early scaffolding (Phase 0 / Phase 1 in progress)**.

## Architecture

Three layers:

1. **Live Bridge** — async WebSocket client to a `WebServer DAT` running inside TouchDesigner (companion `.tox` in `td_bridge_tox/`). JSON protocol: `{id, action, data, token?}` ↔ `{id, ok, result|error}`.
2. **Knowledge** — LanceDB-backed structured operators table + hybrid vector chunks (BGE-M3 embeddings), with dedicated POP and GLSL TOP sub-KBs. *(Phase 3+)*
3. **Agent Loop** — vibe mode (default, short loop with screenshot checkpoints) and rigorous mode (opt-in, typed plans with validation). *(Phase 6+)*

## Install (dev)

```bash
cd td-mcp
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Optional extras: `pip install -e ".[kb,ingest,dev]"` for vector DB + scraping deps.

## Run

```bash
td-mcp                  # stdio MCP server
```

Or wire into Claude Code via `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "td-mcp": { "command": "td-mcp" }
  }
}
```

## TouchDesigner side

Drag-drop `td_bridge_tox/td_mcp_bridge.tox` into `/project1/`. Defaults to port 9981. The companion `.tox` is generated from the Python source in `td_bridge_tox/webserver_callbacks.py`.

## Status of phases

- [x] Phase 0 — boilerplate, ping
- [ ] Phase 1 — bridge live (connect, status, inspect, create, mutate, connect ops, delete)
- [ ] Phase 2 — screenshot, cook stats
- [ ] Phase 2.5 — checkpoint/rollback
- [ ] Phase 3 — KB operators structured (LanceDB + Pydantic validation)
- [ ] Phase 3.5 — Skills authoring (`~/.claude/skills/touchdesigner*/`)
- [ ] Phase 3.6 — Sub-KB POPs + skill + 15-25 curated patterns
- [ ] Phase 3.7 — Sub-KB GLSL TOP + templates
- [ ] Phase 4 — Vector KB ingestion (hybrid search + reranker)
- [ ] Phase 4.5 — Visual techniques curated (80-150 entries)
- [ ] Phase 5 — Workflow patterns curated (30-50, non-POP)
- [ ] Phase 6a — Vibe loop (set_reference, iterate, visual_diff)
- [ ] Phase 6b — Rigorous loop (state machine, propose_plan, validate)
- [ ] Phase 7 — Evals + baseline report
- [ ] Phase 8 — Polish, install script, semver compat check

## License

MIT
