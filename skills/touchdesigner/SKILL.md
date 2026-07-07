---
name: touchdesigner
description: Use when working with TouchDesigner — .toe/.tox files, operator families (CHOP/TOP/SOP/DAT/COMP/MAT/POP), GLSL TOPs, audio-reactive/real-time graphics/projection-mapping/interactive-installation work explicitly in TD, or any of the td-mcp tools (td_create_op, td_set_param, kb_list_operators, ...). Activates on any mention of TouchDesigner operators, networks, or workflows.
version: 0.1.0
---

# TouchDesigner protocol

Your training data on TouchDesigner is outdated and frequently wrong about operator class names, parameter internals, the Python `td` module, and GLSL TOP conventions. POPs (released TD 2025) have near-zero coverage in your training. The td-mcp server provides typed, KB-validated tools that mechanically catch hallucinations — use them instead of guessing.

## Mandatory: plan from the KB before building — and stay critical

For any creative or technical build request, NO operator gets created before this sequence:

1. **Decompose the request into 2-6 named techniques.** One diluted kb_search over the whole request returns noise; per-technique searches hit (validated repeatedly). If the user names an artist or channel (Okamirufu, elekktronaut, paketa12...), search those exact terms — the vision corpus is indexed by channel and the KB knows *their* actual node vocabulary.
2. **Search the KB for EACH technique**: `kb_search` (with source/family filters), `kb_pop_pattern`, `kb_get_cinematic_recipe`, `kb_get_vj_loop_reference`. Note which chunk grounds which part.
   **Reproducing a specific tutorial?** `kb_get_tutorial video_id=...` (or `query=...` to find the id) returns EVERY chunk of the video, ordered — kb_search alone WILL miss segments, and one missing segment silently breaks a step-by-step rebuild. When a vision chunk and the raw transcript disagree on a value, **the transcript wins** — the vision pass misreads parameter panels (observed: "Alpha = 0.2, 2.9" fusing alpha and point size into one fake value).
3. **A gap is not a license to improvise.** A hole in the sequence (e.g. vision chunk 03 missing between 02 and 04) or an unknown config gets resolved by ESCALATING retrieval BEFORE building: `kb_get_tutorial` (full transcript included) → wiki → web. Building through a flagged gap costs a full build-diagnose-rebuild cycle (measured live: an improvised emitter shape produced a flat blob where the tutorial's tube+copy produced the vortex). Improvise only when escalation is exhausted — and say so.
4. **Register the plan with `td_plan` before the first td_create_op** (intention, stages each carrying `kb_source` + `confidence`, unresolved `gaps`, `success_criteria`), and state it to the user: per stage — the op chain, the KB source that grounds it, and its confidence tier: curated pattern > vision chunk (timestamped, shows real wiring) > wiki > transcript > improvisation. td_create_op warns on every create until a plan is registered (hard error with TD_MCP_REQUIRE_PLAN=1). Any stage resting on improvisation gets flagged to the user BEFORE building, not discovered after.
5. **Attack your own plan before executing it**: which stage is most likely to fail visually? Does each stage's output actually feed what the next needs (family, resolution, attribute names)? Fix the plan, not the wreckage.
6. **Escape hatches must be justified against the KB, in writing.** About to reach for a GLSL TOP/POP, a run_script, or an image/video input? Search the KB for the node-native equivalent FIRST and cite why it cannot work. "Cleanest / most controllable / simplest" is not a justification — it is the rationalization that precedes every KB bypass. When the user asked for procedural, file inputs are forbidden, period.
7. **Execute stage by stage, snapshot per stage.** Deviating from the plan is allowed — silently drifting is not: say what changed and why (re-call td_plan when scope shifts).

## Mandatory protocol before writing any TD code or proposing any operator

1. **Connect** if not already: call `mcp__td-mcp__td_connect` (default `ws://127.0.0.1:9988`).
2. **Inspect current state** with `mcp__td-mcp__td_get_network <parent>` before suggesting any change. Never assume what's in the project.
3. **Validate operator class names** via `mcp__td-mcp__kb_get_operator query="..."` or `kb_list_operators family="..."` BEFORE proposing them. The catalog is the source of truth — if `op_type` isn't in it, it doesn't exist or your spelling is wrong. With an enriched catalog, `kb_get_operator` also returns every parameter (internal name, label, style, menu tokens) — use it instead of create-then-td_op_info roundtrips to discover param names. If it answers `params_note` (catalog predates enrichment), run `kb_refresh_operators_catalog` once with TD connected (~10-60s).
4. **Prefer typed tools** (`td_create_op`, `td_set_param`, `td_connect_ops`, `td_op_info`) over `td_run_script`. Typed tools validate against the KB and produce structured responses; `td_run_script` is an escape hatch with no safety net.
5. **Use checkpoints for non-trivial mutations**: `td_checkpoint <comp_path> "<label>"` before, `td_rollback <id>` if the result is wrong. Wrap experiments in a baseCOMP first since rollback is COMP-scoped.

## Mandatory visual validation after any build

A build is NOT done when the last operator is wired — it is done when a snapshot proves it renders. After completing any network that produces visual output:

1. `td_snapshot` the FINAL output TOP. A flat/uniform/empty frame means something upstream is broken — do not report success.
2. If the frame is wrong, bisect upstream: snapshot the render TOP, then intermediate TOPs, until you find the last good stage.
3. The most common silent killers, in observed order: **camera framing** (never set camera transforms blind — pull back on Z looking at origin, snapshot, then adjust), **displace/feedback weights left at 0** (the effect silently does nothing), **fit/resolution mismatches** (content cropped out of frame), **audio inputs with no signal** (reactivity bindings exist but drive constants — sample the CHOP value and say explicitly that reactivity is unverified if it reads 0).
4. Iterate mutate → snapshot → judge until the output visibly matches the intent. Report what the final snapshot shows, not what the network should do.

Building 36 correct operators with one bad camera transform produces a black screen and reads as total failure to the user. The visual loop is what separates "wired" from "working".

## Cook budget protocol — protect the bridge, iterate in isolation

TD's WebServer DAT shares the main cook thread: a graph that cooks heavy
starves the bridge until every call times out, and only the USER's hands can
free it (pause timeline, kill the heavy node). Prevention is the only cure:

1. **Iterate in an isolated COMP with its own small render.** A new visual
   scene gets its own baseCOMP + its own renderTOP at ≤640px while iterating.
   Never grow the main graph (HUD, existing scenes) during exploration —
   compose the validated scene into the main output at the very END, at
   final resolution. Build and judge the flower alone; assemble last.
2. **Known bridge-killers, in observed order:** environmentlightCOMP (the
   IBL prefilter is a GPU wall — during iteration use 2 plain lights + an
   emissive ramp, or set Prefilter Quality to minimum), full-res renders,
   multi-light PBR, large blurs/bloom at full res, maxparticles in the
   hundreds of thousands. Add these at LOW quality first; crank quality
   only after the look is validated at small size.
3. **Freeze what you are not judging.** Scenes not under iteration get
   cooking disabled (`op('X').allowCooking = False` via td_expr/run_script,
   or bypass the render). Re-enable at composition time.
4. **When calls start timing out, STOP calling.** Hammering retries into a
   wedged bridge does nothing — the graph must decook first. Tell the user
   exactly what to do in TD (spacebar to pause the timeline, or disable the
   named heavy node) and wait. Then fix the architecture (points 1-3) before
   resuming, or the wedge returns on the next iteration.

## Mandatory network hygiene after any build

A network the user cannot read is a network the user cannot maintain. After the visual validation passes (never before — layout moves nodes, validate content first):

1. Run `td_layout_network <parent> mode="grid_annotated"` on the COMP you built. It arranges ops on a topological grid (columns by depth, rows by family), wraps detected clusters (audio chain, render chain, feedback loops...) in labeled Annotate COMPs, and renames generic names (`null1`, `math3`) to semantic ones (`null_audioRMS`). It checkpoints itself — the returned diff includes the checkpoint id for rollback.
2. Review the returned renames: if a rename is wrong or a cluster label is misleading, fix it with `td_set_param` rather than accepting noise.
3. For anything the cluster detection cannot know — WHY a magic value was chosen, what an expression binding expects (e.g. "birthrate pulses on kick: play audio into audiodevin"), which op is the intended output — add a short note: a `textDAT` named `README` inside the COMP, or a comment on the relevant Annotate COMP.
4. Never deliver a build where the user has to reverse-engineer `math3 → null7 → level12` to understand their own project. Organized + annotated is part of "done", same as rendering.

## Hard rules

- Never write `op('X').par.Y = Z` from memory. Confirm the param name via `kb_get_operator` (enriched catalog: names, labels, menu tokens — no TD roundtrip) or `td_op_info` (live current values). A failed `td_set_param` returns close-match suggestions — read them instead of guessing again.
- Never invent an op class name. If unsure, `kb_get_operator query="<your guess>"` and read the suggestions.
- **Nodes first, GLSL last.** Writing a shader to avoid wiring operators is an anti-pattern: node networks are what the KB validates, what the user can read and tweak in the editor, and what POPs/TOPs already run on the GPU. Reach for GLSL (TOP **or** POP) only when the effect is genuinely shader-shaped (raymarching, custom per-pixel math with no operator equivalent) or a measured perf wall demands it — and say so explicitly before writing it. If a kb_search for the technique returns a node recipe, build the nodes.
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
