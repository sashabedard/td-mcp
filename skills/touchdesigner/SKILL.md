---
name: touchdesigner
description: Use when working with TouchDesigner — .toe/.tox files, operator families (CHOP/TOP/SOP/DAT/COMP/MAT/POP), GLSL TOPs, audio-reactive/real-time graphics/projection-mapping/interactive-installation work explicitly in TD, or any of the td-mcp tools (td_create_op, td_set_param, kb_list_operators, ...). Activates on any mention of TouchDesigner operators, networks, or workflows.
version: 0.1.0
---

# TouchDesigner protocol

Your training data on TouchDesigner is outdated and frequently wrong about operator class names, parameter internals, the Python `td` module, and GLSL TOP conventions. POPs (released TD 2025) have near-zero coverage in your training. The td-mcp server provides typed, KB-validated tools that mechanically catch hallucinations — use them instead of guessing.

## Mandatory protocol before writing any TD code or proposing any operator

1. **Connect** if not already: call `mcp__td-mcp__td_connect` (default `ws://127.0.0.1:9988`).
2. **Inspect current state** with `mcp__td-mcp__td_get_network <parent>` before suggesting any change. Never assume what's in the project.
3. **Validate operator class names** via `mcp__td-mcp__kb_get_operator query="..."` or `kb_list_operators family="..."` BEFORE proposing them. The catalog is the source of truth — if `op_type` isn't in it, it doesn't exist or your spelling is wrong.
4. **Prefer typed tools** (`td_create_op`, `td_set_param`, `td_connect_ops`, `td_op_info`) over `td_run_script`. Typed tools validate against the KB and produce structured responses; `td_run_script` is an escape hatch with no safety net.
5. **Use checkpoints for non-trivial mutations**: `td_checkpoint <comp_path> "<label>"` before, `td_rollback <id>` if the result is wrong. Wrap experiments in a baseCOMP first since rollback is COMP-scoped.
6. **Layout the network for clarity**: After all operators are created and connected, call `mcp__td-mcp__td_layout_network(path="<the parent>", mode="grid_annotated")` to reorganize the network into a readable grid with cluster annotations and semantic renames. This creates a checkpoint automatically; rollback via `td_rollback(checkpoint_id=...)` if needed.

## Hard rules

- Never write `op('X').par.Y = Z` from memory. Read the param via `td_op_info` first to confirm its name and current value.
- Never invent an op class name. If unsure, `kb_get_operator query="<your guess>"` and read the suggestions.
- For GLSL TOPs: always start from `mcp__td-mcp__kb_glsl_template` — never write the boilerplate yourself. The `TDOutputSwizzle`, `sTD2DInputs`, `uTDOutputInfo` conventions trip up every freshly-written shader.
- After mutating something visual, take a snapshot: `td_snapshot <top_path>` returns an Image you can actually see. Close the loop visually.
- The TD `app.version` returns "099" (legacy field); the real build is in `app.build` (e.g. "2025.32820"). Don't be confused.

## Operator family disambiguation

When the user says a bare name like "noise", "sphere", "math", "instance" — there are usually variants across CHOP/TOP/SOP/POP/COMP families. Call `kb_get_operator query="<bare name>"` to see all matches before picking. POP/SOP confusion in particular is the #1 source of wrong work in TD 2025+.

## When the catalog seems stale

If the user reports an op that should exist but isn't in the catalog, run `kb_refresh_operators_catalog` (re-introspects from the live TD). Most likely cause: TD was upgraded since the JSON was generated. The catalog stores `td_build` to detect drift.

## See also

- `touchdesigner-pops` — POP-specific protocol, auto-loads on POP mentions
- `touchdesigner-vibe` — vibe-loop protocol when reproducing visual references

## Common pitfalls

- `pixelshader` is not a param of glslTOP — the params are `pixeldat`, `vertexdat`, `computedat` (DAT paths). Put shader code in a Text DAT, point the param at it.
- Setting a param to `None` or `""` doesn't always reset to default. Use `op('X').par.Y = op('X').par.Y.default` via `td_run_script` if needed.
- `comp.save(file_path)` exports a `.tox` of the COMP only (not the whole project). `project.save()` saves the `.toe`. Both are exposed via `td_checkpoint` and `td_save_project` respectively.
- WebServer DAT in TD does not implement the WebSocket ping/pong protocol — the td-mcp bridge already works around this with `ping_interval=None`, but if you write your own WS client to TD you'll hit the same issue.
