"""POP patterns sub-KB — Phase 3.6 scaffold.

Curated recipes for common POP workflows (generators, attribute modifiers,
cross-family conversions, particle sims). Each pattern describes a small
network as a sequence of ops + connections, with local names that the
caller resolves to full paths under a wrapper COMP.

This file ships a small, conservative set verified live against TD 2025.
The full target of 15-25 patterns requires real POP curation by the user
based on the workflows they actually do — POPs are too new for any one
person (especially the model) to author the canonical pattern library
from memory.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

_DATA_PATH = Path(__file__).parent / "data" / "pop_patterns.json"

Difficulty = Literal["easy", "medium", "hard"]


class POPOpStep(BaseModel):
    name: str  # local name within the pattern
    op_type: str  # full POP class name (validated against operators catalog)
    params: dict = {}  # {param_name: value} to set after creation


class POPConnectStep(BaseModel):
    out: str  # local source op name
    into: str  # local destination op name
    out_index: int = 0
    in_index: int = 0


class POPPattern(BaseModel):
    id: str
    name: str
    description: str
    tags: list[str] = []
    difficulty: Difficulty = "easy"
    ops: list[POPOpStep]
    connections: list[POPConnectStep] = []
    notes: str = ""
    pitfalls: list[str] = []
    references: list[str] = []
    verified_on_build: str = ""  # TD build string this pattern was tested against


class POPPatternsKB:
    def __init__(self, patterns: list[POPPattern]):
        self.patterns = patterns
        self._by_id = {p.id: p for p in patterns}

    @classmethod
    def load(cls, path: Path = _DATA_PATH) -> "POPPatternsKB":
        if not path.exists():
            return cls([])
        data = json.loads(path.read_text())
        return cls([POPPattern.model_validate(p) for p in data.get("patterns", [])])

    def get(self, pattern_id: str) -> POPPattern | None:
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

    def by_tag(self, tag: str) -> list[POPPattern]:
        return [p for p in self.patterns if tag in p.tags]


_kb: POPPatternsKB | None = None


def get_pop_kb() -> POPPatternsKB:
    global _kb
    if _kb is None:
        _kb = POPPatternsKB.load()
    return _kb


def reset_pop_kb_singleton() -> None:
    """Drop the cached singleton so the next get_pop_kb() re-reads the JSON
    (needed after kb_promote_pop_pattern appends a pattern at runtime)."""
    global _kb
    _kb = None
