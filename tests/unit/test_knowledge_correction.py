"""Correcting a stored fact must actually retire the old one.

A stale hit costs more than a miss: the agent acts on it confidently and the
rework burns more than the lookup saved. That makes the correction path load
bearing, and it has to do something a caller can verify.

The documented workflow was "re-save the fact with validation_status=deprecated".
That never retired anything: `validation_status` is part of `dedup_key`, so the
re-save landed on a *different* key and inserted a second row, while
`ON CONFLICT ... DO UPDATE` only ever touched `updated_at`. The original stayed
exactly as retrievable as before.
"""
import pytest

import rag_engine
from project_context import ProjectContext, ReadScope, ValidationStatus, WriteVisibility


pytestmark = pytest.mark.unit


def make_context(tmp_path, *, validation_status=ValidationStatus.UNVERIFIED):
    return ProjectContext(
        "project-a", "Project A", "root-a", str(tmp_path), (str(tmp_path),),
        WriteVisibility.SHARED, ReadScope.SHARED, "agent-a", "session-a",
        validation_status=validation_status,
    )


class RecordingCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        self.connection.calls.append((str(query), params))

    def fetchone(self):
        return (321,)

    def fetchall(self):
        return []

    @property
    def rowcount(self):
        return 1


class RecordingConnection:
    def __init__(self):
        self.calls = []
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
    engine.encode = lambda _text, **_kw: __import__("numpy").zeros(4, dtype="float32")
    return engine


def test_dedup_key_does_not_partition_a_fact_by_its_status(tmp_path):
    """The same text at two statuses is one fact, not two rows.

    While `validation_status` is part of the key, marking something deprecated
    silently creates a twin and leaves the original live and retrievable.
    """
    connection = RecordingConnection()
    engine = build_engine(connection)

    engine.save(make_context(tmp_path), "a fact worth correcting later")
    engine.save(
        make_context(tmp_path, validation_status=ValidationStatus.DEPRECATED),
        "a fact worth correcting later",
    )

    inserts = [
        params for query, params in connection.calls
        if "INSERT INTO public.project_knowledge" in query
    ]
    assert len(inserts) == 2
    dedup_keys = {params[7] for params in inserts}
    assert len(dedup_keys) == 1, (
        "same content produced two dedup keys, so the correction inserted a twin "
        "instead of landing on the original row"
    )


def test_resaving_a_fact_updates_its_status_in_place(tmp_path):
    """`ON CONFLICT` must carry the new status, not just bump `updated_at`.

    Guarded in one direction only: the incoming status is applied, *except* over a
    row already retired, which stays retired. See
    `test_duplicate_detection.test_resaving_does_not_resurrect_a_retired_fact`
    for the other half of that contract.
    """
    connection = RecordingConnection()
    engine = build_engine(connection)

    engine.save(
        make_context(tmp_path, validation_status=ValidationStatus.DEPRECATED),
        "a fact worth correcting later",
    )

    insert = next(
        query for query, _ in connection.calls
        if "INSERT INTO public.project_knowledge" in query
    )
    conflict = " ".join(insert[insert.index("ON CONFLICT"):].split())
    assert "EXCLUDED.validation_status" in conflict, conflict
    assert "updated_at = now()" in conflict, conflict


def test_deprecate_marks_the_existing_row_and_reports_what_it_touched(tmp_path):
    """An explicit correction call, because a re-save cannot express supersession.

    The real workflow has different words for the corrected fact, so its hash
    differs and no re-save will ever collide with the original. Retiring a fact
    has to be its own operation, and it has to tell the caller whether it hit
    anything — a silent no-op is how a store fills up with contradictions.
    """
    connection = RecordingConnection()
    engine = build_engine(connection)

    result = engine.deprecate(
        make_context(tmp_path),
        chunk_id=321,
        reason="vendor shipped a redesign",
        superseded_by=999,
    )

    assert result["status"] == "success"
    assert result["updated"] == 1
    update = next(
        (query, params) for query, params in connection.calls
        if "UPDATE public.project_knowledge" in query
    )
    query, params = update
    assert "validation_status" in query
    assert "project_id = %s" in query, "a correction must not cross project scope"
    assert "deprecated" in params


def test_deprecate_rejects_a_status_that_is_not_a_correction(tmp_path):
    """`deprecate` retires facts; it is not a back door to bless them."""
    connection = RecordingConnection()
    engine = build_engine(connection)

    result = engine.deprecate(
        make_context(tmp_path), chunk_id=321, status="authoritative"
    )

    assert result["status"] == "error"
    assert not [q for q, _ in connection.calls if "UPDATE" in q]


def test_deprecate_requires_a_target(tmp_path):
    """Without a target this would be an unbounded UPDATE over the project."""
    connection = RecordingConnection()
    engine = build_engine(connection)

    result = engine.deprecate(make_context(tmp_path))

    assert result["status"] == "error"
    assert not [q for q, _ in connection.calls if "UPDATE" in q]
