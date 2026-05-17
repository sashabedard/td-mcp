"""shadertoy.com public API ingestion.

Searches for shaders by query, fetches each result's full source via
the API, and yields Chunk entries for the vector KB. The API key is
read from TD_MCP_SHADERTOY_API_KEY (register one at
shadertoy.com/myapps — free).

Polite: 1 req/sec floor, retries once on transient errors. Disk cache
under TD_MCP_SHADERTOY_CACHE keyed by shader_id so reruns are free.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterator

import httpx

from td_mcp.kb.vector import Chunk

API_BASE = "https://www.shadertoy.com/api/v1"
USER_AGENT = "td-mcp/0.0.2 (personal-research; +contact@labai)"
RATE_LIMIT_SEC = 1.0
DEFAULT_CACHE_DIR = Path(
    os.environ.get(
        "TD_MCP_SHADERTOY_CACHE",
        str(Path.home() / ".cache" / "td-mcp" / "shadertoy"),
    )
)


def _get_api_key() -> str:
    key = os.environ.get("TD_MCP_SHADERTOY_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Missing TD_MCP_SHADERTOY_API_KEY env var. "
            "Register a key at https://www.shadertoy.com/myapps"
        )
    return key


def _polite_get_json(client: httpx.Client, url: str, last: list[float]) -> dict | None:
    elapsed = time.time() - last[0]
    if elapsed < RATE_LIMIT_SEC:
        time.sleep(RATE_LIMIT_SEC - elapsed)
    for attempt in range(2):
        try:
            resp = client.get(url, timeout=20.0)
            last[0] = time.time()
            if resp.status_code == 200:
                return resp.json()
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


def search_shaders(query: str, num: int = 24, client: httpx.Client | None = None) -> list[str]:
    """Return a list of shader IDs matching the query."""
    key = _get_api_key()
    url = f"{API_BASE}/shaders/query/{query}?key={key}&from=0&num={num}"
    own = client is None
    if own:
        client = httpx.Client(headers={"User-Agent": USER_AGENT})
    try:
        data = _polite_get_json(client, url, [0.0])
        if data is None:
            return []
        return list(data.get("Results", []))
    finally:
        if own:
            client.close()


def fetch_shader(shader_id: str, cache_dir: Path, client: httpx.Client, last: list[float]) -> dict | None:
    """Fetch one shader's full data. Caches to disk."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{shader_id}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    key = _get_api_key()
    url = f"{API_BASE}/shaders/{shader_id}?key={key}"
    data = _polite_get_json(client, url, last)
    if data is None or "Shader" not in data:
        return None
    cache_file.write_text(json.dumps(data["Shader"]))
    return data["Shader"]


def _shader_to_chunk(shader: dict) -> Chunk | None:
    """Convert a Shadertoy shader dict to a vector-KB Chunk."""
    info = shader.get("info", {})
    name = info.get("name", "(untitled)")
    description = info.get("description", "")
    username = info.get("username", "unknown")
    tags = info.get("tags", [])
    shader_id = info.get("id", "unknown")

    passes = shader.get("renderpass", [])
    code_parts = []
    for p in passes:
        ptype = p.get("type", "")
        pname = p.get("name", ptype)
        code = p.get("code", "").strip()
        if not code:
            continue
        code_parts.append(f"// {pname} ({ptype})\n```glsl\n{code}\n```")

    if not code_parts:
        return None

    text_parts = []
    if description:
        text_parts.append(description)
    if tags:
        text_parts.append("Tags: " + ", ".join(tags))
    text_parts.append("Author: " + username)
    text_parts.extend(code_parts)

    return Chunk(
        id=f"shadertoy_{shader_id}",
        source="shader_shadertoy",
        source_url=f"https://www.shadertoy.com/view/{shader_id}",
        title=name,
        text="\n\n".join(text_parts),
        is_glsl=True,
    )


def build_shadertoy_chunks(cache_dir: Path = DEFAULT_CACHE_DIR) -> Iterator[Chunk]:
    """Iterate cached shaders and yield Chunk entries."""
    for cache_file in sorted(cache_dir.glob("*.json")):
        shader = json.loads(cache_file.read_text())
        chunk = _shader_to_chunk(shader)
        if chunk is not None:
            yield chunk


def ingest_shadertoy(queries: list[str], num_per_query: int = 24, cache_dir: Path = DEFAULT_CACHE_DIR) -> dict:
    """Search each query, fetch each result, cache to disk.

    Returns a report. Caller separately reindexes via VectorKB.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    report = {"queries": len(queries), "discovered": 0, "fetched": 0, "cached": 0, "failed": 0}
    last = [0.0]
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        seen: set[str] = set()
        for q in queries:
            ids = search_shaders(q, num=num_per_query, client=client)
            report["discovered"] += len(ids)
            for sid in ids:
                if sid in seen:
                    continue
                seen.add(sid)
                cache_file = cache_dir / f"{sid}.json"
                if cache_file.exists():
                    report["cached"] += 1
                    continue
                shader = fetch_shader(sid, cache_dir, client, last)
                if shader is None:
                    report["failed"] += 1
                else:
                    report["fetched"] += 1
    return report
