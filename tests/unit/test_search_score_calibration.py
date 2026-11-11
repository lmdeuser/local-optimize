"""`final_score` must measure relevance, not rank within the candidate pool.

Every consumer of this engine treats the score as an absolute judgement:
`gpu_rag_server` routes on `top_score >= RAG_CONFIDENCE_THRESHOLD`, `fast_router`
escalates to a frontier model below it, `ProjectContext.min_score` filters on it,
and the operator rule tells agents that `>= 0.80` means "answer from this".

None of that holds if the score only says "this candidate beat the others in its
own pool" — a pool of uniformly irrelevant rows still has a best row.
"""
import time

import numpy as np
import pytest

import rag_engine
from tests.similarity_reference import ReferenceReranker
from project_context import ProjectContext, ReadScope, WriteVisibility


pytestmark = pytest.mark.unit


def make_context(tmp_path, *, min_score=0.0):
    return ProjectContext(
        "project-a", "Project A", "root-a", str(tmp_path), (str(tmp_path),),
        WriteVisibility.SHARED, ReadScope.SHARED, "agent-a", "session-a",
        min_score=min_score,
    )


class PoolCursor:
    """Serves one fixed vector-candidate pool; the FTS query matches nothing."""

    def __init__(self, embeddings):
        self.embeddings = embeddings
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        if "websearch_to_tsquery" in str(query):
            self.rows = []
            return
        now = time.time()
        self.rows = [
            (
                idx, f"content {idx}", {}, list(emb), f"hash-{idx}", now,
                None, None, "uri", "code", "unverified", None, "0", "1",
                "local", "", "", "shared", None, "fixture.py", None, "uri", "code",
                "project-a", "root-a",
            )
            for idx, emb in enumerate(self.embeddings)
        ]

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


def build_engine(embeddings, tmp_path):
    engine = object.__new__(rag_engine.RagEngine)
    engine._get_conn = lambda: type(
        "C", (), {"cursor": lambda _self: PoolCursor(embeddings)}
    )()
    engine._put_conn = lambda *_args, **_kwargs: None
    engine.encode = lambda _text, **_kw: np.array([1.0, 0.0], dtype=np.float32)
    engine._expand_query = lambda query: [query]
    engine.reranker = ReferenceReranker()
    return engine


def unit(cos):
    """A 2-D unit vector whose cosine against the query [1, 0] is `cos`."""
    return [float(cos), float(np.sqrt(max(0.0, 1.0 - cos * cos)))]


def top_score_for(cosines, tmp_path, **ctx_kwargs):
    engine = build_engine([unit(c) for c in cosines], tmp_path)
    result = engine._retrieve_candidates(make_context(tmp_path, **ctx_kwargs), "query", limit=5)
    assert result["status"] == "success", result
    return result


def test_pool_of_irrelevant_candidates_does_not_score_as_confident(tmp_path):
    """A query that matches nothing must not come back looking answered.

    Fifty rows at cosine ~0.2 are noise. Ranking them against each other still
    produces a best one, but it is not evidence and must not clear the routing
    threshold.
    """
    result = top_score_for([0.22 - i * 0.001 for i in range(50)], tmp_path)

    assert result["top_score"] < rag_engine.RELEVANCE_FLOOR, (
        f"a pool of cosine~0.2 rows reported top_score={result['top_score']}"
    )


def test_score_tracks_similarity_not_pool_position(tmp_path):
    """Same pool shape, different absolute quality — the scores must differ."""
    good = top_score_for([0.95, 0.60, 0.55], tmp_path)["top_score"]
    poor = top_score_for([0.30, 0.20, 0.15], tmp_path)["top_score"]

    assert good > poor + 0.2, f"good={good} poor={poor}"


def test_single_perfect_match_outscores_a_pool_of_noise(tmp_path):
    """The degenerate-pool case, which min-max normalisation gets backwards.

    With one candidate there is no spread to normalise, so the old `_soft_norm`
    returned a flat 0.5 — an exact match scored *worse* than the best row of a
    fifty-row noise pool, which normalised to 1.0.
    """
    exact = top_score_for([1.0], tmp_path)["top_score"]
    noise = top_score_for([0.25 - i * 0.001 for i in range(50)], tmp_path)["top_score"]

    assert exact > noise, f"exact match scored {exact}, noise pool scored {noise}"


def test_final_score_stays_within_the_documented_range(tmp_path):
    """A threshold is only meaningful against a bounded scale."""
    result = top_score_for([1.0, 0.99, 0.98], tmp_path)

    for hit in result["results"]:
        assert 0.0 <= hit["final_score"] <= 1.0, hit["final_score"]


def test_every_consumer_reads_the_same_calibrated_cut():
    """The engine, the HTTP route decision and the escalation router must agree.

    They previously defaulted to three different numbers against a score that was
    not on a common scale, so "is this hit good enough" had three answers.
    """
    import fast_router
    import gpu_rag_server
    from config import settings

    assert rag_engine.RELEVANCE_FLOOR == settings.relevance_floor
    assert fast_router.RAG_CONFIDENCE_THRESHOLD == settings.relevance_floor
    assert gpu_rag_server.RAG_CONFIDENCE_THRESHOLD == settings.relevance_floor


def test_min_score_filters_on_the_calibrated_scale(tmp_path):
    """`min_score` is the caller's relevance floor, so it must see real scores."""
    result = top_score_for([0.30, 0.25, 0.20], tmp_path, min_score=0.60)

    assert result["results"] == []
    assert result["top_score"] == 0.0
