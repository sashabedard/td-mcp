"""Vector KB — Phase 4 amorce.

LanceDB-backed embedded vector store with hybrid search (vector + SQL
filter). Default embedding model is BAAI/bge-m3 per locked §13 decision
(multilingue FR+EN, 8K context, ~2GB first-time download). Override via
TD_MCP_EMBEDDING_MODEL env var for faster iteration with smaller models.

Index lives under ~/.cache/td-mcp/lancedb/ (overridable via
TD_MCP_VECTOR_DB) — non-versioned, rebuildable from source KBs at any
time via kb_reindex.

Seed sources for the initial corpus: the operators catalog (one chunk per
op), GLSL templates, POP patterns. Web scraping (wiki, forum, tutorials)
is deferred to a later phase — the vector infra is ready when sources are.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

DEFAULT_MODEL = os.environ.get("TD_MCP_EMBEDDING_MODEL", "BAAI/bge-m3")
DEFAULT_DB_PATH = Path(
    os.environ.get(
        "TD_MCP_VECTOR_DB",
        str(Path.home() / ".cache" / "td-mcp" / "lancedb"),
    )
)
TABLE_NAME = "chunks"

ChunkSource = Literal[
    "operators", "glsl_template", "pop_pattern", "wiki", "tutorial", "forum",
    "shader_geeks3d", "shader_shadertoy"
]


class Chunk(BaseModel):
    id: str
    source: ChunkSource
    source_url: str = ""
    title: str
    text: str
    operators: list[str] = []
    families: list[str] = []
    is_glsl: bool = False
    is_python: bool = False

    def embed_text(self) -> str:
        """Concatenated text fed to the embedding model. Title-prefixed so
        title terms dominate the vector for short-query relevance."""
        return f"{self.title}\n\n{self.text}"

    def to_record(self, vector: list[float]) -> dict:
        # lancedb prefers comma-separated strings over list<string> for
        # cheap LIKE filtering. We pay tiny serialization cost, gain SQL.
        return {
            "id": self.id,
            "source": self.source,
            "source_url": self.source_url,
            "title": self.title,
            "text": self.text,
            "operators": ",".join(self.operators),
            "families": ",".join(self.families),
            "is_glsl": self.is_glsl,
            "is_python": self.is_python,
            "vector": vector,
        }


class VectorKB:
    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        model_name: str = DEFAULT_MODEL,
    ):
        self.db_path = db_path
        self.model_name = model_name
        self._db = None
        self._table = None
        self._model = None

    def _get_model(self):
        if self._model is None:
            # Lazy import — sentence-transformers + torch is heavy (~3GB
            # installed), no need to pay it for tools that never touch
            # vectors.
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model

    def _get_db(self):
        if self._db is None:
            import lancedb

            self.db_path.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self.db_path))
        return self._db

    def _embed(self, texts: list[str], batch_size: int = 8) -> list[list[float]]:
        # batch_size=8 keeps peak memory bounded on CPU/MPS — without it
        # large corpora (BGE-M3 with 8K context) trigger 60+GB allocations
        # and crash the embed call.
        model = self._get_model()
        vecs = model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
            batch_size=batch_size,
        )
        return [v.tolist() for v in vecs]

    def has_index(self) -> bool:
        if not self.db_path.exists():
            return False
        try:
            db = self._get_db()
            return TABLE_NAME in db.list_tables().tables
        except Exception:
            return False

    def count(self) -> int:
        if not self.has_index():
            return 0
        return self._get_db().open_table(TABLE_NAME).count_rows()

    def reindex(self, chunks: list[Chunk]) -> dict:
        """Drop and rebuild the chunks table from scratch."""
        import pyarrow as pa

        if not chunks:
            raise ValueError("Cannot reindex with empty chunks list")

        db = self._get_db()
        if TABLE_NAME in db.list_tables().tables:
            db.drop_table(TABLE_NAME)

        texts = [c.embed_text() for c in chunks]
        vectors = self._embed(texts)
        dim = len(vectors[0])
        records = [c.to_record(v) for c, v in zip(chunks, vectors)]

        # Explicit schema so we don't get burned by lancedb auto-inferring
        # vector dim from the first record only.
        schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("source", pa.string()),
                pa.field("source_url", pa.string()),
                pa.field("title", pa.string()),
                pa.field("text", pa.string()),
                pa.field("operators", pa.string()),
                pa.field("families", pa.string()),
                pa.field("is_glsl", pa.bool_()),
                pa.field("is_python", pa.bool_()),
                pa.field("vector", pa.list_(pa.float32(), dim)),
            ]
        )
        db.create_table(TABLE_NAME, data=records, schema=schema)
        return {
            "indexed": len(chunks),
            "dim": dim,
            "model": self.model_name,
            "path": str(self.db_path),
        }

    def upsert(self, chunks: list[Chunk]) -> dict:
        """Incremental index update: embed and add ONLY chunks that are new
        or whose text changed. A full reindex embeds everything (~15 min at
        3k chunks); upsert makes folding fresh knowledge near-free.

        Falls back to reindex() when no table exists yet.
        """
        if not chunks:
            return {"added": 0, "updated": 0, "unchanged": 0, "total": self.count()}
        if not self.has_index():
            r = self.reindex(chunks)
            return {"added": r["indexed"], "updated": 0, "unchanged": 0, "total": r["indexed"]}

        table = self._get_db().open_table(TABLE_NAME)
        existing = {
            row["id"]: row["text"]
            for row in table.search().select(["id", "text"]).limit(1_000_000).to_list()
        }

        to_add: list[Chunk] = []
        to_update: list[Chunk] = []
        unchanged = 0
        for c in chunks:
            prior = existing.get(c.id)
            if prior is None:
                to_add.append(c)
            elif prior != c.text:
                to_update.append(c)
            else:
                unchanged += 1

        pending = to_add + to_update
        if pending:
            vectors = self._embed([c.embed_text() for c in pending])
            if to_update:
                ids = ",".join(f"'{c.id}'" for c in to_update)
                table.delete(f"id IN ({ids})")
            table.add([c.to_record(v) for c, v in zip(pending, vectors)])

        return {
            "added": len(to_add),
            "updated": len(to_update),
            "unchanged": unchanged,
            "total": table.count_rows(),
        }

    def search(
        self,
        query: str,
        k: int = 10,
        source: str | None = None,
        family: str | None = None,
        is_glsl: bool | None = None,
    ) -> list[dict]:
        if not self.has_index():
            return []
        qvec = self._embed([query])[0]
        table = self._get_db().open_table(TABLE_NAME)
        q = table.search(qvec)

        where_clauses: list[str] = []
        if source:
            where_clauses.append(f"source = '{source}'")
        if family:
            # families is a comma-separated string; LIKE matches as substring
            where_clauses.append(f"families LIKE '%{family}%'")
        if is_glsl is not None:
            where_clauses.append(f"is_glsl = {str(is_glsl).lower()}")
        if where_clauses:
            q = q.where(" AND ".join(where_clauses))

        results = q.limit(k).to_list()
        # Strip the heavy vector field from the response — never useful to
        # the caller, and 1024 floats per result blows up token budget.
        for r in results:
            r.pop("vector", None)
        return results


# Module-level singleton
_kb: VectorKB | None = None


def get_vector_kb() -> VectorKB:
    global _kb
    if _kb is None:
        _kb = VectorKB()
    return _kb


def reset_vector_kb_singleton() -> None:
    """Drop the cached singleton so a subsequent get_vector_kb() honors
    any env var changes (TD_MCP_EMBEDDING_MODEL, TD_MCP_VECTOR_DB)."""
    global _kb
    _kb = None


# ─────────────────────────── seed corpus builder ───────────────────────────


def build_seed_chunks() -> list[Chunk]:
    """Chunk the existing structured KBs (operators, GLSL, POP patterns)
    into the vector store. This is the v1 corpus — wiki/forum/tutorial
    ingestion lands in a later phase.
    """
    from td_mcp.kb.glsl import get_glsl_kb
    from td_mcp.kb.operators import get_catalog
    from td_mcp.kb.pop_patterns import get_pop_kb

    chunks: list[Chunk] = []

    for entry in get_catalog().list():
        chunks.append(
            Chunk(
                id=f"op_{entry.python_class}",
                source="operators",
                title=f"{entry.python_class} ({entry.family})",
                text=(
                    f"{entry.family} family operator. "
                    f"Python class: {entry.python_class}. "
                    f"Subtype: {entry.subtype}."
                ),
                operators=[entry.python_class],
                families=[entry.family],
            )
        )

    for tpl in get_glsl_kb().templates:
        chunks.append(
            Chunk(
                id=f"glsl_{tpl.id}",
                source="glsl_template",
                title=tpl.id,
                text=f"{tpl.description}\n\nUniforms: {', '.join(tpl.uniforms_used)}.\n\n{tpl.code}",
                is_glsl=True,
            )
        )

    for pat in get_pop_kb().patterns:
        op_types = [o.op_type for o in pat.ops]
        chunks.append(
            Chunk(
                id=f"poppattern_{pat.id}",
                source="pop_pattern",
                title=pat.name,
                text=(
                    f"{pat.description}\n\n"
                    f"Tags: {', '.join(pat.tags)}.\n"
                    f"Ops: {', '.join(op_types)}.\n\n"
                    f"Notes: {pat.notes}\n\n"
                    f"Pitfalls: {' | '.join(pat.pitfalls)}"
                ),
                operators=op_types,
                families=["POP"],
            )
        )

    # Wiki chunks from cache (no network call — kb_ingest_wiki populates the cache)
    from td_mcp.ingest.wiki import build_wiki_chunks_from_cache

    chunks.extend(build_wiki_chunks_from_cache())

    # YouTube tutorial chunks from cache (kb_ingest_youtube_channel populates)
    from td_mcp.ingest.youtube import build_chunks_from_cache as build_yt_chunks

    chunks.extend(build_yt_chunks())

    # Vision-pass tutorial chunks (kb_ingest_tutorial_vision populates).
    # Complement the transcript chunks: ytv_* ids vs yt_*.
    from td_mcp.ingest.tutorial_vision import build_chunks_from_cache as build_vision_chunks

    chunks.extend(build_vision_chunks())

    return chunks
