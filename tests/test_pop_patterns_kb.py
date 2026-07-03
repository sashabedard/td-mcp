from pathlib import Path

from td_mcp.kb.operators import get_catalog
from td_mcp.kb.pop_patterns import POPPatternsKB, get_pop_kb


def test_seed_patterns_load():
    kb = get_pop_kb()
    ids = {p.id for p in kb.patterns}
    assert "point_cloud_starter" in ids
    assert "noise_displaced_grid" in ids


def test_every_pattern_uses_known_op_types():
    """Patterns reference op_type strings — those must exist in the
    operators catalog, otherwise the pattern can't be executed without
    KB-validation failure."""
    pop_kb = get_pop_kb()
    op_catalog = get_catalog()
    for pat in pop_kb.patterns:
        for step in pat.ops:
            assert op_catalog.get(step.op_type) is not None, (
                f"Pattern {pat.id!r} references unknown op_type {step.op_type!r}"
            )


def test_every_connection_references_local_op_name():
    """connections.out and connections.into must match a name in ops[]."""
    pop_kb = get_pop_kb()
    for pat in pop_kb.patterns:
        local_names = {step.name for step in pat.ops}
        for conn in pat.connections:
            assert conn.out in local_names, (
                f"Pattern {pat.id!r}: connection.out {conn.out!r} not in ops"
            )
            assert conn.into in local_names, (
                f"Pattern {pat.id!r}: connection.into {conn.into!r} not in ops"
            )


def test_seed_patterns_have_verification_metadata():
    pop_kb = get_pop_kb()
    for pat in pop_kb.patterns:
        assert pat.verified_on_build, (
            f"Pattern {pat.id!r} missing verified_on_build — patterns must be "
            "live-verified before shipping"
        )


def test_index_shape():
    kb = get_pop_kb()
    idx = kb.index()
    assert all({"id", "name", "description", "difficulty", "op_count"} <= set(e) for e in idx)


def test_by_tag_filters():
    kb = get_pop_kb()
    starters = kb.by_tag("starter")
    assert len(starters) >= 1
    assert all("starter" in p.tags for p in starters)


def test_load_missing_returns_empty(tmp_path: Path):
    kb = POPPatternsKB.load(tmp_path / "nope.json")
    assert kb.patterns == []


def test_get_unknown_returns_none():
    kb = get_pop_kb()
    assert kb.get("nope") is None
