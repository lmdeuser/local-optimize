"""Evidence decisions cannot silently fall back to candidate similarity."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import evidence_ranker
import rag_engine
from query_analysis import retrieval_variants, query_facet, requests_secret_value
from tests.unit.test_search_scope_key import make_context

pytestmark = pytest.mark.unit


def test_original_question_survives_bounded_expansion():
    query = "How does ProjectContext predict lottery numbers?"
    assert retrieval_variants(query)[0] == query
    assert len(retrieval_variants(query)) <= 3
    assert "lottery" in query_facet(query)
    assert query_facet("How does read_scope differ from write_visibility?") is None
    assert query_facet("Как найти определение функции build_read_scope_sql?") is None
    assert query_facet("Какая зарплата у автора BoundedTTLCache?") is not None


def test_secret_storage_explanation_is_not_secret_disclosure():
    assert requests_secret_value("Какой секретный пароль сейчас записан в secrets.env?")
    assert not requests_secret_value("Где хранятся пароли подключения к базе?")


def test_missing_xpu_refuses_model_construction(monkeypatch):
    import sys
    constructor = Mock()
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(xpu=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoModelForCausalLM=constructor, AutoTokenizer=constructor))
    with pytest.raises(RuntimeError, match="Intel XPU"):
        evidence_ranker.EvidenceRanker()._load()
    constructor.assert_not_called()


def test_high_name_match_is_rejected_if_requested_property_does_not_match():
    ranker = evidence_ranker.EvidenceRanker()
    ranker._load = lambda: None
    calls = []
    def score(query, hits, deadline):
        calls.append(query)
        return [0.99] if "ProjectContext" in query else [0.001]
    ranker._scores = score
    result = ranker.rank("How does ProjectContext predict lottery numbers?",
                         [{"chunk_id": 1, "content": "ProjectContext defines project scope"}], 5, 0)
    assert result == []
    assert len(calls) == 2 and "lottery" in calls[1]


def test_ranker_failure_is_unavailable_not_old_cosine_results(tmp_path):
    engine = object.__new__(rag_engine.RagEngine)
    engine._retrieve_candidates = Mock(return_value={"status": "success", "results": [
        {"chunk_id": 1, "content": "fixture", "final_score": 1.0}]})
    engine.evidence_ranker = SimpleNamespace(rank=Mock(side_effect=TimeoutError))
    result = engine._search_uncached(make_context(tmp_path), "fixture")
    assert result["status"] == "unavailable"
    assert not result.get("results")


def test_cache_is_exact_and_policy_is_part_of_scope(tmp_path, monkeypatch):
    engine = object.__new__(rag_engine.RagEngine)
    engine._init_query_caches()
    assert engine._persistent_cache.semantic_threshold == 1.0
    context = make_context(tmp_path)
    previous = engine._search_scope_key(context, 5)
    monkeypatch.setattr(rag_engine, "POLICY_HASH", "new-policy")
    assert engine._search_scope_key(context, 5) != previous


@pytest.mark.parametrize("query, accepted", [("How long is the cache TTL?", False),
                                            ("What does test_cache_ttl verify?", True)])
def test_test_fixtures_require_a_test_question(query, accepted):
    ranker = evidence_ranker.EvidenceRanker()
    ranker._load = lambda: None
    ranker._scores = lambda query, hits, deadline: [0.99] * len(hits)
    hits = [{"chunk_id": 1, "content": "Synthetic fixture TTL is 42 seconds",
             "source_kind": "test_fixture"}]
    assert bool(ranker.rank(query, hits, 5, 0)) == accepted
