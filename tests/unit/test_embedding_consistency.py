"""Retrieval must refuse to serve when stored vectors came from another model.

Query and stored embeddings are only comparable when one model produced both.
Mixing them raises nothing — pgvector measures a cosine between two unrelated
embedding spaces perfectly happily — it just returns confident nonsense, scored
and ranked like any other hit. Since a model swap is necessarily two steps
(configure, then re-embed), there is a window where exactly that happens.
"""
import pytest

import rag_engine


pytestmark = pytest.mark.unit


class PopulationCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        assert "embedding_model" in str(query)

    def fetchall(self):
        return self.rows


def build_engine(rows):
    engine = object.__new__(rag_engine.RagEngine)
    connection = type(
        "C",
        (),
        {
            "cursor": lambda _self: PopulationCursor(rows),
            "rollback": lambda _self: None,
        },
    )()
    engine._get_conn = lambda: connection
    engine._put_conn = lambda *_args, **_kwargs: None
    return engine


def test_a_uniform_store_on_the_configured_model_is_consistent():
    engine = build_engine([(rag_engine.EMBEDDING_MODEL_NAME, rag_engine.EMBEDDING_DIM, 1124)])

    report = engine.embedding_consistency()

    assert report["consistent"] is True
    assert report["mismatched_rows"] == 0


def test_rows_from_another_model_are_counted_as_mismatched():
    engine = build_engine([
        (rag_engine.EMBEDDING_MODEL_NAME, rag_engine.EMBEDDING_DIM, 900),
        ("some-other-model", rag_engine.EMBEDDING_DIM, 224),
    ])

    report = engine.embedding_consistency()

    assert report["consistent"] is False
    assert report["mismatched_rows"] == 224


def test_a_dimension_mismatch_counts_even_under_the_right_model_name():
    engine = build_engine([(rag_engine.EMBEDDING_MODEL_NAME, 768, 10)])

    report = engine.embedding_consistency()

    assert report["consistent"] is False


def test_readiness_fails_closed_during_a_half_finished_reembed(monkeypatch):
    """A partly re-embedded store must not answer queries at all."""
    engine = build_engine([])
    monkeypatch.setattr(
        "migration_runner.verify_runtime_schema", lambda _conn, **_kwargs: True
    )
    engine.embedding_consistency = lambda: {"consistent": False, "mismatched_rows": 224}

    assert engine.check_readiness() is False


def test_readiness_passes_when_the_store_is_uniform(monkeypatch):
    engine = build_engine([])
    monkeypatch.setattr(
        "migration_runner.verify_runtime_schema", lambda _conn, **_kwargs: True
    )
    engine.embedding_consistency = lambda: {"consistent": True, "mismatched_rows": 0}

    assert engine.check_readiness() is True



def test_retrieval_readiness_is_cached_for_thirty_seconds():
    from unittest.mock import Mock

    engine = object.__new__(rag_engine.RagEngine)
    engine._pool = object()
    engine.check_readiness = Mock(return_value=True)

    engine.require_readiness()
    engine.require_readiness()

    assert engine.check_readiness.call_count == 1
