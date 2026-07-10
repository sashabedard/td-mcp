"""Palette discovery and resolution — pure filesystem logic, no bridge.

TD ships a curated component library (app.paletteFolder, ~280 .tox) and
users grow their own (app.userPaletteFolder). These helpers walk both
roots and resolve a caller-supplied identifier to exactly one .tox on
disk; the actual instantiation happens TD-side via the load_tox bridge
action (COMP.loadTox).

Same-machine trust model as checkpoints: the MCP server walks the folders
directly, only the roots come from the live TD (app.* attributes).
"""
from __future__ import annotations

import difflib
from pathlib import Path

PALETTE_SOURCES = ("builtin", "user")


def scan_palette(root: str | Path, source: str) -> list[dict]:
    """All .tox under `root`, recursively. Missing root → empty list
    (a fresh machine has no user palette yet — that's not an error)."""
    root = Path(root)
    if not root.is_dir():
        return []
    entries = []
    for p in sorted(root.rglob("*.tox")):
        rel = p.relative_to(root).as_posix()
        entries.append({
            "name": p.stem,
            "relpath": rel,
            "source": source,
            "size_kb": round(p.stat().st_size / 1024, 1),
        })
    return entries


def filter_palette(entries: list[dict], query: str = "") -> list[dict]:
    if not query:
        return entries
    q = query.lower()
    return [e for e in entries if q in e["name"].lower() or q in e["relpath"].lower()]


def _key(entry: dict) -> str:
    return f"{entry['source']}:{entry['relpath']}"


def resolve_tox(identifier: str, entries: list[dict]) -> tuple[dict | None, list[str]]:
    """Resolve `identifier` to exactly one palette entry.

    Accepted forms, most to least specific:
    - 'user:Tools/thing.tox' / 'builtin:Tools/thing' (source-qualified)
    - 'Tools/thing.tox' / 'Tools/thing' (relpath, .tox optional)
    - 'thing' (bare component name)

    Returns (entry, []) on a unique hit, (None, suggestions) otherwise —
    ambiguity (same name in both palettes) lists the qualified candidates
    so the caller can retry with a source prefix.
    """
    ident = identifier.strip()
    pool = entries
    for src in PALETTE_SOURCES:
        prefix = src + ":"
        if ident.lower().startswith(prefix):
            pool = [e for e in entries if e["source"] == src]
            ident = ident[len(prefix):]
            break

    def norm(s: str) -> str:
        return s.lower().removesuffix(".tox")

    ident_n = norm(ident)
    if "/" in ident_n:
        hits = [e for e in pool if norm(e["relpath"]) == ident_n]
    else:
        # Bare names match by stem ONLY — a root-level 'Grid.tox' must not
        # shadow a same-named component elsewhere; collisions surface below.
        hits = [e for e in pool if e["name"].lower() == ident_n]
    if len(hits) == 1:
        return hits[0], []
    if len(hits) > 1:
        return None, sorted(_key(e) for e in hits)

    candidates = {norm(e["relpath"]): e for e in pool}
    candidates.update({e["name"].lower(): e for e in pool})
    close = difflib.get_close_matches(ident_n, list(candidates), n=5, cutoff=0.5)
    return None, [_key(candidates[c]) for c in close]
