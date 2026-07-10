import json
from pathlib import Path

import pytest

from td_mcp.kb.operators import OperatorEntry, OperatorsCatalog, ParamEntry


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


# ─────────────────────────── param enrichment ───────────────────────────


@pytest.fixture
def enriched_catalog() -> OperatorsCatalog:
    return OperatorsCatalog(
        [
            OperatorEntry(
                python_class="selectPOP",
                family="POP",
                subtype="select",
                params=[
                    ParamEntry(name="pop", label="POP", style="OP"),
                    ParamEntry(name="pointattrscope", label="Point Attribute Scope", style="Str"),
                ],
            ),
            OperatorEntry(
                python_class="noisePOP",
                family="POP",
                subtype="noise",
                params=[
                    ParamEntry(
                        name="combineentity",
                        label="Combine Entity",
                        style="Menu",
                        menu_names=["noise", "gradient", "curl3d", "curl2d"],
                    ),
                    ParamEntry(name="period", label="Period", style="Float"),
                ],
            ),
            OperatorEntry(python_class="bareCHOP", family="CHOP", subtype="bare"),
        ],
        td_build="2025.test",
    )


def test_params_roundtrip_save_load(tmp_path: Path, enriched_catalog: OperatorsCatalog):
    path = tmp_path / "operators.json"
    enriched_catalog.save(path)
    loaded = OperatorsCatalog.load(path)

    noise = loaded.get("noisePOP")
    combineentity = next(p for p in noise.params if p.name == "combineentity")
    assert combineentity.menu_names == ["noise", "gradient", "curl3d", "curl2d"]
    assert combineentity.label == "Combine Entity"
    # Non-enriched entries survive the roundtrip with empty params
    assert loaded.get("bareCHOP").params == []


def test_save_excludes_defaults_for_lean_file(tmp_path: Path, enriched_catalog: OperatorsCatalog):
    path = tmp_path / "operators.json"
    enriched_catalog.save(path)
    payload = json.loads(path.read_text())
    bare = next(o for o in payload["operators"] if o["python_class"] == "bareCHOP")
    assert "params" not in bare  # empty default not serialized
    period = next(
        p
        for o in payload["operators"]
        if o["python_class"] == "noisePOP"
        for p in o["params"]
        if p["name"] == "period"
    )
    assert "menu_names" not in period  # non-menu param stays lean


def test_suggest_params_catches_typo(enriched_catalog: OperatorsCatalog):
    # The exact miss from the vortex session: guessing 'pops' for selectPOP
    assert "pop" in enriched_catalog.suggest_params("selectPOP", "pops")


def test_suggest_params_unknown_class_or_unenriched(enriched_catalog: OperatorsCatalog):
    assert enriched_catalog.suggest_params("nopeTOP", "x") == []
    assert enriched_catalog.suggest_params("bareCHOP", "x") == []


def test_param_entry_tolerates_none_label_and_style():
    """TD returns None for label/style on some params (header rows,
    python-only pars) — a single None killed whole catalog refreshes."""
    p = ParamEntry.model_validate({"name": "x", "label": None, "style": None})
    assert p.label == ""
    assert p.style == ""
