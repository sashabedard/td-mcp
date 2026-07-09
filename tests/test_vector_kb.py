"""Vector KB tests.

Integration tests against a real embedding model are GATED behind the env
var TD_MCP_RUN_HEAVY_TESTS=1 because they download a model (~80MB for the
tiny test model, ~2GB for the production default). CI / fast test runs
get the cheap unit tests only.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from td_mcp.kb.vector import Chunk, VectorKB, build_seed_chunks, reset_vector_kb_singleton

HEAVY = os.environ.get("TD_MCP_RUN_HEAVY_TESTS") == "1"


def test_chunk_embed_text_includes_title():
    c = Chunk(id="a", source="operators", title="noiseCHOP", text="generates noise channels")
    assert c.embed_text().startswith("noiseCHOP")
    assert "noise channels" in c.embed_text()


def test_chunk_to_record_serializes_lists_as_strings():
    c = Chunk(
        id="x",
        source="pop_pattern",
        title="t",
        text="body",
        operators=["gridPOP", "noisePOP"],
        families=["POP", "CHOP"],
    )
    rec = c.to_record([0.1, 0.2, 0.3])
    assert rec["operators"] == "gridPOP,noisePOP"
    assert rec["families"] == "POP,CHOP"
    assert rec["vector"] == [0.1, 0.2, 0.3]


def test_seed_chunks_cover_all_three_sources():
    chunks = build_seed_chunks()
    sources = {c.source for c in chunks}
    assert {"operators", "glsl_template", "pop_pattern"} <= sources


def test_seed_chunks_operators_count_matches_catalog():
    from td_mcp.kb.operators import get_catalog

    chunks = build_seed_chunks()
    op_chunks = [c for c in chunks if c.source == "operators"]
    assert len(op_chunks) == get_catalog().count


def test_seed_chunks_include_shader_sources(monkeypatch):
    """kb_ingest_geeks3d_shaders / kb_ingest_shadertoy_shaders both tell the
    user to run kb_reindex afterward — so build_seed_chunks must actually
    fold the cached shader chunks in."""
    from td_mcp.ingest import shaders_geeks3d, shaders_shadertoy

    st = Chunk(id="shadertoy_X", source="shader_shadertoy", title="t", text="x", is_glsl=True)
    g3 = Chunk(id="geeks3d_Y", source="shader_geeks3d", title="t", text="x", is_glsl=True)
    monkeypatch.setattr(shaders_shadertoy, "build_shadertoy_chunks", lambda *a, **k: iter([st]))
    monkeypatch.setattr(shaders_geeks3d, "build_geeks3d_chunks", lambda *a, **k: iter([g3]))

    ids = {c.id for c in build_seed_chunks()}
    assert "shadertoy_X" in ids
    assert "geeks3d_Y" in ids


def test_seed_chunks_pop_pattern_metadata():
    chunks = build_seed_chunks()
    pop_chunks = [c for c in chunks if c.source == "pop_pattern"]
    for c in pop_chunks:
        assert c.operators, f"POP pattern chunk {c.id} has no operators"
        assert "POP" in c.families


def test_kb_has_no_index_returns_zero_count(tmp_path: Path):
    reset_vector_kb_singleton()
    kb = VectorKB(db_path=tmp_path / "empty_db")
    assert kb.has_index() is False
    assert kb.count() == 0


def test_search_without_index_returns_empty(tmp_path: Path):
    kb = VectorKB(db_path=tmp_path / "empty_db")
    assert kb.search("anything") == []


def _make_table_without_model(kb: VectorKB, chunks: list[Chunk]) -> None:
    """Create the chunks table with dummy vectors — get_video_chunks is a
    filter scan and must be testable without downloading an embedding model."""
    db = kb._get_db()
    records = [c.to_record([0.0, 0.0, 0.0, 0.0]) for c in chunks]
    db.create_table("chunks", data=records)


def _video_chunk(chunk_id: str, title: str = "t") -> Chunk:
    return Chunk(id=chunk_id, source="tutorial", title=title, text=f"body of {chunk_id}")


def test_get_video_chunks_orders_and_splits_sources(tmp_path: Path):
    kb = VectorKB(db_path=tmp_path / "db")
    _make_table_without_model(kb, [
        _video_chunk("ytv_4bIQXKJaWlA_02"),
        _video_chunk("yt_4bIQXKJaWlA_01", title="Point Vortex (part 2/4)"),
        _video_chunk("ytv_4bIQXKJaWlA_10"),
        _video_chunk("yt_4bIQXKJaWlA_00", title="Point Vortex (part 1/4)"),
        _video_chunk("ytv_zzzOTHERvid_01"),  # different video must not leak
    ])
    result = kb.get_video_chunks("4bIQXKJaWlA")
    assert [r["id"] for r in result["transcript"]] == ["yt_4bIQXKJaWlA_00", "yt_4bIQXKJaWlA_01"]
    assert [r["id"] for r in result["vision"]] == ["ytv_4bIQXKJaWlA_02", "ytv_4bIQXKJaWlA_10"]
    assert result["title"] == "Point Vortex (part 1/4)"
    assert all("vector" not in r for r in result["transcript"] + result["vision"])


def test_get_video_chunks_underscore_video_id_no_like_bleed(tmp_path: Path):
    """'_' in a video id is a LIKE single-char wildcard — the exact regex
    filter must reject near-miss ids that the SQL prefilter lets through."""
    kb = VectorKB(db_path=tmp_path / "db")
    _make_table_without_model(kb, [
        _video_chunk("ytv_1_0O_oceZbU_02"),
        _video_chunk("ytv_1a0OboceZbU_01"),  # matches LIKE '%1_0O_oceZbU%'? close shape
    ])
    result = kb.get_video_chunks("1_0O_oceZbU")
    assert [r["id"] for r in result["vision"]] == ["ytv_1_0O_oceZbU_02"]


def test_get_video_chunks_no_index(tmp_path: Path):
    kb = VectorKB(db_path=tmp_path / "nope")
    result = kb.get_video_chunks("whatever")
    assert result["transcript"] == [] and result["vision"] == []


@pytest.mark.skipif(not HEAVY, reason="Heavy: requires embedding model download")
def test_reindex_and_search_roundtrip(tmp_path: Path):
    """Full integration test — uses MiniLM (~80MB) for speed."""
    kb = VectorKB(
        db_path=tmp_path / "test_db",
        model_name="sentence-transformers/all-MiniLM-L6-v2",
    )
    chunks = [
        Chunk(id="a", source="operators", title="noiseCHOP",
              text="generates noise channels for audio reactive work"),
        Chunk(id="b", source="operators", title="rectangleTOP",
              text="draws a flat colored rectangle"),
        Chunk(id="c", source="pop_pattern", title="audio reactive particles",
              text="drive particle motion from audio fft"),
    ]
    result = kb.reindex(chunks)
    assert result["indexed"] == 3
    assert kb.has_index()
    assert kb.count() == 3

    # Semantic query should rank pattern + noise CHOP above rectangle
    hits = kb.search("audio reactive", k=3)
    assert len(hits) == 3
    top_ids = [h["id"] for h in hits[:2]]
    assert "b" not in top_ids  # rectangle should NOT be in top 2

    # Filter by source
    only_ops = kb.search("audio", k=10, source="operators")
    assert all(h["source"] == "operators" for h in only_ops)


# ─────────────────────────── upsert correctness ──────────────────────────────


def _fake_embed(kb: VectorKB) -> None:
    kb._embed = lambda texts, batch_size=8: [[0.0, 0.0, 0.0, 0.0] for _ in texts]


def test_upsert_detects_title_change(tmp_path: Path):
    """The embedded vector is title+text — a title-only change must be
    re-embedded, not counted 'unchanged' with the stale title kept."""
    kb = VectorKB(db_path=tmp_path / "db")
    _make_table_without_model(kb, [
        Chunk(id="x", source="tutorial", title="old title", text="same body"),
    ])
    _fake_embed(kb)

    report = kb.upsert([Chunk(id="x", source="tutorial", title="new title", text="same body")])
    assert report["updated"] == 1
    rows = kb._get_db().open_table("chunks").search().select(["id", "title"]).limit(10).to_list()
    assert rows[0]["title"] == "new title"


def test_upsert_purges_orphans_of_present_sources_only(tmp_path: Path):
    """Re-segmenting a video 6→4 chunks must drop the stale _04/_05 rows
    (kb_get_tutorial promises EVERY chunk, ordered — mixing segmentations
    breaks step-by-step rebuilds). Sources absent from the new chunk set
    (e.g. wiki cache not on this machine) must NOT be purged."""
    kb = VectorKB(db_path=tmp_path / "db")
    _make_table_without_model(kb, [
        *[_video_chunk(f"yt_vid_{i:02d}") for i in range(6)],
        Chunk(id="op_noiseCHOP", source="operators", title="noiseCHOP", text="op"),
    ])
    _fake_embed(kb)

    new_chunks = [
        Chunk(id=f"yt_vid_{i:02d}", source="tutorial", title="t", text=f"body of yt_vid_{i:02d}")
        for i in range(4)
    ]
    report = kb.upsert(new_chunks)
    assert report["removed"] == 2, "stale tutorial chunks not purged"

    ids = {r["id"] for r in kb._get_db().open_table("chunks").search()
           .select(["id"]).limit(100).to_list()}
    assert "yt_vid_04" not in ids and "yt_vid_05" not in ids
    assert "op_noiseCHOP" in ids, "absent source wrongly purged"


def test_has_index_propagates_real_errors(tmp_path: Path):
    """A corrupted/unreadable index must surface its real error — reporting
    'index is empty' sends the agent into a pointless 15-min reindex."""
    kb = VectorKB(db_path=tmp_path / "db")
    (tmp_path / "db").mkdir()

    def boom():
        raise RuntimeError("lance table corrupted")

    kb._get_db = boom
    with pytest.raises(RuntimeError, match="corrupted"):
        kb.has_index()


# ─────────────────────────── server-side filter validation ──────────────────


@pytest.mark.asyncio
async def test_kb_search_rejects_unknown_source_with_valid_values():
    from td_mcp import server

    result = await server.kb_search("noise", source="tutorials")  # typo: plural
    assert result["ok"] is False
    assert "tutorial" in str(result.get("valid_sources", "")), result


@pytest.mark.asyncio
async def test_kb_list_operators_normalizes_family_case():
    from td_mcp import server

    result = await server.kb_list_operators(family="chop")
    assert result["ok"] is True
    assert result["total_in_family"] > 0, "lowercase family silently matched nothing"


@pytest.mark.asyncio
async def test_kb_list_operators_rejects_unknown_family():
    from td_mcp import server

    result = await server.kb_list_operators(family="CHOPS")
    assert result["ok"] is False
    assert "CHOP" in str(result.get("valid_families", ""))
