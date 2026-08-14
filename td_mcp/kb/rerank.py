"""Cross-encoder reranking over vector-search candidates.

The bi-encoder in vector.py embeds query and chunk separately, so it only
ever compares two independent vectors — it ranks on "do these texts look
alike", not "does this chunk answer this question". A cross-encoder reads
the pair together in one attention pass and can make that second judgement.
The cost is linear in candidates: there is no reusable vector, so every
(query, chunk) pair needs its own forward pass. Hence the shape here —
fetch a wide-ish candidate set cheaply by vector similarity, then spend the
cross-encoder only on those.

Model is BAAI/bge-reranker-v2-m3: same family as the BGE-M3 embedder and
likewise multilingual, so non-English queries keep ranking sensibly. The
small MS MARCO rerankers are far faster but English-only, which would
silently degrade exactly the queries that look fine.

OFF BY DEFAULT — measured, not assumed. Across 10 gap probes on this
corpus (2026-08, bge-reranker-v2-m3, fetch_k=50, 512-token window):

    practical tutorial chunks in top-3   10/30  ->  7/30
    freshly-ingested videos in top-3      7/30  ->  5/30
    latency per search                    47ms  ->  1694ms

It over-matches queries against encyclopedic wiki passages: ask about
"deploying an installation ... perform mode" and it promotes the Perform
Mode reference page over the deployment tutorial that answers the question.
There are real wins — it tightened the Extensions results and surfaced a
depth-camera tutorial the bi-encoder missed — but they are inconsistent,
while the latency cost is not. The metric is a proxy (no labelled
relevance judgements exist for this corpus), so this is a default, not a
verdict: enable per call with rerank=True or globally with TD_MCP_RERANK=1.

Model load is lazy and failure is non-fatal — see rerank().
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_RERANK_MODEL = os.environ.get(
    "TD_MCP_RERANK_MODEL", "BAAI/bge-reranker-v2-m3"
)
# Candidates pulled from the vector index before reranking. Wide enough to
# give the cross-encoder something to rescue from the tail, small enough to
# stay in a sane latency budget.
DEFAULT_FETCH_K = int(os.environ.get("TD_MCP_RERANK_FETCH_K", "50"))
# bge-reranker-v2-m3 accepts 8192 tokens. Uncapped it reads whole chunks,
# and this corpus is wildly uneven: tutorial segments are ~400 words while
# wiki chunks run to thousands. That costs seconds per search AND lets the
# long chunks out-match the short ones on surface area rather than
# relevance. 512 is the usual reranking window and levels the comparison.
RERANK_MAX_TOKENS = int(os.environ.get("TD_MCP_RERANK_MAX_TOKENS", "512"))

_TRUTHY = {"1", "true", "yes", "on"}


def reranking_enabled() -> bool:
    """Off unless asked for. Read at call time, not import time, so the
    switch works for a long-lived server process and for tests.

    Default chosen from measurement, not preference — see the module
    docstring for what reranking actually did to this corpus.
    """
    return os.environ.get("TD_MCP_RERANK", "0").strip().lower() in _TRUTHY


class Reranker:
    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or DEFAULT_RERANK_MODEL
        self._model = None
        # Sticky: one failed load must not re-attempt (and re-stall) on
        # every subsequent search.
        self._unavailable = False

    def _load(self):
        from sentence_transformers import CrossEncoder  # lazy — heavy import

        return CrossEncoder(self.model_name)

    def _get_model(self):
        if self._model is None and not self._unavailable:
            try:
                model = self._load()
                model.max_seq_length = RERANK_MAX_TOKENS
                self._model = model
            except Exception as e:
                self._unavailable = True
                logger.warning(
                    "reranker %s unavailable (%s) — falling back to vector order",
                    self.model_name,
                    e,
                )
        return self._model

    def rerank(self, query: str, rows: list[dict], k: int) -> list[dict]:
        """Reorder `rows` by cross-encoder relevance and return the top `k`.

        Every failure path degrades to vector order rather than raising: an
        unranked answer is worth more to the caller than no answer, and the
        reranker is an enhancement to a search path that already worked.
        """
        if not rows:
            return []
        model = self._get_model()
        if model is None:
            return rows[:k]
        try:
            scores = model.predict([(query, r.get("text", "")) for r in rows])
        except Exception as e:
            logger.warning("reranker scoring failed (%s) — vector order kept", e)
            return rows[:k]
        for row, score in zip(rows, scores):
            # _distance stays: callers read it, and keeping both makes the
            # reordering auditable.
            row["_rerank_score"] = float(score)
        return sorted(rows, key=lambda r: r["_rerank_score"], reverse=True)[:k]


_reranker: Reranker | None = None


def get_reranker() -> Reranker:
    """Process-wide singleton — the model is expensive to hold twice."""
    global _reranker
    if _reranker is None:
        _reranker = Reranker()
    return _reranker


def reset_reranker_singleton() -> None:
    global _reranker
    _reranker = None
