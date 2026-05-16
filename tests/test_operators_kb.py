import json
from pathlib import Path

import pytest

from td_mcp.kb.operators import OperatorEntry, OperatorsCatalog


@pytest.fixture
def sample_catalog() -> OperatorsCatalog:
    return OperatorsCatalog(
        [
            OperatorEntry(python_class="noiseCHOP", family="CHOP", subtype="noise"),
            OperatorEntry(python_class="noiseTOP", family="TOP", subtype="noise"),
            OperatorEntry(python_class="noiseSOP", family="SOP", subtype="noise"),
            OperatorEntry(python_class="noisePOP", family="POP", subtype="noise"),
            OperatorEntry(python_class="spherePOP", family="POP", subtype="sphere"),
            OperatorEntry(python_class="sphereSOP", family="SOP", subtype="sphere"),
            OperatorEntry(python_class="mathCHOP", family="CHOP", subtype="math"),
        ],
        td_build="2025.test",
    )


def test_get_returns_entry(sample_catalog):
    assert sample_catalog.get("noiseCHOP").family == "CHOP"
    assert sample_catalog.get("nonexistent") is None


def test_list_unfiltered(sample_catalog):
    assert len(sample_catalog.list()) == 7


def test_list_by_family(sample_catalog):
    pops = sample_catalog.list(family="POP")
    assert len(pops) == 2
    assert {e.python_class for e in pops} == {"noisePOP", "spherePOP"}


def test_suggest_catches_typo(sample_catalog):
    # Common case: lowercase suffix typo
    matches = sample_catalog.suggest("noisechop")
    assert "noiseCHOP" in matches


def test_suggest_cross_family(sample_catalog):
    # Critical for Phase 3.6 disambiguation: querying "sphere" should surface
    # both POP and SOP variants so the agent doesn't blindly pick one.
    matches = sample_catalog.suggest("sphere")
    assert "spherePOP" in matches
    assert "sphereSOP" in matches


def test_family_counts(sample_catalog):
    counts = sample_catalog.family_counts()
    assert counts == {"CHOP": 2, "TOP": 1, "SOP": 2, "POP": 2}


def test_roundtrip_save_load(tmp_path: Path, sample_catalog: OperatorsCatalog):
    path = tmp_path / "operators.json"
    sample_catalog.save(path)

    loaded = OperatorsCatalog.load(path)
    assert loaded.count == 7
    assert loaded.td_build == "2025.test"
    assert loaded.get("spherePOP").subtype == "sphere"

    payload = json.loads(path.read_text())
    assert payload["by_family"]["POP"] == 2


def test_load_missing_file_returns_empty(tmp_path: Path):
    loaded = OperatorsCatalog.load(tmp_path / "nope.json")
    assert loaded.is_empty
    assert loaded.count == 0


def test_empty_catalog_suggest_returns_empty():
    cat = OperatorsCatalog([])
    assert cat.suggest("anything") == []
