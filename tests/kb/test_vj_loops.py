from td_mcp.kb.vj_loops import VJLoopPattern, VJLoopsKB, get_vj_loops_kb


def test_pattern_schema_validates():
    p = VJLoopPattern(
        pattern_name="feedback_kaleidoscope",
        tempo_bpm_range=[90, 130],
        energy="medium",
        palette=["#ff00aa", "#00aaff", "#000000"],
        key_operators=["feedbackTOP", "transformTOP", "compositeTOP"],
        glsl_hint=None,
        description_fr="Boucle de rétroaction avec rotation continue, créant un effet kaléidoscope.",
        tags=["geometric", "feedback"],
    )
    assert p.energy == "medium"


def test_kb_loads_and_searches_by_tag():
    kb = get_vj_loops_kb()
    geometric = kb.by_tag("geometric")
    assert len(geometric) >= 1


def test_kb_text_search_returns_top_k():
    kb = get_vj_loops_kb()
    results = kb.search("calme géométrique", top_k=3)
    assert len(results) <= 3
    assert all(isinstance(r, VJLoopPattern) for r in results)
