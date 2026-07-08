"""Every operator class referenced by a curated KB must exist in the
operators catalog — these KBs exist precisely to prevent hallucinated op
names, so a phantom class here defeats their purpose (and gets rejected
by td_create_op's catalog gate at build time)."""
import json
from pathlib import Path

DATA = Path(__file__).parent.parent.parent / "td_mcp" / "kb" / "data"


def _catalog_classes() -> set[str]:
    ops = json.loads((DATA / "operators.json").read_text())["operators"]
    return {e["python_class"] for e in ops}


def test_vj_loop_patterns_key_operators_exist_in_catalog():
    catalog = _catalog_classes()
    data = json.loads((DATA / "vj_loop_patterns.json").read_text())
    phantoms = [
        (pat["pattern_name"], op)
        for pat in data["patterns"]
        for op in pat["key_operators"]
        if op not in catalog
    ]
    assert not phantoms, f"phantom operator classes: {phantoms}"


def test_cinematic_recipes_ops_exist_in_catalog():
    catalog = _catalog_classes()
    data = json.loads((DATA / "cinematic_recipes.json").read_text())
    phantoms = []
    for recipe in data["recipes"]:
        for step in recipe["operator_chain"]:
            if step["op_type"] not in catalog:
                phantoms.append((recipe["look"], "chain", step["op_type"]))
        for op in recipe.get("param_values", {}):
            if op not in catalog:
                phantoms.append((recipe["look"], "param_values", op))
    assert not phantoms, f"phantom operator classes: {phantoms}"


def test_pop_patterns_ops_exist_in_catalog():
    catalog = _catalog_classes()
    data = json.loads((DATA / "pop_patterns.json").read_text())
    phantoms = [
        (pat["id"], o["op_type"])
        for pat in data["patterns"]
        for o in pat["ops"]
        if o["op_type"] not in catalog
    ]
    assert not phantoms, f"phantom operator classes: {phantoms}"
