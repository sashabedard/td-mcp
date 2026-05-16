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


def test_build_wiki_chunks_ignores_unknown_pages(tmp_path: Path):
    cache = tmp_path / "wiki"
    cache.mkdir()
    (cache / "notarealoppop.txt").write_text("something")
    chunks = list(build_wiki_chunks_from_cache(cache_dir=cache))
    # Unknown name should be silently dropped — not crash
    assert chunks == []
