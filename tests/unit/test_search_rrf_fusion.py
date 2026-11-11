"""Reciprocal rank fusion must rank within each query's own result list.

`search` issues one vector query and one FTS query per expanded query. RRF gives
a candidate `weight / (K + rank + 1)`, which only means anything when `rank` is
that candidate's position in a single ranked list. Concatenating the pools first
makes the second expansion's *best* hit look like it placed 51st.
"""
import time

import numpy as np
import pytest

import rag_engine
from tests.similarity_reference import ReferenceReranker
from project_context import ProjectContext, ReadScope, WriteVisibility


pytestmark = pytest.mark.unit


def make_context(tmp_path):
    return ProjectContext(
        "project-a", "Project A", "root-a", str(tmp_path), (str(tmp_path),),
        WriteVisibility.SHARED, ReadScope.SHARED, "agent-a", "session-a",
    )


class PerQueryCursor:
    """Returns a different vector pool for each expanded query, in call order."""

    def __init__(self, pools):
        self.pools = pools
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
        pool = self.pools.pop(0) if self.pools else []
        self.rows = [
            (
                chunk_id, f"content {chunk_id}", {}, [1.0, 0.0], f"hash-{chunk_id}",
                now, None, None, "uri", "code", "unverified", None, "0", "1",
                "local", "", "", "shared", None, "fixture.py", None, "uri", "code",
                "project-a", "root-a",
            )
            for chunk_id in pool
        ]

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


def run_search(pools, expansions, tmp_path):
    state = {"pools": [list(p) for p in pools]}
    engine = object.__new__(rag_engine.RagEngine)
    cursor = PerQueryCursor(state["pools"])
    engine._get_conn = lambda: type("C", (), {"cursor": lambda _self: cursor})()
    engine._put_conn = lambda *_args, **_kwargs: None
    engine.encode = lambda _text, **_kw: np.array([1.0, 0.0], dtype=np.float32)
    engine._expand_query = lambda query: expansions
    engine.reranker = ReferenceReranker()
    return engine._retrieve_candidates(make_context(tmp_path), "query", limit=10)


def test_rank_is_scoped_to_the_query_that_produced_it(tmp_path):
    """A chunk that tops the second expansion's list must be scored as a winner.

    Both pools here are single-element, so chunk 1 and chunk 2 each placed first
    for their own query. Their fused ranking contribution must be identical.
    """
    result = run_search([[1], [2]], ["query", "expansion"], tmp_path)

    assert result["status"] == "success", result
    by_id = {hit["chunk_id"]: hit for hit in result["results"]}
    assert set(by_id) == {1, 2}
    first = by_id[1]["score_components"]["rrf_score"]
    second = by_id[2]["score_components"]["rrf_score"]
    assert first == pytest.approx(second), (
        f"rank leaked across pools: {first} vs {second}"
    )


def test_a_chunk_ranked_first_twice_beats_one_ranked_first_once(tmp_path):
    """Agreement between expansions is the signal RRF exists to capture."""
    result = run_search([[1, 2], [1, 3]], ["query", "expansion"], tmp_path)

    by_id = {hit["chunk_id"]: hit for hit in result["results"]}
    agreed = by_id[1]["score_components"]["rrf_score"]
    for other in (2, 3):
        assert agreed > by_id[other]["score_components"]["rrf_score"]


def test_rrf_reference_is_the_score_of_a_candidate_first_in_both_pools(tmp_path):
    """The published [0, 1] scale depends on this constant being exactly that."""
    expected = (
        rag_engine.VECTOR_SOURCE_WEIGHT + rag_engine.FTS_SOURCE_WEIGHT
    ) / float(rag_engine.RRF_K + 1)

    assert rag_engine.RRF_REFERENCE == pytest.approx(expected)
