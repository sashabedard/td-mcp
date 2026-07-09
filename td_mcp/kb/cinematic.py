"""Cinematic look recipes — typed, hand-curated.

Lookup is by Literal `look` key. Pydantic validation rejects unknown
looks mechanically — anti-amnésie lever (model can't invent recipes).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

_DATA_PATH = Path(__file__).parent / "data" / "cinematic_recipes.json"

CinematicLook = Literal[
    "dof_shallow",
    "dof_rack_focus",
    "lumablur_soft",
    "lumablur_bloom",
    "anamorphic_flare",
    "filmic_grade",
    "volumetric_god_rays",
    "motion_blur_velocity",
    "chromatic_aberration_subtle",
    "film_grain_clean",
]


class OperatorStep(BaseModel):
    op_type: str
    role: str
    notes: str = ""
    # Per-step values — takes precedence over param_values[op_type] when
    # applying the recipe. Needed whenever a chain uses the same op class
    # twice with different settings (e.g. chromatic aberration's two
    # transformTOPs with OPPOSITE offsets), which the class-keyed
    # param_values dict cannot express.
    params: dict[str, float | int | str | bool] = {}


class CinematicRecipe(BaseModel):
    look: CinematicLook
    operator_chain: list[OperatorStep]
    # Class-keyed defaults; ambiguous when a class appears twice in the
    # chain — per-step `params` wins in that case.
    param_values: dict[str, dict[str, float | int | str | bool]]
    common_pitfalls: list[str] = []
    example_screenshot_url: str | None = None


class CinematicKB:
    def __init__(self, recipes: list[CinematicRecipe]):
        self._by_look = {r.look: r for r in recipes}

    @classmethod
    def load(cls, path: Path = _DATA_PATH) -> "CinematicKB":
        if not path.exists():
            return cls([])
        data = json.loads(path.read_text())
        return cls([CinematicRecipe.model_validate(r) for r in data.get("recipes", [])])

    def get(self, look: str) -> CinematicRecipe | None:
        return self._by_look.get(look)

    def list_looks(self) -> list[str]:
        return list(self._by_look.keys())


_kb: CinematicKB | None = None


def get_cinematic_kb() -> CinematicKB:
    global _kb
    if _kb is None:
        _kb = CinematicKB.load()
    return _kb
