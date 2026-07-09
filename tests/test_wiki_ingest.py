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


def test_normalize_strips_filesystem_hostile_chars():
    from td_mcp.ingest.wiki import _normalize
    assert _normalize("Palette/Mapping") == "palettemapping"
    assert _normalize("Category:CHOPs") == "categorychops"
    assert "/" not in _normalize("a/b/c")


def test_split_text_hard_caps_giant_paragraphs():
    """Un tableau wiki rendu comme un seul bloc sans \\n\\n ne doit jamais
    produire un chunk au-delà du budget (crash MPS 32GiB observé)."""
    from td_mcp.ingest.wiki import split_text
    giant = "cell " * 5000  # 5000 mots, zéro frontière de paragraphe
    pieces = split_text(giant.strip(), max_words=700)
    assert all(len(p.split()) <= 700 * 1.35 for p in pieces)
    assert sum(len(p.split()) for p in pieces) == 5000


# ─────────────────────────── resilience ─────────────────────────────────────

import httpx  # noqa: E402


class _FlakyTransport(httpx.BaseTransport):
    """Fails the first request with a 503, succeeds afterwards."""

    def __init__(self):
        self.calls = 0

    def handle_request(self, request):
        self.calls += 1
        if self.calls == 1:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(
            200,
            json={"query": {"allpages": [{"title": "Noise CHOP"}]}, },
        )


def test_wiki_client_retries_once_on_transient_error(tmp_path: Path):
    """The module docstring promises 'retries once on transient errors' —
    a single 503 must not abort a 300-request run."""
    from td_mcp.ingest.wiki import WikiClient

    client = WikiClient(cache_dir=tmp_path, min_interval=0.0)
    transport = _FlakyTransport()
    client._client = httpx.Client(transport=transport)
    titles = client.enumerate_all_pages()
    assert titles == ["Noise CHOP"]
    assert transport.calls == 2


def test_ingest_all_pages_survives_a_failing_page(tmp_path: Path):
    """One page erroring mid-run must be counted, not fatal."""
    from unittest.mock import MagicMock

    from td_mcp.ingest.wiki import ingest_all_pages

    client = MagicMock()
    client.enumerate_all_pages.return_value = ["Good Page", "Bad Page", "Also Good"]

    def fetch(title):
        if title == "Bad Page":
            raise httpx.ConnectError("boom")
        return f"text of {title}"

    client.fetch_page_text.side_effect = fetch
    report = ingest_all_pages(client, cache_dir=tmp_path)
    assert report["fetched_new"] == 2
    assert report["errors"] == 1


def test_ingest_all_pages_flags_normalize_collisions(tmp_path: Path):
    """Two distinct titles normalizing to the same cache key silently drop
    a page — the collision must at least be counted and visible."""
    from unittest.mock import MagicMock

    from td_mcp.ingest.wiki import ingest_all_pages

    client = MagicMock()
    client.enumerate_all_pages.return_value = ["Palette:Kinect", "Palette Kinect"]
    client.fetch_page_text.side_effect = lambda title: f"text of {title}"

    report = ingest_all_pages(client, cache_dir=tmp_path)
    assert report["collisions"] == 1
