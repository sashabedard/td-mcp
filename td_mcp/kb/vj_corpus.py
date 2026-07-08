"""LanceDB table `vj_references` — CLIP-embedded frames from VJ corpus."""
from __future__ import annotations

import os
from pathlib import Path

import lancedb
import pyarrow as pa

TABLE_NAME = "vj_references"
DEFAULT_DB_PATH = Path(
    os.environ.get(
        "TD_MCP_VECTOR_DB",
        str(Path.home() / ".cache" / "td-mcp" / "lancedb"),
    )
)

SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("artist", pa.string()),
    pa.field("frame_path", pa.string()),
    pa.field("energy", pa.string()),
    pa.field("palette_hex", pa.string()),
    pa.field("tempo_estimate", pa.float32()),
    pa.field("embedding", pa.list_(pa.float32(), 512)),
])


def open_table(db_path: Path = DEFAULT_DB_PATH):
    db = lancedb.connect(str(db_path))
    if TABLE_NAME not in db.list_tables().tables:
        return db.create_table(TABLE_NAME, schema=SCHEMA)
    return db.open_table(TABLE_NAME)


def search_by_embedding(embedding: list[float], top_k: int = 3) -> list[dict]:
    table = open_table()
    rows = table.search(embedding).limit(top_k).to_list()
    for r in rows:
        # 512 floats per row is response noise for every caller.
        r.pop("embedding", None)
    return rows
