"""Operators catalog — Phase 3 amorce.

Source of truth for typed validation in td_create_op. The catalog is
introspected directly from a running TD instance via dir(td), filtered
by the convention that creatable op classes start with a lowercase letter
(ObjectCOMP, PanelCOMP, etc. are abstract bases and are excluded).

Rich metadata (params, descriptions, examples) lands in Phase 3.6+ via
manual curation or wiki scraping. For now the catalog only knows: which
python_class names exist, and which family they belong to. That's enough
to mechanically reject hallucinated op names — the #1 lever from spec §10.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

OpFamily = Literal["CHOP", "TOP", "SOP", "DAT", "COMP", "MAT", "POP"]

_CATALOG_PATH = Path(__file__).parent / "data" / "operators.json"


class ParamEntry(BaseModel):
    """One settable parameter, introspected from a live instance.

    Captures exactly what an agent needs to call td_set_param without a
    td_op_info roundtrip: the internal name (case-sensitive), the display
    label (what tutorials say aloud), the style, and menu tokens (menu
    params reject anything not in this list).
    """
    name: str
    label: str = ""
    style: str = ""
    menu_names: list[str] = []


class OperatorEntry(BaseModel):
    python_class: str
    family: OpFamily
    subtype: str = ""

    # Introspected from a live instance by kb_refresh_operators_catalog
    # (include_params=True). Empty on catalogs built before enrichment.
    params: list[ParamEntry] = []

    # Reserved for Phase 3.6+ enrichment from wiki / Op Snippets:
    name: str = ""
    description: str = ""
    version_added: str = ""


class OperatorsCatalog:
    def __init__(self, entries: list[OperatorEntry] | None = None, td_build: str = ""):
        self._entries = entries or []
        self._by_class = {e.python_class: e for e in self._entries}
        self.td_build = td_build

    @classmethod
    def load(cls, path: Path = _CATALOG_PATH) -> "OperatorsCatalog":
        if not path.exists():
            return cls([])
        data = json.loads(path.read_text())
        entries = [OperatorEntry.model_validate(e) for e in data.get("operators", [])]
        return cls(entries, td_build=data.get("td_build", ""))

    def save(self, path: Path = _CATALOG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        by_family: dict[str, int] = {}
        for e in self._entries:
            by_family[e.family] = by_family.get(e.family, 0) + 1
        payload = {
            "version": "1.1",
            "td_build": self.td_build,
            "count": len(self._entries),
            "by_family": by_family,
            # exclude_defaults keeps the file lean: param-less entries and
            # empty menu lists serialize to nothing instead of noise. With
            # full param enrichment the file is a few MB — acceptable for a
            # local source of truth that kills live introspection roundtrips.
            "operators": [e.model_dump(exclude_defaults=True) for e in self._entries],
        }
        path.write_text(json.dumps(payload, indent=2))

    def get(self, python_class: str) -> OperatorEntry | None:
        return self._by_class.get(python_class)

    def suggest_params(self, python_class: str, param: str, n: int = 5) -> list[str]:
        """Close matches for a misspelled param name on a known op class.
        Empty when the class is unknown or the catalog predates enrichment."""
        entry = self._by_class.get(python_class)
        if entry is None or not entry.params:
            return []
        return difflib.get_close_matches(
            param, [p.name for p in entry.params], n=n, cutoff=0.4
        )

    def list(self, family: OpFamily | None = None) -> list[OperatorEntry]:
        if family is None:
            return list(self._entries)
        return [e for e in self._entries if e.family == family]

    def suggest(self, query: str, n: int = 5) -> list[str]:
        # cutoff=0.4 is permissive enough to catch likely typos (e.g. "noisechop"
        # → "noiseCHOP") and family confusion ("noiseSOP" → "noiseCHOP" if SOP
        # variant doesn't exist), while still filtering out random strings.
        return difflib.get_close_matches(query, self._by_class.keys(), n=n, cutoff=0.4)

    @property
    def is_empty(self) -> bool:
        return len(self._entries) == 0

    @property
    def count(self) -> int:
        return len(self._entries)

    def family_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self._entries:
            counts[e.family] = counts.get(e.family, 0) + 1
        return counts


# Module-level singleton — loaded lazily so import-time failure doesn't break
# the whole MCP server if the catalog file is missing or corrupted.
_catalog: OperatorsCatalog | None = None


def get_catalog() -> OperatorsCatalog:
    global _catalog
    if _catalog is None:
        _catalog = OperatorsCatalog.load()
    return _catalog


def reload_catalog() -> OperatorsCatalog:
    global _catalog
    _catalog = OperatorsCatalog.load()
    return _catalog
