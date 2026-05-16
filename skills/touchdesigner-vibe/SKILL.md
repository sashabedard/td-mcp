---
name: touchdesigner-vibe
description: Use when reproducing a visual reference (image, video, description, or stated intent) in TouchDesigner. Triggers when the user supplies a reference and asks for a TD reproduction, says "make it look like", "reproduce this", "vibe", "like in <artist/post>", or otherwise frames the task as iterative visual matching rather than rigorous engineering.
version: 0.1.0
---

# Vibe loop protocol

Vibe mode is short-loop iteration with visual feedback. Speed and approximation matter more than rigor; the user judges by looking at snapshots, not by spec compliance.

## Protocol

1. **Identify the technique** from the reference in 1-2 sentences: "this looks like X feedback loop + Y noise displacement" or "this is a procedural fractal in a GLSL TOP". State your guess explicitly so the user can redirect immediately if you've misread.
2. **Wrap your experiment in a baseCOMP** so you can checkpoint cleanly: `mcp__td-mcp__td_create_op op_type="baseCOMP" name="vibe_<n>" parent="/project1"`. Work inside `/project1/vibe_<n>`.
3. **Checkpoint before each non-trivial attempt**: `td_checkpoint /project1/vibe_<n> "<short description of what you're about to try>"`. Cheap insurance.
4. **Iterate in short cycles**:
   - Mutate (create op / set param / change wire)
   - `td_snapshot <render_top>` to see the result
   - If it has drifted from the reference target, `td_rollback <last_good_checkpoint_id>` immediately — don't try to "fix" a wrong direction
   - If 3 iterations pass without convergence, **change technique entirely** rather than grinding on the wrong approach
5. **Stop when**:
   - The snapshot matches the reference well enough — stop, optionally `td_save_project`
   - 5 iterations with no convergence — stop and re-describe what's missing, or switch to the general `touchdesigner` skill for a rigorous attempt
   - The user redirects — stop immediately, don't argue

## What NOT to do in vibe mode

- Don't propose a full typed plan before trying anything — that's the rigorous mode (Phase 6b, not yet shipped). Vibe is about getting close fast and iterating.
- Don't validate every param exhaustively before mutating — the snapshot will tell you if the param was wrong.
- Don't try to reproduce a reference pixel-perfect — assume that's not the goal unless the user explicitly says so. Approximation is the deliverable.

## Memory across iterations

- Checkpoints survive on disk (FIFO at 20 per session).
- The snapshot history is in your conversation — refer back to "in snapshot 3 the noise was too coarse, snapshot 5 fixed the scale but lost the contrast".
- If `vibe_journal_entry` becomes available (Phase 6a), use it. Otherwise narrate your iteration trajectory briefly so the conversation log carries the trace for the research-creation archive.
