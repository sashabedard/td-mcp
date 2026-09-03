"""TOP patterns sub-KB — symmetric counterpart of pop_patterns.py.

Curated recipes for TOP-family workflows that are hard to rediscover:
numerical solvers, feedback architectures and volumetric rendering built
from node operators rather than GLSL. Each pattern describes a network as
a sequence of ops + connections, with local names that the caller resolves
to full paths under a wrapper COMP.

Same shape as pop_patterns.py on purpose — the only difference is the
family gate on promotion (at least one non-COMP TOP-side operator instead
of at least one POP). These patterns exist because the operator semantics
they rely on were measured live, not read: they are exactly the knowledge
that a fresh session would otherwise have to re-derive.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

_DATA_PATH = Path(__file__).parent / "data" / "top_patterns.json"

Difficulty = Literal["easy", "medium", "hard"]


class TOPOpStep(BaseModel):
    name: str  # local name within the pattern
    op_type: str  # full class name (validated against operators catalog)
    params: dict = {}  # {param_name: value} to set after creation


class TOPConnectStep(BaseModel):
    out: str  # local source op name
    into: str  # local destination op name
    out_index: int = 0
    in_index: int = 0


class TOPPattern(BaseModel):
    id: str
    name: str
    description: str
    tags: list[str] = []
    difficulty: Difficulty = "easy"
    ops: list[TOPOpStep]
    connections: list[TOPConnectStep] = []
    notes: str = ""
    pitfalls: list[str] = []
    references: list[str] = []
    verified_on_build: str = ""  # TD build string this pattern was tested against


class TOPPatternsKB:
    def __init__(self, patterns: list[TOPPattern]):
        self.patterns = patterns
        self._by_id = {p.id: p for p in patterns}

    @classmethod
    def load(cls, path: Path = _DATA_PATH) -> "TOPPatternsKB":
        if not path.exists():
            return cls([])
        data = json.loads(path.read_text())
        return cls([TOPPattern.model_validate(p) for p in data.get("patterns", [])])

    def get(self, pattern_id: str) -> TOPPattern | None:
        return self._by_id.get(pattern_id)

    def index(self) -> list[dict]:
        return [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "tags": p.tags,
                "difficulty": p.difficulty,
                "op_count": len(p.ops),
            }
            for p in self.patterns
        ]

    def by_tag(self, tag: str) -> list[TOPPattern]:
        return [p for p in self.patterns if tag in p.tags]


_kb: TOPPatternsKB | None = None


def get_top_kb() -> TOPPatternsKB:
    global _kb
    if _kb is None:
        _kb = TOPPatternsKB.load()
    return _kb


def reset_top_kb_singleton() -> None:
    """Drop the cached singleton so the next get_top_kb() re-reads the JSON
    (needed after kb_promote_top_pattern appends a pattern at runtime)."""
    global _kb
    _kb = None
