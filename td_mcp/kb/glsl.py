"""GLSL TOP templates and reference — Phase 3.7.

TD's GLSL TOP has a fixed set of auto-injected uniforms and a mandatory
TDOutputSwizzle() wrapper that the model typically hallucinates wrong.
This module ships 4 vetted templates (pixel × 3 input counts + compute)
plus a uniforms reference and the most common antipatterns.

Source is canonical TD 2025 GLSL TOP conventions. Templates are static
JSON, not introspected from TD (TD's starter shader text isn't exposed
via Python — it's a UI-only artifact). If a template proves wrong on
your build, edit glsl_templates.json directly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

_DATA_PATH = Path(__file__).parent / "data" / "glsl_templates.json"

ShaderType = Literal["pixel", "compute", "vertex"]


class GLSLTemplate(BaseModel):
    id: str
    shader_type: ShaderType
    input_count: int
    description: str
    uniforms_used: list[str]
    code: str


class UniformRef(BaseModel):
    name: str
    type: str
    scope: str
    desc: str


class Antipattern(BaseModel):
    id: str
    desc: str
    wrong: str
    right: str


class GLSLKnowledge:
    def __init__(
        self,
        templates: list[GLSLTemplate],
        uniforms: list[UniformRef],
        antipatterns: list[Antipattern],
    ):
        self.templates = templates
        self.uniforms = uniforms
        self.antipatterns = antipatterns
        self._by_id = {t.id: t for t in templates}

    @classmethod
    def load(cls, path: Path = _DATA_PATH) -> "GLSLKnowledge":
        if not path.exists():
            return cls([], [], [])
        data = json.loads(path.read_text())
        return cls(
            templates=[GLSLTemplate.model_validate(t) for t in data.get("templates", [])],
            uniforms=[UniformRef.model_validate(u) for u in data.get("uniforms_reference", [])],
            antipatterns=[Antipattern.model_validate(a) for a in data.get("antipatterns", [])],
        )

    def get(self, template_id: str) -> GLSLTemplate | None:
        return self._by_id.get(template_id)

    def index(self) -> list[dict]:
        """One-line summary per template, for the no-arg listing call."""
        return [
            {
                "id": t.id,
                "shader_type": t.shader_type,
                "input_count": t.input_count,
                "description": t.description,
            }
            for t in self.templates
        ]


_kb: GLSLKnowledge | None = None


def get_glsl_kb() -> GLSLKnowledge:
    global _kb
    if _kb is None:
        _kb = GLSLKnowledge.load()
    return _kb
