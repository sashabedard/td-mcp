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

## Mandatory visual validation after any build

A build is NOT done when the last operator is wired — it is done when a snapshot proves it renders. After completing any network that produces visual output:

1. `td_snapshot` the FINAL output TOP. A flat/uniform/empty frame means something upstream is broken — do not report success.
2. If the frame is wrong, bisect upstream: snapshot the render TOP, then intermediate TOPs, until you find the last good stage.
3. The most common silent killers, in observed order: **camera framing** (never set camera transforms blind — pull back on Z looking at origin, snapshot, then adjust), **displace/feedback weights left at 0** (the effect silently does nothing), **fit/resolution mismatches** (content cropped out of frame), **audio inputs with no signal** (reactivity bindings exist but drive constants — sample the CHOP value and say explicitly that reactivity is unverified if it reads 0).
4. Iterate mutate → snapshot → judge until the output visibly matches the intent. Report what the final snapshot shows, not what the network should do.

Building 36 correct operators with one bad camera transform produces a black screen and reads as total failure to the user. The visual loop is what separates "wired" from "working".

## Mandatory network hygiene after any build

A network the user cannot read is a network the user cannot maintain. After the visual validation passes (never before — layout moves nodes, validate content first):

1. Run `td_layout_network <parent> mode="grid_annotated"` on the COMP you built. It arranges ops on a topological grid (columns by depth, rows by family), wraps detected clusters (audio chain, render chain, feedback loops...) in labeled Annotate COMPs, and renames generic names (`null1`, `math3`) to semantic ones (`null_audioRMS`). It checkpoints itself — the returned diff includes the checkpoint id for rollback.
2. Review the returned renames: if a rename is wrong or a cluster label is misleading, fix it with `td_set_param` rather than accepting noise.
3. For anything the cluster detection cannot know — WHY a magic value was chosen, what an expression binding expects (e.g. "birthrate pulses on kick: play audio into audiodevin"), which op is the intended output — add a short note: a `textDAT` named `README` inside the COMP, or a comment on the relevant Annotate COMP.
4. Never deliver a build where the user has to reverse-engineer `math3 → null7 → level12` to understand their own project. Organized + annotated is part of "done", same as rendering.

## Hard rules

- Never write `op('X').par.Y = Z` from memory. Read the param via `td_op_info` first to confirm its name and current value.
- Never invent an op class name. If unsure, `kb_get_operator query="<your guess>"` and read the suggestions.
- **Nodes first, GLSL last.** Writing a shader to avoid wiring operators is an anti-pattern: node networks are what the KB validates, what the user can read and tweak in the editor, and what POPs/TOPs already run on the GPU. Reach for a GLSL TOP only when the effect is genuinely shader-shaped (raymarching, custom per-pixel math with no operator equivalent) or a measured perf wall demands it — and say so explicitly before writing it. If a kb_search for the technique returns a node recipe, build the nodes.
- For GLSL TOPs (when justified): always start from `mcp__td-mcp__kb_glsl_template` — never write the boilerplate yourself. The `TDOutputSwizzle`, `sTD2DInputs`, `uTDOutputInfo` conventions trip up every freshly-written shader. After pointing `pixeldat` at your Text DAT, read the glslTOP's compile status (`td_op_info` warnings/errors or `op.errors()` via td_expr) — a shader that silently outputs black is the GLSL equivalent of the unvalidated build.
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
