"""VJ loop patterns sub-KB — curated recipes for common VJ aesthetics.

Stage 1: text-only patterns. Stage 2 (Task 12) attaches `visual_refs`
from the ingested CLIP-indexed corpus when available.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

_DATA_PATH = Path(__file__).parent / "data" / "vj_loop_patterns.json"

Energy = Literal["calm", "medium", "high", "frantic"]


class VisualRef(BaseModel):
    frame_path: str
    artist: str
    similarity: float


class VJLoopPattern(BaseModel):
    pattern_name: str
    tempo_bpm_range: tuple[int, int]
    energy: Energy
    palette: list[str]
    key_operators: list[str]
    glsl_hint: str | None = None
    description_fr: str
    tags: list[str] = []
    visual_refs: list[VisualRef] = []


class VJLoopsKB:
    def __init__(self, patterns: list[VJLoopPattern]):
        self.patterns = patterns

    @classmethod
    def load(cls, path: Path = _DATA_PATH) -> "VJLoopsKB":
        if not path.exists():
            return cls([])
        data = json.loads(path.read_text())
        return cls([VJLoopPattern.model_validate(p) for p in data.get("patterns", [])])

    def by_tag(self, tag: str) -> list[VJLoopPattern]:
        return [p for p in self.patterns if tag in p.tags]

    def search(self, query: str, top_k: int = 3, attach_visuals: bool = True) -> list[VJLoopPattern]:
        tokens = set(re.findall(r"\w+", query.lower()))
        if not tokens:
            results = self.patterns[:top_k]
        else:
            def score(p: VJLoopPattern) -> int:
                haystack = (
                    p.description_fr.lower()
                    + " "
                    + " ".join(p.tags).lower()
                    + " "
                    + p.pattern_name.lower()
                )
                haystack_tokens = set(re.findall(r"\w+", haystack))
                return len(tokens & haystack_tokens)
            ranked = sorted(self.patterns, key=score, reverse=True)
            results = [p for p in ranked if score(p) > 0][:top_k]

        if attach_visuals:
            results = [self._with_visuals(p) for p in results]
        return results

    def _with_visuals(self, pattern: VJLoopPattern, top_k_refs: int = 3) -> VJLoopPattern:
        """Attach visual_refs from the corpus matching pattern.energy.

        Stage 2 enhancement could use CLIP text encoder on description_fr
        for true cross-modal search; stage 1 uses energy tag as a proxy.
        Silently returns the pattern unchanged if the corpus table is empty
        or unavailable.
        """
        try:
            from td_mcp.kb.vj_corpus import open_table
            table = open_table()
            df = table.to_pandas()
            if df.empty:
                return pattern
            matches = df[df["energy"] == pattern.energy].head(top_k_refs)
            refs = [
                VisualRef(
                    frame_path=row["frame_path"],
                    artist=row["artist"],
                    similarity=1.0,
                )
                for _, row in matches.iterrows()
            ]
            return pattern.model_copy(update={"visual_refs": refs})
        except Exception:
            return pattern


_kb: VJLoopsKB | None = None


def get_vj_loops_kb() -> VJLoopsKB:
    global _kb
    if _kb is None:
        _kb = VJLoopsKB.load()
    return _kb
