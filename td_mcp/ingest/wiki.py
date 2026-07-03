"""derivative.ca wiki scraper — Phase 4.1 amorce.

Polite scraper for docs.derivative.ca: uses the MediaWiki Category API
to enumerate canonical op page titles per family, then fetches each page
and extracts clean text via trafilatura.

Rate-limited (1 req/sec floor), retries once on transient errors, caches
extracted text to disk so reruns are free. The cache is the source for
build_wiki_chunks() — separating fetch from indexing keeps the ingestion
restartable and the indexing fast.

Naming normalization: catalog python_class names (e.g. "abletonlinkCHOP")
are mapped to wiki canonical titles (e.g. "Ableton Link CHOP") by
lowercase-and-strip-spaces equality. The wiki's category list is the
source of truth — we never derive the slug algorithmically.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterator

import httpx

from td_mcp.kb.vector import Chunk

WIKI_API = "https://docs.derivative.ca/api.php"
WIKI_BASE = "https://docs.derivative.ca"
USER_AGENT = "td-mcp/0.0.2 (personal-research; +contact@labai)"

DEFAULT_CACHE_DIR = Path(
    os.environ.get("TD_MCP_WIKI_CACHE", str(Path.home() / ".cache" / "td-mcp" / "wiki"))
)

# MediaWiki category name per op family. COMPs are missing because the
# wiki has many sub-categories for COMPs (no single Category:COMPs page).
CATEGORY_BY_FAMILY = {
    "CHOP": "Category:CHOPs",
    "TOP": "Category:TOPs",
    "SOP": "Category:SOPs",
    "POP": "Category:POPs",
    "DAT": "Category:DATs",
    "MAT": "Category:MATs",
}


def _normalize(name: str) -> str:
    """Lowercase + strip whitespace + strip underscores for matching.
    Also drops filesystem-hostile chars: wiki titles like "Palette/Mapping"
    would otherwise become nested cache paths and crash the write."""
    return (
        "".join(name.lower().split())
        .replace("_", "")
        .replace("/", "")
        .replace(":", "")
    )


class WikiClient:
    def __init__(
        self,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        min_interval: float = 1.0,
        timeout: float = 15.0,
    ):
        self.cache_dir = cache_dir
        self.min_interval = min_interval
        self.timeout = timeout
        self._last_request_at: float = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._client.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def list_category(self, category_title: str) -> list[str]:
        """Return all canonical page titles in a category (paginated)."""
        titles: list[str] = []
        cont: dict = {}
        while True:
            self._throttle()
            params = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": category_title,
                "cmlimit": 500,
                "cmtype": "page",
                "format": "json",
                **cont,
            }
            r = self._client.get(WIKI_API, params=params)
            r.raise_for_status()
            data = r.json()
            for m in data.get("query", {}).get("categorymembers", []):
                titles.append(m["title"])
            cont = data.get("continue", {})
            if not cont:
                break
        return titles

    def enumerate_all_pages(self) -> list[str]:
        """All article titles (namespace 0) via the allpages API, paginated.
        ~2100 titles on docs.derivative.ca; a handful of API calls."""
        titles: list[str] = []
        cont: dict = {}
        while True:
            self._throttle()
            params = {
                "action": "query",
                "list": "allpages",
                "aplimit": 500,
                "apnamespace": 0,
                "format": "json",
                **cont,
            }
            r = self._client.get(WIKI_API, params=params)
            r.raise_for_status()
            data = r.json()
            for m in data.get("query", {}).get("allpages", []):
                titles.append(m["title"])
            cont = data.get("continue", {})
            if not cont:
                break
        return titles

    def fetch_page_text(self, title: str) -> str:
        """Fetch a page's rendered HTML and extract clean text. Uses on-disk
        cache keyed by normalized title; subsequent calls return cached text.
        """
        import trafilatura

        cache_file = self.cache_dir / f"{_normalize(title)}.txt"
        if cache_file.exists():
            return cache_file.read_text()

        self._throttle()
        # Convert title to wiki slug (spaces → underscores) for URL fetch
        slug = title.replace(" ", "_")
        url = f"{WIKI_BASE}/{slug}"
        r = self._client.get(url)
        if r.status_code != 200:
            return ""

        text = trafilatura.extract(r.text, include_tables=True, include_comments=False) or ""
        if text:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(text)
        return text


# ─────────────────────────── ingestion pipeline ────────────────────────────


def map_catalog_to_wiki(
    family: str, client: WikiClient
) -> dict[str, str]:
    """Return {python_class: wiki_title} for ops in the given family that
    have a matching wiki page (normalized lowercase-nospaces equality)."""
    from td_mcp.kb.operators import get_catalog

    category = CATEGORY_BY_FAMILY.get(family)
    if not category:
        return {}

    wiki_titles = client.list_category(category)
    by_norm = {_normalize(t): t for t in wiki_titles}

    catalog = get_catalog()
    mapping: dict[str, str] = {}
    for entry in catalog.list(family=family):  # type: ignore
        norm = _normalize(entry.python_class)
        if norm in by_norm:
            mapping[entry.python_class] = by_norm[norm]
    return mapping


def ingest_family(
    family: str,
    client: WikiClient,
    limit: int | None = None,
) -> list[Chunk]:
    """Fetch all wiki pages for a family, extract text, return chunks.
    Pages already cached on disk are reused; only new ones cost a request.
    """
    mapping = map_catalog_to_wiki(family, client)
    items = list(mapping.items())
    if limit:
        items = items[:limit]

    chunks: list[Chunk] = []
    for python_class, wiki_title in items:
        text = client.fetch_page_text(wiki_title)
        if not text:
            continue
        slug = wiki_title.replace(" ", "_")
        chunks.append(
            Chunk(
                id=f"wiki_{python_class}",
                source="wiki",
                source_url=f"{WIKI_BASE}/{slug}",
                title=wiki_title,
                text=text,
                operators=[python_class],
                families=[family],
            )
        )
    return chunks


def build_wiki_chunks_from_cache(
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> Iterator[Chunk]:
    """Yield Chunks from previously-cached wiki pages without network calls.
    Delegates to the full-wiki builder: operator pages keep their historical
    ids, concept/Python/guide pages get wikip_* ids."""
    yield from build_full_wiki_chunks_from_cache(cache_dir)


def _pages_manifest_path(cache_dir: Path) -> Path:
    return cache_dir / "pages_manifest.json"


def _load_pages_manifest(cache_dir: Path) -> dict:
    p = _pages_manifest_path(cache_dir)
    return json.loads(p.read_text()) if p.exists() else {}


def ingest_all_pages(
    client: WikiClient,
    limit: int | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> dict:
    """Fetch EVERY wiki article (not just operator pages). Cached pages are
    free; only new pages cost a request (1/sec politeness). A titles
    manifest is persisted so non-operator pages keep their real title and
    URL at chunk-build time. Resumable: re-run continues where it stopped."""
    titles = client.enumerate_all_pages()
    manifest_data = _load_pages_manifest(cache_dir)

    fetched, cached, empty = 0, 0, 0
    for title in titles:
        norm = _normalize(title)
        cache_file = cache_dir / f"{norm}.txt"
        already = cache_file.exists()
        if not already and limit is not None and fetched >= limit:
            continue
        text = client.fetch_page_text(title)
        if norm not in manifest_data:
            manifest_data[norm] = {"title": title}
            _pages_manifest_path(cache_dir).parent.mkdir(parents=True, exist_ok=True)
            _pages_manifest_path(cache_dir).write_text(json.dumps(manifest_data, indent=0))
        if already:
            cached += 1
        elif text:
            fetched += 1
        else:
            empty += 1

    return {
        "total_titles": len(titles),
        "fetched_new": fetched,
        "already_cached": cached,
        "empty_or_failed": empty,
        "remaining": len(titles) - fetched - cached - empty,
    }


def split_text(text: str, max_words: int = 700) -> list[str]:
    """Split page text into ~max_words chunks on paragraph boundaries.
    Splitting mid-paragraph hurts embedding quality; a page slightly over
    budget stays whole rather than producing a tiny orphan chunk."""
    words_total = len(text.split())
    if words_total <= int(max_words * 1.3):
        return [text]
    paras = [p for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    count = 0
    for p in paras:
        w = len(p.split())
        if count + w > max_words and current:
            chunks.append("\n\n".join(current))
            current, count = [], 0
        current.append(p)
        count += w
    if current:
        # avoid a tiny trailing orphan: merge into the previous chunk
        if chunks and count < max_words * 0.25:
            chunks[-1] = chunks[-1] + "\n\n" + "\n\n".join(current)
        else:
            chunks.append("\n\n".join(current))
    return chunks


def build_full_wiki_chunks_from_cache(
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> Iterator[Chunk]:
    """Yield Chunks for EVERY cached wiki page (operator pages and concept/
    Python/guide pages alike). Operator pages keep their historical id
    (wiki_<python_class>) on the first chunk for index continuity; long
    pages are split on paragraph boundaries."""
    from td_mcp.kb.operators import get_catalog

    if not cache_dir.exists():
        return

    catalog = get_catalog()
    by_norm = {_normalize(e.python_class): e for e in catalog.list()}
    titles = _load_pages_manifest(cache_dir)

    for txt_file in sorted(cache_dir.glob("*.txt")):
        norm = txt_file.stem
        text = txt_file.read_text()
        if not text:
            continue
        entry = by_norm.get(norm)
        meta = titles.get(norm, {})
        if entry:
            title = meta.get("title") or f"{entry.subtype} {entry.family}".strip()
            base_id = f"wiki_{entry.python_class}"
            operators = [entry.python_class]
            families = [entry.family]
        else:
            title = meta.get("title") or norm
            base_id = f"wikip_{norm}"
            operators = []
            families = []
        slug = title.replace(" ", "_")
        pieces = split_text(text)
        for i, piece in enumerate(pieces):
            suffix = "" if i == 0 else f"_{i:02d}"
            part = f" (part {i + 1}/{len(pieces)})" if len(pieces) > 1 else ""
            yield Chunk(
                id=f"{base_id}{suffix}",
                source="wiki",
                source_url=f"{WIKI_BASE}/{slug}",
                title=f"{title}{part}",
                text=piece,
                operators=operators,
                families=families,
            )


def _legacy_build_wiki_chunks(
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> Iterator[Chunk]:
    """Legacy operator-only chunk builder (superseded by
    build_full_wiki_chunks_from_cache)."""
    if not cache_dir.exists():
        return

    from td_mcp.kb.operators import get_catalog

    catalog = get_catalog()
    by_norm = {_normalize(e.python_class): e for e in catalog.list()}

    for txt_file in cache_dir.glob("*.txt"):
        norm = txt_file.stem
        entry = by_norm.get(norm)
        if not entry:
            continue
        text = txt_file.read_text()
        if not text:
            continue
        # Derive readable title from python_class (good-enough fallback)
        wiki_title = f"{entry.subtype} {entry.family}".strip()
        slug = wiki_title.replace(" ", "_")
        yield Chunk(
            id=f"wiki_{entry.python_class}",
            source="wiki",
            source_url=f"{WIKI_BASE}/{slug}",
            title=wiki_title,
            text=text,
            operators=[entry.python_class],
            families=[entry.family],
        )


def manifest(cache_dir: Path = DEFAULT_CACHE_DIR) -> dict:
    """Summarize the wiki cache: total pages, total bytes, list of family
    representations based on the operators catalog."""
    if not cache_dir.exists():
        return {"cache_dir": str(cache_dir), "exists": False, "count": 0, "bytes": 0}

    files = list(cache_dir.glob("*.txt"))
    return {
        "cache_dir": str(cache_dir),
        "exists": True,
        "count": len(files),
        "bytes": sum(f.stat().st_size for f in files),
    }
