"""A query must not be able to kill the search it is asking.

`websearch_to_tsquery` compiles into a conjunction with a bounded parse stack,
and overflowing it raises `tsquery stack too small` - which aborts the entire
hybrid search rather than only its lexical half. The trigger found in practice
was not exotic: a 12-word window lifted from an indexed shell script that
contained a rule of seventy dashes, taken from the live store while re-deriving
the relevance floor.

Pasting a log line or a separator row is an ordinary thing for a caller to do.
"""

import pytest

import rag_engine


pytestmark = pytest.mark.unit


def test_a_run_of_punctuation_collapses():
    rule = "-" * 70
    sanitized = rag_engine.sanitize_fts_query(f'echo "{rule}" done')
    assert rule not in sanitized
    assert "-" in sanitized, "the character itself carries meaning; the run does not"
    assert "echo" in sanitized and "done" in sanitized


def test_short_punctuation_is_left_alone():
    """Two characters is a token people mean, not a rule someone drew."""
    assert rag_engine.sanitize_fts_query("a -- b") == "a -- b"
    assert "::" in rag_engine.sanitize_fts_query("std::vector")


def test_term_count_is_capped():
    many = " ".join(f"term{i}" for i in range(500))
    assert len(rag_engine.sanitize_fts_query(many).split()) == rag_engine.FTS_MAX_TERMS


def test_ordinary_queries_pass_through_unchanged():
    for query in (
        "как устроено кеширование запросов",
        "build_read_scope_sql binds session_id only for read_scope=session",
        "",
    ):
        assert rag_engine.sanitize_fts_query(query) == query


def test_the_measured_trigger_no_longer_explodes_the_term_count():
    trigger = 'под тяжелые нагрузки и СУБД!" echo "' + "-" * 70 + '" echo'
    sanitized = rag_engine.sanitize_fts_query(trigger)
    assert len(sanitized.split()) <= rag_engine.FTS_MAX_TERMS
    assert "СУБД" in sanitized
