"""Network layout / cluster detection / rename — pure logic.

Separated from server.py so the algorithms are unit-testable without
the TD bridge. The MCP tool in server.py orchestrates: bridge fetch
network → call these functions → bridge apply diff.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

# Families ordered top-to-bottom in the row layout.
FAMILY_ROW_ORDER = ["CHOP", "DAT", "TOP", "MAT", "SOP", "POP", "COMP"]

GENERIC_NAME_RE = re.compile(
    r"^(null|transform|select|switch|merge|out|in|composite|reorder|math|trail|filter|delay|noise|constant|level|blur)(\d+)?$"
)


def assign_columns_by_depth(
    ops: list[str], edges: list[tuple[str, str]]
) -> dict[str, int]:
    """Longest-path depth from each source. Disconnected ops get column 0."""
    incoming = defaultdict(list)
    outgoing = defaultdict(list)
    for src, dst in edges:
        outgoing[src].append(dst)
        incoming[dst].append(src)

    depth: dict[str, int] = {}

    def compute(op: str, stack: set[str]) -> int:
        if op in depth:
            return depth[op]
        if op in stack:
            return 0  # cycle break — feedback loops are common in TD
        if not incoming[op]:
            depth[op] = 0
            return 0
        stack.add(op)
        d = 1 + max(compute(p, stack) for p in incoming[op])
        stack.remove(op)
        depth[op] = d
        return d

    for op in ops:
        compute(op, set())
    return depth


def geometric_layout(
    ops_meta: list[dict], col_width: int = 200, row_height: int = 150
) -> dict[str, tuple[int, int]]:
    """Assign (x, y) positions on a grid grouped by family, ordered by column.

    `ops_meta` items: {path, family, column}. Returns {path: (x, y)}.
    """
    by_family: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for op in ops_meta:
        by_family[op["family"]].append((op["column"], op["path"]))

    positions: dict[str, tuple[int, int]] = {}
    row_cursor = 0
    for family in FAMILY_ROW_ORDER:
        if family not in by_family:
            continue
        col_used_rows: dict[int, int] = defaultdict(int)
        for column, path in sorted(by_family[family]):
            sub_row = col_used_rows[column]
            col_used_rows[column] += 1
            x = column * col_width
            y = (row_cursor + sub_row) * row_height
            positions[path] = (x, y)
        row_cursor += max(col_used_rows.values()) if col_used_rows else 1
    return positions


def _has_type(ops_meta: list[dict], op_type: str) -> list[dict]:
    return [o for o in ops_meta if o["op_type"] == op_type]


def detect_clusters(
    ops_meta: list[dict], edges: list[tuple[str, str]]
) -> list[dict]:
    """Heuristic cluster detection. Each cluster: {name, members: [paths]}.

    Heuristics:
    - "Audio reactive": an audio-input CHOP plus every downstream CHOP
      reachable from it (BFS over edges) — real audio chains have several
      hops between input and analysis, a direct-edge test never fires
    - "Render chain": cameraCOMP + geometryCOMP + renderTOP all present
    - "Feedback loop": any feedbackTOP
    """
    clusters: list[dict] = []

    audio_ins = _has_type(ops_meta, "audiofileinCHOP") + _has_type(ops_meta, "audiodeviceinCHOP")
    if audio_ins:
        chop_paths = {o["path"] for o in ops_meta if o["op_type"].endswith("CHOP")}
        downstream: dict[str, list[str]] = {}
        for src, dst in edges:
            downstream.setdefault(src, []).append(dst)
        for a in audio_ins:
            members = [a["path"]]
            queue = [a["path"]]
            seen = {a["path"]}
            while queue:
                cur = queue.pop(0)
                for nxt in downstream.get(cur, []):
                    if nxt not in seen and nxt in chop_paths:
                        seen.add(nxt)
                        members.append(nxt)
                        queue.append(nxt)
            if len(members) >= 2:
                clusters.append({"name": "Audio reactive", "members": members})

    cams = _has_type(ops_meta, "cameraCOMP")
    geos = _has_type(ops_meta, "geometryCOMP")
    renders = _has_type(ops_meta, "renderTOP")
    if cams and geos and renders:
        clusters.append({
            "name": "Render chain",
            "members": [o["path"] for o in cams + geos + renders],
        })

    feedbacks = _has_type(ops_meta, "feedbackTOP")
    for f in feedbacks:
        clusters.append({"name": "Feedback loop", "members": [f["path"]]})

    return clusters


UPSTREAM_SUFFIXES = {
    "audiofileinCHOP": "audioRMS",
    "analyzeCHOP": "audioRMS",
    "audiodeviceinCHOP": "audioRMS",
    "cameraCOMP": "cameraOrbit",
    "lfoCHOP": "lfo",
    "noiseCHOP": "noise",
    "noiseTOP": "noiseTex",
    "feedbackTOP": "feedback",
}


def propose_rename(op: dict, upstream_types: Iterable[str]) -> str | None:
    """Suggest a new short name if the current one is generic.

    Returns None if the name should be kept (non-generic or no upstream hint).
    """
    name = op["path"].rsplit("/", 1)[-1]
    m = GENERIC_NAME_RE.match(name)
    if not m:
        return None
    base = m.group(1)
    for ut in upstream_types:
        suffix = UPSTREAM_SUFFIXES.get(ut)
        if suffix:
            return f"{base}_{suffix}"
    return None
