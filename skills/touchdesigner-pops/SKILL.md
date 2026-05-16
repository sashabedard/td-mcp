---
name: touchdesigner-pops
description: Use when working with TouchDesigner POPs (Point Operators), point clouds, particle systems, or 3D point manipulation in TD. Triggers on "POP", "Point Operator", any *POP class name (spherePOP, noisePOP, instancePOP, attributePOP, accumulatePOP, ...), point clouds in TD, particle systems, or POP-driven workflows like LED control, instancing via grid, audio-reactive particles, mesh cloth, depth-TOP-to-points, curl noise fields.
version: 0.1.0
---

# POP-specific protocol

POPs were released in TouchDesigner 2025 and your training data has effectively zero coverage. Reasoning by analogy from SOPs WILL produce wrong code — POPs are attribute-driven, SOPs are vertex-driven, they use different mental models.

## Hard rules

- Never generate POP code from memory. Always start from `mcp__td-mcp__kb_list_operators family="POP"` to see what exists (101 POPs in TD 2025.32820 — you do not know them all by heart).
- Never reason "the SOP version probably works the same way" — verify with `kb_get_operator`. Some SOPs have POP cousins (`sphereSOP` / `spherePOP`), many don't.
- For ambiguous names ("sphere", "noise", "instance"), `mcp__td-mcp__kb_get_operator query="<bare name>"` returns close matches across families — pick consciously rather than defaulting to a family.
- Verify POP output visually: snapshot a `Render TOP` downstream of the POP chain via `mcp__td-mcp__td_snapshot`. Internal POP attribute state is NOT introspectable the same way CHOP samples are.

## Mental model — read this once per session

- POPs operate on **points** carrying **attributes** (P=position, N=normal, Cd=color, plus custom). Each POP either generates points, modifies attributes, or filters which points pass through.
- The closest mental analog is **Houdini SOPs** (Houdini's geometry pipeline, which TD's POPs are loosely inspired by), NOT TD's own SOPs.
- Inputs/outputs flow points + attributes. If a POP requires an attribute the upstream chain doesn't produce, it errors or silently no-ops. Verify required attributes before chaining.

## Asking "what's the POP equivalent of SOP X"

There is often no 1-to-1 mapping. The POP family is structured differently — some operations that take multiple SOPs collapse into a single POP, some SOP operations have no POP equivalent in TD 2025. Always `kb_get_operator` rather than assuming a name pattern.

## When the user provides a vague POP intent

"Make audio-reactive particles" — ask: what visual? Points jittering on RMS, mesh deforming on frequency band, or instanced geometry scaling? The technique varies enormously. Get a reference image/video before scaffolding.

## Checkpoint discipline for POP work

POP chains are easy to get wrong (wrong attribute, wrong family chained in, wrong order). Wrap any experiment in a baseCOMP and `td_checkpoint` before each meaningful step — POP debugging is much harder than CHOP/TOP because state is implicit in the point cloud.
