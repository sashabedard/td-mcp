from pathlib import Path

from td_mcp.kb.glsl import GLSLKnowledge, get_glsl_kb


def test_loads_shipped_templates():
    kb = get_glsl_kb()
    ids = {t.id for t in kb.templates}
    assert {"pixel_procedural", "pixel_one_input", "pixel_two_inputs", "compute_basic"} <= ids


def test_every_template_includes_TDOutputSwizzle():
    # The whole point of shipping templates is to enforce TDOutputSwizzle;
    # if a template ever drops it, the test fails loudly.
    kb = get_glsl_kb()
    for tpl in kb.templates:
        assert "TDOutputSwizzle" in tpl.code, f"{tpl.id} is missing TDOutputSwizzle"


def test_no_template_uses_texture2D_deprecated():
    kb = get_glsl_kb()
    for tpl in kb.templates:
        # Allow it in comments but not in actual code lines that aren't a comment
        for line in tpl.code.splitlines():
            stripped = line.strip()
            if stripped.startswith("//"):
                continue
            assert "texture2D(" not in stripped, f"{tpl.id} uses deprecated texture2D()"


def test_no_template_emits_version_directive():
    # TD auto-prepends #version; emitting it ourselves breaks compilation.
    kb = get_glsl_kb()
    for tpl in kb.templates:
        for line in tpl.code.splitlines():
            assert not line.strip().startswith("#version"), f"{tpl.id} has #version"


def test_compute_template_has_layout():
    kb = get_glsl_kb()
    tpl = next(t for t in kb.templates if t.shader_type == "compute")
    assert "layout(local_size_x" in tpl.code


def test_get_unknown_returns_none():
    kb = get_glsl_kb()
    assert kb.get("nope") is None


def test_index_shape():
    kb = get_glsl_kb()
    idx = kb.index()
    assert all({"id", "shader_type", "input_count", "description"} <= set(e) for e in idx)


def test_uniforms_reference_has_swizzle_entry():
    kb = get_glsl_kb()
    names = [u.name for u in kb.uniforms]
    assert any("TDOutputSwizzle" in n for n in names)


def test_load_missing_returns_empty(tmp_path: Path):
    kb = GLSLKnowledge.load(tmp_path / "nope.json")
    assert kb.templates == []
