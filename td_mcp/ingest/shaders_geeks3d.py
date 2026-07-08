"""geeks3d.com/shader-library scraper.

Walks the shader-library index, fetches each linked article, extracts
the description text plus all <pre>/<code> code blocks. Result feeds
the existing vector KB as Chunk(source="shader_geeks3d", is_glsl=True).

Polite: 1 req/sec floor, retries once on transient errors, caches
extracted pages to disk so reruns are free.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from pathlib import Path
from typing import Iterator

import httpx

from td_mcp.kb.vector import Chunk
from td_mcp.util import write_json_atomic

INDEX_URL = "https://www.geeks3d.com/shader-library/"
USER_AGENT = "td-mcp/0.0.2 (personal-research; +contact@labai)"
DEFAULT_CACHE_DIR = Path(
    os.environ.get(
        "TD_MCP_GEEKS3D_CACHE",
        str(Path.home() / ".cache" / "td-mcp" / "geeks3d"),
    )
)
RATE_LIMIT_SEC = 1.0

# Article URLs look like geeks3d.com/YYYYMMDD/slug/ or geeks3d.com/YYYY/slug/
_ARTICLE_RE = re.compile(r'href="(https?://(?:www\.)?geeks3d\.com/\d{4,8}/[^"#?]+/)"')
# Strip script/style before code-block extraction
_TAG_STRIP_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
# GLSL code blocks: <pre>...</pre> or <code>...</code>
_CODE_BLOCK_RE = re.compile(r"<(pre|code)[^>]*>(.*?)</\1>", re.S | re.I)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _polite_get(client: httpx.Client, url: str, last_request_time: list[float]) -> str | None:
    """GET with rate-limit + one retry. Returns text or None on failure."""
    elapsed = time.time() - last_request_time[0]
    if elapsed < RATE_LIMIT_SEC:
        time.sleep(RATE_LIMIT_SEC - elapsed)
    for attempt in range(2):
        try:
            resp = client.get(url, timeout=20.0)
            last_request_time[0] = time.time()
            if resp.status_code == 200:
                return resp.text
            if resp.status_code >= 500 and attempt == 0:
                time.sleep(2.0)
                continue
            return None
        except httpx.HTTPError:
            if attempt == 0:
                time.sleep(2.0)
                continue
            return None
    return None


def _decode_html(s: str) -> str:
    """Minimal HTML entity decode for the few entities common in code blocks."""
    return html.unescape(s)


def _extract_title(html_text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
    if not m:
        return "(untitled)"
    return _decode_html(re.sub(r"\s+", " ", m.group(1))).strip()


def _extract_text_and_code(html_text: str) -> str:
    """Extract description text + code blocks, joined."""
    cleaned = _TAG_STRIP_RE.sub("", html_text)
    code_blocks = [_decode_html(m.group(2).strip()) for m in _CODE_BLOCK_RE.finditer(cleaned)]
    # Strip code blocks from text body so they don't double-appear
    body_html = _CODE_BLOCK_RE.sub(" ", cleaned)
    body_text = _decode_html(_HTML_TAG_RE.sub(" ", body_html))
    body_text = re.sub(r"\s+", " ", body_text).strip()
    parts = [body_text] if body_text else []
    for cb in code_blocks:
        parts.append("```glsl\n" + cb + "\n```")
    return "\n\n".join(parts)


def discover_article_urls(client: httpx.Client | None = None) -> list[str]:
    """Fetch the index page and return all article URLs."""
    own = client is None
    if own:
        client = httpx.Client(headers={"User-Agent": USER_AGENT})
    try:
        last = [0.0]
        html_text = _polite_get(client, INDEX_URL, last)
        if html_text is None:
            return []
        urls = sorted(set(_ARTICLE_RE.findall(html_text)))
        return urls
    finally:
        if own:
            client.close()


def fetch_article(url: str, cache_dir: Path, client: httpx.Client, last: list[float]) -> dict | None:
    """Fetch one article. Returns {title, url, text} or None.

    On-disk cache: cache_dir/<sha1>.json so reruns are free.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha1(url.encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    html_text = _polite_get(client, url, last)
    if html_text is None:
        return None
    record = {
        "url": url,
        "title": _extract_title(html_text),
        "text": _extract_text_and_code(html_text),
    }
    write_json_atomic(cache_file, record)
    return record


def build_geeks3d_chunks(cache_dir: Path = DEFAULT_CACHE_DIR) -> Iterator[Chunk]:
    """Iterate cached articles and yield Chunk entries.

    Caller is responsible for running ingest_geeks3d() first to populate
    the cache, or calling this against an existing cache.
    """
    for cache_file in sorted(cache_dir.glob("*.json")):
        record = json.loads(cache_file.read_text())
        if not record.get("text"):
            continue
        yield Chunk(
            id=f"geeks3d_{cache_file.stem}",
            source="shader_geeks3d",
            source_url=record["url"],
            title=record["title"],
            text=record["text"],
            is_glsl=True,
        )


def ingest_geeks3d(cache_dir: Path = DEFAULT_CACHE_DIR, limit: int | None = None) -> dict:
    """End-to-end: discover URLs -> fetch each -> cache to disk.

    Returns a report. Caller separately runs reindex / VectorKB.add_chunks
    to push the cached records into LanceDB.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    report = {"discovered": 0, "fetched": 0, "cached": 0, "failed": 0}
    last = [0.0]
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        urls = discover_article_urls(client)
        report["discovered"] = len(urls)
        if limit:
            urls = urls[:limit]
        for url in urls:
            cache_key = hashlib.sha1(url.encode()).hexdigest()
            cache_file = cache_dir / f"{cache_key}.json"
            if cache_file.exists():
                report["cached"] += 1
                continue
            record = fetch_article(url, cache_dir, client, last)
            if record is None:
                report["failed"] += 1
            else:
                report["fetched"] += 1
    return report
