from pathlib import Path

from td_mcp.ingest.wiki import (
    CATEGORY_BY_FAMILY,
    _normalize,
    build_wiki_chunks_from_cache,
    manifest,
)


def test_normalize_strips_spaces_underscores_case():
    assert _normalize("Sphere POP") == "spherepop"
    assert _normalize("Ableton Link CHOP") == "abletonlinkchop"
    assert _normalize("GLSL POP") == "glslpop"
    assert _normalize("CPlusPlus_POP") == "cpluspluspop"
    assert _normalize("noisePOP") == "noisepop"


def test_normalize_matches_catalog_to_wiki_title_examples():
    # Known wiki page titles taken from live derivative.ca
    pairs = [
        ("spherePOP", "Sphere POP"),
        ("abletonlinkCHOP", "Ableton Link CHOP"),
        ("glslPOP", "GLSL POP"),
        ("choptoPOP", "CHOP to POP"),
        ("cplusplusPOP", "CPlusPlus POP"),
    ]
    for python_class, wiki_title in pairs:
        assert _normalize(python_class) == _normalize(wiki_title), (
            f"{python_class!r} should normalize-equal {wiki_title!r}"
        )


def test_category_map_covers_main_families():
    for fam in ("CHOP", "TOP", "SOP", "POP", "DAT", "MAT"):
        assert fam in CATEGORY_BY_FAMILY
        assert CATEGORY_BY_FAMILY[fam].startswith("Category:")


def test_manifest_on_missing_cache(tmp_path: Path):
    m = manifest(cache_dir=tmp_path / "nope")
    assert m["exists"] is False
    assert m["count"] == 0


def test_manifest_counts_cached_files(tmp_path: Path):
    cache = tmp_path / "wiki"
    cache.mkdir()
    (cache / "spherepop.txt").write_text("Sphere POP body text")
    (cache / "noisepop.txt").write_text("Noise POP body text")
    m = manifest(cache_dir=cache)
    assert m["count"] == 2
    assert m["bytes"] > 0


def test_build_wiki_chunks_from_cache_matches_catalog(tmp_path: Path):
    # Use a cache name matching an actual catalog op
    cache = tmp_path / "wiki"
    cache.mkdir()
    (cache / "spherepop.txt").write_text("Sphere POP creates spherical point clouds.")
    chunks = list(build_wiki_chunks_from_cache(cache_dir=cache))
    assert len(chunks) == 1
    c = chunks[0]
    assert c.source == "wiki"
    assert c.operators == ["spherePOP"]
    assert c.families == ["POP"]
    assert "spherical" in c.text


def test_build_wiki_chunks_keeps_unknown_pages_as_wikip(tmp_path: Path):
    """Contract change with the full-wiki scrape: non-operator pages are no
    longer dropped — they become wikip_* concept chunks."""
    cache = tmp_path / "wiki"
    cache.mkdir()
    (cache / "notarealoppop.txt").write_text("something")
    chunks = list(build_wiki_chunks_from_cache(cache_dir=cache))
    assert len(chunks) == 1
    assert chunks[0].id == "wikip_notarealoppop"
    assert chunks[0].operators == []


def test_split_text_short_page_stays_whole():
    from td_mcp.ingest.wiki import split_text
    text = "word " * 500
    assert split_text(text.strip(), max_words=700) == [text.strip()]


def test_split_text_long_page_splits_on_paragraphs():
    from td_mcp.ingest.wiki import split_text
    para = ("lorem " * 300).strip()
    text = "\n\n".join([para] * 5)  # 1500 mots
    pieces = split_text(text, max_words=700)
    assert len(pieces) == 3  # 600+600+300, orphelin fusionné ou pas selon seuil
    assert all(p for p in pieces)
    # rien n'est perdu
    assert sum(len(p.split()) for p in pieces) == 1500


def test_full_wiki_chunks_nonoperator_pages_get_wikip_ids(tmp_path):
    import json
    from td_mcp.ingest.wiki import build_full_wiki_chunks_from_cache
    (tmp_path / "feedbacktop.txt").write_text("feedback op page text")
    (tmp_path / "renderpickdat.txt").write_text("some other page")
    (tmp_path / "pythonclasses.txt").write_text("python classes overview")
    (tmp_path / "pages_manifest.json").write_text(json.dumps({
        "feedbacktop": {"title": "Feedback TOP"},
        "pythonclasses": {"title": "Python Classes"},
    }))
    chunks = list(build_full_wiki_chunks_from_cache(tmp_path))
    by_id = {c.id: c for c in chunks}
    assert "wiki_feedbackTOP" in by_id           # page opérateur: id historique
    assert by_id["wiki_feedbackTOP"].operators == ["feedbackTOP"]
    assert "wikip_pythonclasses" in by_id        # page concept: id wikip_
    assert by_id["wikip_pythonclasses"].title == "Python Classes"
    assert by_id["wikip_pythonclasses"].operators == []
