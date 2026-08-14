"""Cross-encoder reranking over vector-search candidates.

The bi-encoder scores query and chunk independently and never sees them
together, so it ranks on coarse similarity. A cross-encoder reads the pair
in one pass and can judge whether a chunk answers *this* query. These tests
pin the reordering contract and — more importantly — the failure behaviour:
a KB search that dies because a reranker is unavailable is worse than an
unranked result.

The CrossEncoder itself is faked throughout; the real model is a 2.3GB
download and has no place in a unit suite.
"""
from unittest.mock import patch

from td_mcp.kb.rerank import Reranker, reranking_enabled


def _rows(n: int) -> list[dict]:
    """Vector-search output shape: ordered by ascending distance."""
    return [
        {"id": f"c{i}", "text": f"chunk {i}", "_distance": 0.1 * i}
        for i in range(n)
    ]


class FakeCrossEncoder:
    """Scores pairs by a caller-supplied function of the chunk text."""

    def __init__(self, score_fn):
        self._score_fn = score_fn

    def predict(self, pairs, **kwargs):
        return [self._score_fn(text) for _query, text in pairs]


def _reranker_with(score_fn) -> Reranker:
    r = Reranker()
    r._model = FakeCrossEncoder(score_fn)
    return r


def test_rerank_puts_highest_scoring_chunk_first():
    """The whole point: a chunk the bi-encoder ranked last can win."""
    rows = _rows(5)
    # Score inverts vector order — chunk 4 is the true best answer.
    r = _reranker_with(lambda text: float(text.split()[-1]))

    out = r.rerank("q", rows, k=3)

    assert [row["id"] for row in out] == ["c4", "c3", "c2"]


def test_rerank_keeps_distance_and_adds_score():
    """Downstream code reads _distance; reranking must add signal, not
    replace it."""
    r = _reranker_with(lambda text: 1.0)

    out = r.rerank("q", _rows(2), k=2)

    assert all("_distance" in row for row in out)
    assert all("_rerank_score" in row for row in out)


def test_rerank_falls_back_to_vector_order_when_model_unavailable():
    """Offline, out of memory, model yanked from the hub — the search still
    has to answer. Falling back to bi-encoder order loses quality; raising
    loses the result entirely."""
    r = Reranker()
    with patch.object(Reranker, "_load", side_effect=RuntimeError("no model")):
        out = r.rerank("q", _rows(4), k=2)

    assert [row["id"] for row in out] == ["c0", "c1"]


def test_reranker_caps_sequence_length_on_load():
    """bge-reranker-v2-m3 accepts 8192 tokens. Left uncapped it reads whole
    chunks — and wiki chunks run to thousands of words while tutorial
    segments are ~400. That costs seconds per search (measured 5-19s across
    probes) and hands long chunks more surface to match on than short ones,
    which biases ranking by length rather than relevance."""
    from td_mcp.kb.rerank import RERANK_MAX_TOKENS

    class FakeModel:
        max_seq_length = 8192

        def predict(self, pairs, **kwargs):
            return [0.0] * len(pairs)

    r = Reranker()
    with patch.object(Reranker, "_load", return_value=FakeModel()):
        r.rerank("q", _rows(1), k=1)

    assert r._model.max_seq_length == RERANK_MAX_TOKENS


def test_rerank_of_empty_candidate_set_is_empty():
    """A filtered search can legitimately match nothing."""
    r = _reranker_with(lambda text: 1.0)
    assert r.rerank("q", [], k=5) == []


# ───────────────────────── wiring into VectorKB.search ─────────────────────


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.limit_arg = None
        self.where_arg = None

    def where(self, clause):
        self.where_arg = clause
        return self

    def limit(self, n):
        self.limit_arg = n
        return self

    def to_list(self):
        return [dict(r) for r in self._rows[: self.limit_arg]]


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows
        self.query = None

    def search(self, _vec):
        self.query = _FakeQuery(self._rows)
        return self.query


def _kb_over(rows):
    """A VectorKB whose index is `rows`, with embedding stubbed out."""
    from td_mcp.kb.vector import VectorKB

    kb = VectorKB()
    table = _FakeTable(rows)

    class _DB:
        def open_table(self, _name):
            return table

    kb.has_index = lambda: True
    kb._embed = lambda texts: [[0.0]]
    kb._get_db = lambda: _DB()
    return kb, table


def test_search_rescues_a_chunk_the_bi_encoder_buried(monkeypatch):
    """The reason reranking exists: a chunk ranked 40th by vector distance
    can be the best answer. That is only reachable if search fetches well
    beyond k before reranking."""
    monkeypatch.setenv("TD_MCP_RERANK", "1")
    rows = _rows(50)
    kb, table = _kb_over(rows)
    # Only the last candidate is truly relevant.
    scorer = _reranker_with(lambda text: 1.0 if text == "chunk 49" else 0.0)

    with patch("td_mcp.kb.vector.get_reranker", return_value=scorer):
        out = kb.search("q", k=3)

    assert out[0]["id"] == "c49"
    assert len(out) == 3
    assert table.query.limit_arg > 3, "must fetch a wider pool than k to rerank"


def test_search_leaves_vector_order_alone_when_disabled(monkeypatch):
    monkeypatch.setenv("TD_MCP_RERANK", "0")
    kb, table = _kb_over(_rows(50))

    out = kb.search("q", k=3)

    assert [r["id"] for r in out] == ["c0", "c1", "c2"]
    assert all("_rerank_score" not in r for r in out)
    assert table.query.limit_arg == 3, "no reason to over-fetch when not reranking"


def test_reranking_off_by_default(monkeypatch):
    """Measured on this corpus (10 gap probes, 2026-08): reranking dropped
    practical tutorial chunks in the top-3 from 10/30 to 7/30 and cost
    47ms -> 1694ms per search. It over-matches queries against
    encyclopedic wiki passages — asking about "perform mode" surfaces the
    Perform Mode reference page over the deployment tutorial that actually
    answers it. Real wins exist but are inconsistent; the cost is not.
    Opt-in per call or per environment, never imposed."""
    monkeypatch.delenv("TD_MCP_RERANK", raising=False)
    assert reranking_enabled() is False


def test_reranking_opt_in_by_env(monkeypatch):
    monkeypatch.setenv("TD_MCP_RERANK", "1")
    assert reranking_enabled() is True
