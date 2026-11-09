from dataclasses import replace

import pytest
from project_context import WriteVisibility
from tests.unit.test_duplicate_detection import RecordingConnection, build_engine, make_context

pytestmark = pytest.mark.unit


def test_reindex_reads_and_retires_only_its_own_visibility_and_session(tmp_path):
    connection = RecordingConnection(rows=[])
    engine = build_engine(connection)
    context = replace(make_context(tmp_path), write_visibility=WriteVisibility.SESSION_PRIVATE)
    engine._live_chunk_ids(context, 'source.py', 'code')
    engine._retire_superseded_chunks(context, {7})
    scoped = [(sql, params) for sql, params in connection.calls if 'project_knowledge' in sql]
    assert len(scoped) == 2
    for sql, params in scoped:
        assert 'visibility = %s' in sql and 'agent_id = %s' in sql and 'session_id = %s' in sql
        assert 'session-private' in params and context.agent_id in params and context.session_id in params
