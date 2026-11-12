"""A fact's age must be measured from its last confirmation, not its first write.

`recency_decay` ranks by write time, which is not truth. The facts most exposed
to this describe other people's systems — DOM selectors, undocumented endpoints,
UI behaviour — and their expiry is invisible from this machine. It cannot be
detected, only declared in advance and then re-confirmed.
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


class RecordingCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        self.connection.calls.append((" ".join(str(query).split()), list(params)))

    def fetchall(self):
        return self.connection.rows

    def fetchone(self):
        return (1,)

    @property
    def rowcount(self):
        return 1


class RecordingConnection:
    def __init__(self, rows=()):
        self.calls = []
        self.rows = list(rows)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def build_engine(connection):
    engine = object.__new__(rag_engine.RagEngine)
    engine._get_conn = lambda: connection
    engine._put_conn = lambda *_args, **_kwargs: None
    return engine


class FreshnessCursor:
    """One vector candidate whose verification state the test controls."""

    def __init__(self, created_ts, verified_ts, recheck_days):
        self.state = (created_ts, verified_ts, recheck_days)
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        if "websearch_to_tsquery" in str(query):
            self.rows = []
            return
        created_ts, verified_ts, recheck_days = self.state
        self.rows = [(
            1, "content", {}, [1.0, 0.0], "hash", created_ts, verified_ts,
            recheck_days, "uri", "code", "unverified", None, "0", "1", "local",
            "", "", "shared", None, "fixture.py", None, "uri", "code", "project-a", "root-a",
        )]

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


def search_with(created_ts, verified_ts, recheck_days, tmp_path):
    engine = object.__new__(rag_engine.RagEngine)
    cursor = FreshnessCursor(created_ts, verified_ts, recheck_days)
    engine._get_conn = lambda: type("C", (), {"cursor": lambda _self: cursor})()
    engine._put_conn = lambda *_args, **_kwargs: None
    engine.encode = lambda _text, **_kw: np.array([1.0, 0.0], dtype=np.float32)
    engine._expand_query = lambda query: [query]
    engine.reranker = ReferenceReranker()
    result = engine._retrieve_candidates(make_context(tmp_path), "query", limit=1)
    assert result["status"] == "success", result
    return result["results"][0]


DAY = 86400.0


def test_recent_confirmation_makes_an_old_fact_fresh(tmp_path):
    """Confirming a fact resets its age. That is the point of recording it."""
    now = time.time()
    hit = search_with(now - 400 * DAY, now - 2 * DAY, None, tmp_path)

    assert hit["freshness_status"] == "fresh"
    assert hit["ever_verified"] is True
    assert hit["days_since_confirmation"] < 3


def test_never_confirmed_falls_back_to_write_time(tmp_path):
    now = time.time()
    hit = search_with(now - 400 * DAY, None, None, tmp_path)

    assert hit["freshness_status"] == "stale"
    assert hit["ever_verified"] is False
    assert hit["days_since_confirmation"] > 390


def test_passing_a_declared_recheck_deadline_is_reported_as_overdue(tmp_path):
    """The one case where the store knows a fact may have rotted."""
    now = time.time()
    hit = search_with(now - 90 * DAY, now - 60 * DAY, 30, tmp_path)

    assert hit["freshness_status"] == "overdue"
    assert hit["recheck_after_days"] == 30


def test_an_overdue_fact_is_still_returned(tmp_path):
    """An old fact is not a wrong fact — dropping it trades staleness for a miss."""
    now = time.time()
    hit = search_with(now - 90 * DAY, now - 60 * DAY, 30, tmp_path)

    assert hit["content"] == "content"
    assert hit["final_score"] > 0.0


def test_within_the_deadline_is_not_overdue(tmp_path):
    now = time.time()
    hit = search_with(now - 90 * DAY, now - 10 * DAY, 30, tmp_path)

    assert hit["freshness_status"] != "overdue"


def test_verify_stamps_the_row_and_is_project_scoped(tmp_path):
    connection = RecordingConnection()
    engine = build_engine(connection)

    result = engine.verify(make_context(tmp_path), chunk_id=5, recheck_after_days=30)

    assert result["status"] == "success"
    assert result["updated"] == 1
    query, params = next(
        (q, p) for q, p in connection.calls if "UPDATE public.project_knowledge" in q
    )
    assert "verified_at = now()" in query
    assert "project_id = %s" in query
    assert "root_id = %s" in query
    assert "root-a" in params
    assert 30 in params


def test_verify_refuses_to_confirm_a_retired_fact(tmp_path):
    """Re-confirming something already retired would quietly un-retire it."""
    connection = RecordingConnection()
    engine = build_engine(connection)

    engine.verify(make_context(tmp_path), chunk_id=5)

    query, _ = next(
        (q, p) for q, p in connection.calls if "UPDATE public.project_knowledge" in q
    )
    assert "validation_status NOT IN ('invalidated', 'deprecated')" in query


def test_verify_requires_a_target(tmp_path):
    connection = RecordingConnection()
    engine = build_engine(connection)

    result = engine.verify(make_context(tmp_path))

    assert result["status"] == "error"
    assert not [q for q, _ in connection.calls if "UPDATE" in q]


def test_verify_rejects_a_nonsense_interval(tmp_path):
    connection = RecordingConnection()
    engine = build_engine(connection)

    result = engine.verify(make_context(tmp_path), chunk_id=5, recheck_after_days=0)

    assert result["status"] == "error"


def test_stale_report_orders_by_how_far_past_due(tmp_path):
    connection = RecordingConnection(rows=[
        (5, "preview", "uri", 8, "unverified", 91.0, 30, True),
    ])
    engine = build_engine(connection)

    result = engine.stale_report(make_context(tmp_path), limit=10)

    assert result["status"] == "success"
    assert result["overdue_count"] == 1
    assert result["overdue"][0]["chunk_id"] == 5
    assert result["overdue"][0]["days_since_confirmation"] == 91.0
    query, _ = connection.calls[0]
    assert "recheck_after_days IS NOT NULL" in query
    assert "ORDER BY" in query


def test_stale_report_does_not_touch_the_store(tmp_path):
    connection = RecordingConnection(rows=[])
    engine = build_engine(connection)

    engine.stale_report(make_context(tmp_path))

    assert connection.commits == 0
    assert not [q for q, _ in connection.calls if "UPDATE" in q or "DELETE" in q]


def test_ageing_population_is_opt_in(tmp_path):
    """Facts with no declared interval are judgement, not a rule — ask for them."""
    connection = RecordingConnection(rows=[])
    engine = build_engine(connection)

    without = engine.stale_report(make_context(tmp_path))
    assert len(connection.calls) == 1

    connection.calls.clear()
    with_ageing = engine.stale_report(
        make_context(tmp_path), include_undeclared_after_days=180
    )
    assert len(connection.calls) == 2
    assert without["ageing"] == [] and with_ageing["status"] == "success"
    for query, params in connection.calls:
        assert "root_id = %s" in query
        assert params[:2] == ["project-a", "root-a"]
