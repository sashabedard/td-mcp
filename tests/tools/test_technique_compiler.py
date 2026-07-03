"""Pure-logic tests for the technique compiler (no TD required)."""


def test_resolve_instance_by_stem():
    from td_mcp.tools.technique_compiler import resolve_instance
    classes = ["noisePOP", "mathcombinePOP", "rayPOP", "spherePOP"]
    assert resolve_instance("noise3", classes) == "noisePOP"
    assert resolve_instance("mathcombine2", classes) == "mathcombinePOP"
    assert resolve_instance("rayPOP", classes) == "rayPOP"  # déjà une classe
    assert resolve_instance("banana7", classes) is None


def test_resolve_instance_ambiguous_returns_none():
    from td_mcp.tools.technique_compiler import resolve_instance
    # noiseTOP et noisePOP dans la même extraction → stem 'noise' ambigu
    assert resolve_instance("noise1", ["noiseTOP", "noisePOP"]) is None


def test_build_plan_filters_catalog_and_reports_unresolved():
    from td_mcp.tools.technique_compiler import build_plan
    extraction = {
        "operators": ["noisePOP", "rayPOP", "inventedPOP"],
        "connections": [
            {"source": "noise1", "target": "ray1"},
            {"source": "ghost2", "target": "ray1"},
        ],
        "parameters": [
            {"operator": "ray1", "parameter": "Hit Normal", "value": "On"},
        ],
    }
    plan = build_plan(extraction, catalog_classes={"noisePOP", "rayPOP"})
    created = dict(plan.creates)
    assert created == {"noise1": "noisePOP", "ray1": "rayPOP"}  # inventedPOP filtré
    assert plan.connections == [("noise1", "ray1")]
    assert ("ghost2", "ray1") in plan.dropped_connections
    assert "ghost2" in plan.unresolved_instances
    assert plan.params == [("ray1", "Hit Normal", "On")]


def test_build_plan_instantiates_unreferenced_classes():
    from td_mcp.tools.technique_compiler import build_plan
    extraction = {"operators": ["trailPOP"], "connections": [], "parameters": []}
    plan = build_plan(extraction, catalog_classes={"trailPOP"})
    assert plan.creates == [("trail1", "trailPOP")]


def test_compile_script_is_flat_and_reports():
    from td_mcp.tools.technique_compiler import BuildPlan, compile_script
    plan = BuildPlan(
        creates=[("noise1", "noisePOP"), ("ray1", "rayPOP")],
        connections=[("noise1", "ray1")],
        params=[("ray1", "Hit Normal", "On"), ("noise1", "Translate", "0 1 0")],
    )
    script = compile_script(plan, "/project1", "compile_test")
    # pas de def imbriqué (gotcha des namespaces exec du bridge)
    assert "def " not in script
    assert "_w.create(noisePOP, 'noise1')" in script
    assert "verified_ops" in script
    compile(script, "<test>", "exec")  # syntaxe valide
