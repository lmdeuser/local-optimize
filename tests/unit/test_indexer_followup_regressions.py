import hashlib
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
    supervisor = watchdog_indexer.WatchdogSupervisor(watch_paths=[parent])
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
    supervisor = watchdog_indexer.WatchdogSupervisor(watch_paths=[root])
    try:
        supervisor.reconcile_registry()
        indexer = supervisor.indexers[("root", "primary")]
        indexer.queue.reconcile = Mock()

        supervisor.reconcile_registry()
        supervisor.reconcile_registry()

        assert indexer.queue.reconcile.call_count == 2
    finally:
        supervisor.close()



def test_provenance_reference_key_includes_root(tmp_path):
    class SaveCursor:
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
            return SaveCursor(self)

    connection = SaveConnection(existing=[])
    engine = build_engine(connection)
    engine.encode = lambda *_args, **_kwargs: type("Vector", (), {"tolist": lambda _self: [0.0]})()
    context = make_context(tmp_path)

    result = engine.save(context, "root scoped provenance fixture")

    assert result["status"] == "success"
    query, params = next(call for call in connection.calls if "INSERT INTO public.knowledge_provenance" in call[0])
    assert "root_id" in query
    assert params[3] == context.root_id
    material = "\x1f".join(
        (context.project_id, context.root_id, "", "agent_generated", "llm_output", "", "", "")
    )
    assert params[-2] == hashlib.sha256(material.encode("utf-8")).hexdigest()


def test_delete_invalidates_only_after_last_provenance_reference(tmp_path):
    connection = Connection(existing=[7])
    engine = build_engine(connection)
    engine._invalidate_search_cache = lambda *_args, **_kwargs: None
    context = make_context(tmp_path)

    result = engine.delete_document(context, str(tmp_path / "missing.py"))

    assert result["status"] == "success"
    query, params = connection.calls[0]
    assert query.startswith("DELETE FROM public.knowledge_provenance")
    assert "p.root_id = %s" in query
    invalidate_query, _invalidate_params = connection.calls[1]
    assert "NOT EXISTS (SELECT 1 FROM public.knowledge_provenance p" in invalidate_query
    assert params.count(context.root_id) >= 2


def test_search_uses_live_alternate_provenance_when_original_is_missing(tmp_path):
    from source_evidence import read_snapshot

    root = tmp_path / "root"
    root.mkdir()
    missing = root / "missing.py"
    live = root / "live.py"
    live.write_text("print('live')", encoding="utf-8")
    snapshot = read_snapshot(live)
    reference = (
        7,
        11,
        str(live),
        "code",
        "live.py",
        {"source_snapshot": snapshot},
        "root-a",
    )

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _query, _params):
            pass

        def fetchall(self):
            return [reference]

    class Conn:
        def cursor(self):
            return Cursor()

    engine = object.__new__(rag_engine.RagEngine)
    engine._get_conn = lambda: Conn()
    engine._put_conn = lambda *_args, **_kwargs: None
    registry = ProjectRegistry([ProjectDefinition("project-a", "Project A", {"root-a": root})])
    original_registry = rag_engine.get_project_registry
    rag_engine.get_project_registry = lambda _settings: registry
    try:
        result = engine._check_source_versions_with_provenance(
            "project-a",
            {
                "status": "success",
                "results": [
                    {
                        "chunk_id": 7,
                        "source_uri": str(missing),
                        "source_type": "code",
                        "file_path": "missing.py",
                        "root_id": "root-a",
                        "entity_id": 99,
                        "metadata": {
                            "entity_id": 99,
                            "source_snapshot": {"mtime_ns": 1, "size": 1},
                        },
                        "final_score": 0.9,
                    }
                ],
            },
        )
    finally:
        rag_engine.get_project_registry = original_registry

    assert "unusable_sources_omitted" not in result
    assert len(result["results"]) == 1
    hit = result["results"][0]
    assert hit["source_uri"] == str(live)
    assert hit["root_id"] == "root-a"
    assert hit["entity_id"] is None
    assert "entity_id" not in hit["metadata"]
    assert result["top_score"] == 0.9



def test_deprecate_retires_graph_evidence_in_same_scope(tmp_path):
    connection = Connection(existing=[1])
    engine = build_engine(connection)

    result = engine.deprecate(make_context(tmp_path), chunk_id=1)

    assert result["status"] == "success"
    for table in ("entity_evidence", "relation_evidence"):
        query, params = next(call for call in connection.calls if f"UPDATE public.{table}" in call[0])
        assert "is_active = false" in query
        assert "root_id = %s" in query
        assert "chunk_id IN (SELECT id FROM public.project_knowledge" in query
        assert params[2] == "root-a"


def test_upserts_reactivate_current_graph_evidence(tmp_path):
    class Cursor:
        def __init__(self):
            self.calls = []

        def execute(self, query, params=()):
            self.calls.append((" ".join(str(query).split()), list(params)))

        def fetchone(self):
            return (7,)

    context = make_context(tmp_path)
    engine = object.__new__(rag_engine.RagEngine)
    cursor = Cursor()
    engine._upsert_entity(cursor, context, "file", "key", "file", "description", {}, source_ref="a.py")
    engine._upsert_relation(cursor, context, 7, 7, "contains", source_ref="a.py")

    entity_update = next(call for call in cursor.calls if call[0].startswith("UPDATE public.entity_evidence"))
    relation_update = next(call for call in cursor.calls if call[0].startswith("UPDATE public.relation_evidence"))
    for query, _params in (entity_update, relation_update):
        assert "is_active = true" in query
        assert "root_id = %s" in query
        assert "source_ref = %s" in query


def test_move_uses_root_and_owner_scoped_stable_key_prefix(tmp_path):
    source = tmp_path / "old.py"
    destination = tmp_path / "new.py"
    source.write_text("source", encoding="utf-8")
    destination.write_text("destination", encoding="utf-8")
    connection = Connection(existing=[])
    engine = build_engine(connection)

    engine.move_document(make_context(tmp_path), str(source), str(destination))

    _query, params = next(call for call in connection.calls if "UPDATE public.kb_entities SET stable_key" in call[0])
    assert params[0] == "project-a:root-a:shared:::new.py:"
    assert params[5] == "project-a:root-a:shared:::old.py:%"
