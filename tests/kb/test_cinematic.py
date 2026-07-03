from td_mcp.kb.cinematic import CinematicRecipe, get_cinematic_kb


def test_recipe_schema_validates():
    recipe = CinematicRecipe(
        look="dof_shallow",
        operator_chain=[
            {"op_type": "cameraCOMP", "role": "camera", "notes": "focalDistance drives focus"},
            {"op_type": "renderTOP", "role": "render", "notes": "enable Depth in Render TOP"},
        ],
        param_values={
            "cameraCOMP": {"focallength": 50.0, "focaldist": 4.0, "fstop": 1.4},
            "renderTOP": {"depth": True},
        },
        common_pitfalls=["focaldist must be in scene units, not pixels"],
        example_screenshot_url=None,
    )
    assert recipe.look == "dof_shallow"
    assert len(recipe.operator_chain) == 2


def test_kb_lookup_by_look_literal():
    kb = get_cinematic_kb()
    recipe = kb.get("dof_shallow")
    assert recipe is not None
    assert recipe.look == "dof_shallow"


def test_kb_unknown_look_returns_none():
    kb = get_cinematic_kb()
    assert kb.get("dof_shallow_typo_zzz") is None


import pytest


@pytest.mark.asyncio
async def test_kb_get_cinematic_recipe_returns_known_look():
    from td_mcp.server import kb_get_cinematic_recipe
    result = await kb_get_cinematic_recipe(look="dof_shallow")
    assert result["ok"] is True
    assert result["recipe"]["look"] == "dof_shallow"


@pytest.mark.asyncio
async def test_kb_get_cinematic_recipe_unknown_look():
    from td_mcp.server import kb_get_cinematic_recipe
    result = await kb_get_cinematic_recipe(look="dof_shallow_typo_zzz")
    assert result["ok"] is False
    assert "unknown" in result["error"].lower()
    assert "available" in result


def test_all_literal_looks_have_recipes():
    from typing import get_args
    from td_mcp.kb.cinematic import CinematicLook, get_cinematic_kb
    kb = get_cinematic_kb()
    for look in get_args(CinematicLook):
        assert kb.get(look) is not None, f"missing recipe for {look}"
