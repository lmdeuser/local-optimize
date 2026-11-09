"""Compaction may drop diagnostics; it may not change a decision or hide a caveat.

The compact rendering exists because the audit rendering costs roughly twice the
tokens for the same answer, and retrieval that is expensive to read cancels much
of the point of retrieval. But a projection that quietly loses something a reader
would have acted on is worse than the tokens it saved.

These tests draw that line in both directions: what must survive compaction
(the text, where it came from, the score, a rotted fact's freshness, and the
disclosure that an answer came from a *different* question's cache) and what must
not (score components, hashes, per-hit model provenance, duplicated text).
"""

import json

import pytest

import response_format as rf


pytestmark = pytest.mark.unit


def make_hit(**overrides):
    hit = {
        "chunk_id": 1128,
        "document_id": None,
        "content": "Кеш ключуется по read_scope, а не по session_id.",
        "metadata": {"relative_path": "docs/query-cache.md", "chunk_id": 1, "total_chunks": 2},
        "source_uri": "/srv/local-optimize/docs/query-cache.md",
        "source_type": "document",
        "content_hash": "a" * 64,
        "score_components": {
            "vector_gpu_score": 0.8897882103919983,
            "rrf_score": 0.032786883413791656,
            "lexical_boost": 0.09000000357627869,
            "recency_decay": 0.97780841588974,
            "authority_weight": 0.949999988079071,
        },
        "final_score": 0.6858,
        "score": 0.6858,
        "start_locator": "3",
        "end_locator": "4",
        "embedding_model": "multilingual-e5-small",
        "embedding_version": "2.0",
        "validation_status": "unverified",
        "freshness_status": "recent",
        "days_since_confirmation": 21.8,
        "ever_verified": False,
        "recheck_after_days": None,
    }
    hit.update(overrides)
    return hit


def make_response(hits=None, **overrides):
    response = {
        "status": "success",
        "top_score": 0.6858,
        "evidence_coverage": 1.0,
        "composite_metrics": {"candidates_evaluated": 55, "top_score": 0.6858, "avg_score": 0.52},
        "results": hits if hits is not None else [make_hit()],
    }
    response.update(overrides)
    return response


def test_compaction_keeps_what_a_reader_acts_on():
    compact = rf.compact_search_response(make_response())
    hit = compact["results"][0]
    assert hit["id"] == 1128
    assert hit["score"] == 0.686
    assert hit["src"] == "docs/query-cache.md#1/2"
    assert hit["status"] == "unverified"
    assert hit["fresh"] == "recent"
    assert "read_scope" in hit["text"]
    assert compact["top_score"] == 0.6858
    assert compact["candidates"] == 55


def test_compaction_drops_diagnostics_and_per_hit_constants():
    hit = rf.compact_search_response(make_response())["results"][0]
    for dropped in (
        "score_components",
        "content_hash",
        "embedding_model",
        "embedding_version",
        "metadata",
        "source_uri",
        "start_locator",
        "end_locator",
        "final_score",
        "document_id",
    ):
        assert dropped not in hit, f"{dropped} survived compaction"


def test_defaults_are_omitted_but_deviations_are_kept():
    """`ever_verified: false` on every hit teaches nothing; `true` on one does."""
    plain = rf.compact_search_response(make_response())["results"][0]
    assert "verified" not in plain
    assert "recheck_days" not in plain
    assert "age_days" not in plain, "age only matters once freshness says it does"

    notable = rf.compact_search_response(
        make_response(
            [make_hit(ever_verified=True, recheck_after_days=30, freshness_status="overdue")]
        )
    )["results"][0]
    assert notable["verified"] is True
    assert notable["recheck_days"] == 30
    assert notable["age_days"] == 21.8
    assert notable["fresh"] == "overdue"


def test_full_verbosity_is_the_untouched_response():
    original = make_response()
    assert rf.compact_search_response(original, verbosity="full") is original


def test_minimal_drops_the_text_but_keeps_the_addresses():
    compact = rf.compact_search_response(make_response(), verbosity="minimal")
    hit = compact["results"][0]
    assert set(hit) == {"id", "score", "src"}
    assert compact["top_score"] == 0.6858


def test_identical_text_is_collapsed():
    response = make_response([make_hit(), make_hit(chunk_id=999)])
    compact = rf.compact_search_response(response)
    assert len(compact["results"]) == 1, "the same content_hash twice is one paragraph"


def test_truncation_is_marked_not_silent():
    compact = rf.compact_search_response(make_response(), max_content_chars=20)
    text = compact["results"][0]["text"]
    assert text.endswith(rf.TRUNCATION_MARKER)
    assert len(text) <= 20 + len(rf.TRUNCATION_MARKER)


def test_cache_disclosure_survives_compaction():
    """Answering one question from another question's answer must stay visible."""
    provenance = {"layer": "l2-semantic", "matched_query": "иначе", "similarity": 0.991}
    compact = rf.compact_search_response(make_response(cache=provenance))
    assert compact["cache"] == provenance


def test_errors_pass_through_untouched():
    error = {"status": "error", "message": "search backend returned an error status"}
    assert rf.compact_search_response(error) is error


def test_unknown_verbosity_falls_back_to_compact_rather_than_full():
    """A typo must not silently triple the token cost of every retrieval."""
    assert rf.normalize_verbosity("verbose") == rf.VERBOSITY_COMPACT
    assert rf.normalize_verbosity(None) == rf.DEFAULT_VERBOSITY
    assert rf.normalize_verbosity("FULL") == rf.VERBOSITY_FULL


def test_dumps_spends_no_tokens_on_whitespace():
    payload = {"a": 1, "b": [1, 2]}
    assert rf.dumps(payload) == '{"a":1,"b":[1,2]}'
    assert len(rf.dumps(payload)) < len(json.dumps(payload, indent=2))


def test_compaction_measurably_shrinks_a_realistic_response():
    response = make_response([make_hit(chunk_id=i, content_hash=str(i) * 64) for i in range(5)])
    full = json.dumps(response, ensure_ascii=False, indent=2)
    compact = rf.dumps(rf.compact_search_response(response))
    assert len(compact) < len(full) * 0.6


class TestContextBlock:
    def test_empty_hits_render_nothing_at_all(self):
        """A header over an empty memory still costs tokens and still invites trust."""
        assert rf.render_context_block([]) == ""
        assert rf.render_context_block([{"text": "   "}]) == ""

    def test_block_is_marked_so_memory_cannot_pass_as_user_input(self):
        block = rf.render_context_block([{"text": "факт", "src": "a.md#1", "score": 0.5}])
        assert block.startswith(rf.CONTEXT_HEADER)
        assert block.endswith(rf.CONTEXT_FOOTER)
        assert "a.md#1" in block

    def test_rotted_facts_carry_their_warning_into_the_prompt(self):
        block = rf.render_context_block(
            [{"text": "факт", "src": "a.md#1", "score": 0.5, "fresh": "overdue"}]
        )
        assert "overdue" in block
        assert "re-check" in block

    def test_the_budget_is_a_budget(self):
        hits = [{"text": "x" * 400, "src": f"f{i}.md#0", "score": 0.5} for i in range(10)]
        block = rf.render_context_block(hits, max_chars=900)
        assert len(block) < 1200, "the ceiling may be approached, not ignored"
