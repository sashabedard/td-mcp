from td_mcp.kb.operators import get_catalog
from td_mcp.kb.top_patterns import get_top_kb


def test_seed_patterns_load():
    kb = get_top_kb()
    ids = {p.id for p in kb.patterns}
    assert "poisson_jacobi_convolve" in ids
    assert "semilagrangian_advect_packed_rgba" in ids


def test_every_pattern_uses_known_op_types():
    """op_type strings must exist in the operators catalog, otherwise the
    pattern cannot be executed without KB-validation failure."""
    op_catalog = get_catalog()
    for pat in get_top_kb().patterns:
        for step in pat.ops:
            assert op_catalog.get(step.op_type) is not None, (
                f"Pattern {pat.id!r} references unknown op_type {step.op_type!r}"
            )


def test_every_connection_references_local_op_name():
    for pat in get_top_kb().patterns:
        local_names = {step.name for step in pat.ops}
        for conn in pat.connections:
            assert conn.out in local_names, (
                f"Pattern {pat.id!r}: connection.out {conn.out!r} not in ops"
            )
            assert conn.into in local_names, (
                f"Pattern {pat.id!r}: connection.into {conn.into!r} not in ops"
            )


def test_every_pattern_has_at_least_one_top():
    """The promotion gate mirrors kb_promote_pop_pattern: this KB curates
    TOP-family workflows."""
    for pat in get_top_kb().patterns:
        assert any(s.op_type.endswith("TOP") for s in pat.ops), pat.id


def test_patterns_carry_pitfalls_and_build():
    """The pitfalls are the expensive part — a pattern without them is a
    recipe the next session will still have to debug from scratch."""
    for pat in get_top_kb().patterns:
        assert pat.pitfalls, f"{pat.id!r} has no pitfalls"
        assert pat.verified_on_build, f"{pat.id!r} has no verified_on_build"
