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


import pytest


@pytest.mark.asyncio
async def test_kb_get_vj_loop_reference_returns_results():
    from td_mcp.server import kb_get_vj_loop_reference
    result = await kb_get_vj_loop_reference(query="calme organique", top_k=2)
    assert result["ok"] is True
    assert "patterns" in result
    assert len(result["patterns"]) <= 2


@pytest.mark.asyncio
async def test_kb_get_vj_loop_reference_empty_query_returns_all_capped():
    from td_mcp.server import kb_get_vj_loop_reference
    result = await kb_get_vj_loop_reference(query="", top_k=3)
    assert result["ok"] is True
    assert len(result["patterns"]) <= 3


def test_kb_has_minimum_pattern_count():
    kb = get_vj_loops_kb()
    assert len(kb.patterns) >= 20, f"expected >=20 patterns, got {len(kb.patterns)}"


def test_all_patterns_have_required_fields():
    kb = get_vj_loops_kb()
    for p in kb.patterns:
        assert p.pattern_name
        assert p.tempo_bpm_range[0] < p.tempo_bpm_range[1]
        assert len(p.palette) >= 1
        assert len(p.key_operators) >= 1
        assert len(p.description_fr) >= 20


from unittest.mock import patch


@pytest.mark.asyncio
async def test_kb_ingest_vj_corpus_runs_pipeline(tmp_path):
    from td_mcp.server import kb_ingest_vj_corpus
    url_list = tmp_path / "urls.json"
    url_list.write_text("[]")
    fake_report = {"videos_processed": 2, "frames_added": 40, "videos_failed": 0}
    with patch("td_mcp.server.ingest_vj_corpus", return_value=fake_report):
        result = await kb_ingest_vj_corpus(url_list_path=str(url_list))
        assert result["ok"] is True
        assert result["report"]["frames_added"] == 40


def test_search_attaches_visual_refs_when_table_has_matches():
    pd = pytest.importorskip("pandas")
    from unittest.mock import MagicMock, patch
    from td_mcp.kb.vj_loops import VJLoopsKB, VJLoopPattern

    pattern = VJLoopPattern(
        pattern_name="noise_warp_calm",
        tempo_bpm_range=[60, 90],
        energy="calm",
        palette=["#1a2a4a"],
        key_operators=["noiseTOP"],
        description_fr="Boucle calme contemplative.",
        tags=["calm"],
    )
    kb = VJLoopsKB([pattern])

    fake_df = pd.DataFrame([
        {"energy": "calm", "frame_path": "/tmp/f1.png", "artist": "ouchhh"},
    ])
    fake_table = MagicMock()
    fake_table.to_pandas.return_value = fake_df
    with patch("td_mcp.kb.vj_corpus.open_table", return_value=fake_table):
        results = kb.search("calme", top_k=1, attach_visuals=True)
        assert len(results) == 1
        assert len(results[0].visual_refs) == 1
        assert results[0].visual_refs[0].artist == "ouchhh"
