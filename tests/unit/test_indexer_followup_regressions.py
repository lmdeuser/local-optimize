from unittest.mock import Mock

import pytest

import rag_engine
from project_context import ProjectDefinition, ProjectRegistry
from tests.unit.test_reindex_supersedes import Connection, build_engine, make_context


pytestmark = pytest.mark.unit


def test_retained_root_reconciles_files_and_refreshes_context(tmp_path, monkeypatch):
    import watchdog_indexer

    parent = tmp_path / "parent"
    nested = parent / "nested"
    nested.mkdir(parents=True)
    current = ProjectRegistry([ProjectDefinition("parent", "Parent", {"primary": parent})])
    monkeypatch.setattr(watchdog_indexer, "get_project_registry", lambda _settings: current)
    supervisor = watchdog_indexer.WatchdogSupervisor()
    try:
        supervisor.reconcile_registry()
        indexer = supervisor.indexers[("parent", "primary")]
        old_fingerprint = indexer.context.context_fingerprint
        indexer.queue.reconcile = Mock()

        current = ProjectRegistry([
            ProjectDefinition("parent", "Parent", {"primary": parent}),
            ProjectDefinition("nested", "Nested", {"primary": nested}),
        ])
        supervisor.reconcile_registry()

        assert indexer.context.context_fingerprint != old_fingerprint
        indexer.queue.reconcile.assert_called_once()
    finally:
        supervisor.close()


def test_live_chunks_are_limited_to_active_root(tmp_path):
    connection = Connection(existing=[])
    engine = build_engine(connection)
    context = make_context(tmp_path)

    engine._live_chunk_ids(context, "same.py", "code")

    query, params = connection.calls[0]
    assert "root_id = %s" in query
    assert context.root_id in params


def test_delete_is_limited_to_active_root(tmp_path):
    source = tmp_path / "same.md"
    source.write_text("content", encoding="utf-8")
    connection = Connection(existing=[])
    engine = build_engine(connection)
    engine._invalidate_search_cache = lambda *_args, **_kwargs: None
    context = make_context(tmp_path)

    engine.delete_document(context, str(source))

    query, params = connection.calls[0]
    assert "root_id = %s" in query
    assert context.root_id in params


def test_move_selects_and_updates_only_active_root(tmp_path):
    source = tmp_path / "same.md"
    destination = tmp_path / "renamed.md"
    source.write_text("source", encoding="utf-8")
    destination.write_text("destination", encoding="utf-8")
    connection = Connection(existing=[])
    engine = build_engine(connection)
    context = make_context(tmp_path)

    engine.move_document(context, str(source), str(destination))

    document_query, document_params = connection.calls[0]
    assert "root_id = %s" in document_query
    assert context.root_id in document_params
    knowledge_query, knowledge_params = connection.calls[1]
    assert "root_id = %s" in knowledge_query
    assert context.root_id in knowledge_params


def test_reverting_source_can_reactivate_only_through_indexing(tmp_path):
    class Cursor:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=()):
            self.connection.calls.append((" ".join(str(query).split()), list(params)))

        def fetchone(self):
            return (7, True)

    class SaveConnection(Connection):
        def cursor(self):
            return Cursor(self)

    connection = SaveConnection()
    engine = build_engine(connection)
    engine.encode = lambda *_args, **_kwargs: type("Vector", (), {"tolist": lambda _self: [0.0]})()
    engine._invalidate_search_cache = lambda *_args, **_kwargs: None
    context = make_context(tmp_path)

    result = engine.save(context, "restored source content", restore_source=True)

    assert result["status"] == "success"
    query, params = connection.calls[0]
    assert "WHEN %s THEN EXCLUDED.validation_status" in query
    assert True in params


def test_document_indexing_marks_source_restore_explicitly(tmp_path):
    source = tmp_path / "note.md"
    source.write_text("restored source content that is long enough " * 3, encoding="utf-8")
    engine = object.__new__(rag_engine.RagEngine)
    engine._validate_index_file = lambda *_args: (source, None)
    engine._chunk_text = lambda *_args, **_kwargs: [source.read_text()]
    engine._live_chunk_ids = lambda *_args: set()
    engine._retire_superseded_chunks = lambda *_args: 0
    engine._invalidate_search_cache = lambda *_args, **_kwargs: None
    saved = []
    engine.save = lambda **kwargs: saved.append(kwargs) or {"status": "success", "chunk_id": 7}

    result = engine.index_document(make_context(tmp_path), str(source))

    assert result["status"] == "success"
    assert saved[0]["restore_source"] is True



def test_retained_root_reconciles_files_on_every_interval(tmp_path, monkeypatch):
    import watchdog_indexer

    root = tmp_path / "root"
    root.mkdir()
    current = ProjectRegistry([ProjectDefinition("root", "Root", {"primary": root})])
    monkeypatch.setattr(watchdog_indexer, "get_project_registry", lambda _settings: current)
    supervisor = watchdog_indexer.WatchdogSupervisor()
    try:
        supervisor.reconcile_registry()
        indexer = supervisor.indexers[("root", "primary")]
        indexer.queue.reconcile = Mock()

        supervisor.reconcile_registry()
        supervisor.reconcile_registry()

        assert indexer.queue.reconcile.call_count == 2
    finally:
        supervisor.close()
