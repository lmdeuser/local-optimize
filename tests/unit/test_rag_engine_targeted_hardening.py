import hashlib
import sys
import threading
import types

import numpy as np
import pytest

import rag_engine
from tests.similarity_reference import ReferenceReranker
from project_context import ProjectContext, ProjectDefinition, ProjectRegistry, ReadScope, WriteVisibility


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def registered_test_context(monkeypatch, tmp_path):
    registry = ProjectRegistry([ProjectDefinition("project-a", "Project A", {"root-a": tmp_path})])
    monkeypatch.setattr(rag_engine, "get_project_registry", lambda _settings: registry)


def make_context(tmp_path, *, visibility=WriteVisibility.SESSION_PRIVATE):
    return ProjectContext(
        "project-a", "Project A", "root-a", str(tmp_path), (str(tmp_path),),
        visibility, ReadScope.SESSION, "agent-a", "session-a",
    )


def test_init_verifies_schema_before_local_ml_components(monkeypatch, tmp_path):
    events = []
    local_model = tmp_path / "model"
    local_model.mkdir()

    class FakePool:
        def __init__(self, **kwargs):
            events.append(("pool", kwargs["minconn"]))

        def closeall(self):
            events.append(("close", None))

    class FakeTokenizer:
        @staticmethod
        def from_pretrained(path, **kwargs):
            events.append(("tokenizer", (path, kwargs)))
            return object()

    monkeypatch.setattr(rag_engine, "TOKENIZER_PATH", str(local_model))
    monkeypatch.setattr(rag_engine, "MODEL_PATH", str(local_model))
    monkeypatch.setattr(rag_engine, "get_db_params", lambda: {})
    monkeypatch.setattr(rag_engine.pgpool, "ThreadedConnectionPool", FakePool)
    monkeypatch.setattr(rag_engine.RagEngine, "_ensure_schema", lambda self: events.append(("schema", None)))
    monkeypatch.setattr(rag_engine, "AutoTokenizer", FakeTokenizer)
    monkeypatch.setattr(rag_engine, "XpuReranker", lambda: events.append(("reranker", None)) or object())
    monkeypatch.setattr(rag_engine.RagEngine, "_load_model", lambda self: events.append(("model", None)))
    monkeypatch.setattr(rag_engine.EvidenceRanker, "warm_up", lambda self: events.append(("evidence", None)))

    engine = rag_engine.RagEngine()

    assert [event[0] for event in events[:5]] == ["pool", "schema", "tokenizer", "reranker", "model"]
    assert events[0][1] == 1
    assert events[2][1][1]["local_files_only"] is True
    assert events[5][0] == "evidence"
    engine.close()


def test_index_document_rejects_symlink_oversize_and_extension_before_read(monkeypatch, tmp_path):
    engine = object.__new__(rag_engine.RagEngine)
    context = make_context(tmp_path)
    target = tmp_path / "target.md"
    target.write_text("safe content", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(target)

    assert engine.index_document(context, str(link))["error"]["code"] == "INDEX_FILE_REJECTED"

    oversized = tmp_path / "large.md"
    oversized.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(rag_engine.settings, "max_file_size_bytes", 4)
    assert "max_file_size_bytes" in engine.index_document(context, str(oversized))["error"]["message"]

    forbidden = tmp_path / "payload.exe"
    forbidden.write_bytes(b"MZ")
    assert "extension" in engine.index_document(context, str(forbidden))["error"]["message"]


def test_directory_indexing_is_bounded_and_reports_partial(monkeypatch, tmp_path):
    context = make_context(tmp_path)
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 2", encoding="utf-8")
    engine = object.__new__(rag_engine.RagEngine)
    engine.index_code = lambda _context, path: {"status": "success", "path": path}
    engine.index_document = lambda _context, path: {"status": "success", "path": path}
    monkeypatch.setattr(rag_engine, "INDEX_DIRECTORY_MAX_FILES", 1)

    result = rag_engine.RagEngine.index_code(engine, context, str(tmp_path))

    assert result["status"] == "partial"
    assert result["partial"] is True
    assert result["stop_reason"] == "file_count"
    assert result["scanned_files"] == 1


class GraphCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        text = str(query)
        self.connection.calls.append((text, tuple(params)))
        if text.startswith("SET LOCAL statement_timeout"):
            self.rows = []
        elif "FROM public.kb_entities" in text:
            assert "id = ANY(%s)" in text
            assert "ORDER BY e.id LIMIT %s" in text or "ORDER BY id LIMIT %s" in text
            self.rows = [(7, "Seed", "class", "seed", "seed-key")]
        elif "FROM public.kb_relations" in text:
            limit = int(params[-1])
            assert limit <= 12
            self.rows = []

    def fetchall(self):
        return self.rows


class GraphConnection:
    def __init__(self):
        self.calls = []
        self.rollbacks = 0

    def cursor(self):
        return GraphCursor(self)

    def rollback(self):
        self.rollbacks += 1


def test_agentic_search_uses_hit_entity_ids_and_canonical_results(tmp_path):
    context = make_context(tmp_path)
    connection = GraphConnection()
    engine = object.__new__(rag_engine.RagEngine)
    engine._get_conn = lambda: connection
    engine._put_conn = lambda _connection, error=False: None
    engine.search = lambda *_args, **_kwargs: {
        "status": "success",
        "results": [{"content": "hit", "metadata": {"entity_id": 7}, "score": 1.0}],
    }
    engine.ram_pages = {}
    engine._ram_lock = threading.RLock()
    engine.ram_allocate = lambda *_args, **_kwargs: None
    engine._sanitize_context = lambda value: value
    engine.minimize_context_tokens = lambda hits, graph, **_kwargs: (hits, graph)

    result = engine.agentic_search(context, "unrelated name", max_depth=2)

    assert result["status"] == "success"
    assert result["results"][0]["metadata"]["entity_id"] == 7
    assert "vector_hits" not in result
    entity_queries = [query for query, _ in connection.calls if "FROM public.kb_entities" in query]
    assert entity_queries and all("id = ANY(%s)" in query for query in entity_queries)
    assert connection.rollbacks == 1


class SearchCursor:
    def __init__(self):
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        text = str(query)
        if "websearch_to_tsquery" in text:
            self.rows = []
        else:
            self.rows = [(
                1, "content", {}, [1.0, 0.0], "hash", 1.0, None, None, "uri",
                "code", "validated", None, "0", "1", "local", "", "", "shared", 77,
                "docs/socket.cpp", 42, "/project/docs/socket.cpp", "code", "project-a", "root-a",
            )]

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class SearchConnection:
    def cursor(self):
        return SearchCursor()


def test_search_copies_entity_id_into_hit_metadata(tmp_path):
    engine = object.__new__(rag_engine.RagEngine)
    engine._get_conn = SearchConnection
    engine._put_conn = lambda *_args, **_kwargs: None
    engine.encode = lambda _text, **_kw: np.array([1.0, 0.0], dtype=np.float32)
    engine._expand_query = lambda query: [query]
    engine.reranker = ReferenceReranker()

    result = engine._retrieve_candidates(make_context(tmp_path), "query", limit=1)

    assert result["status"] == "success"
    assert result["results"][0]["metadata"]["entity_id"] == 77
    assert result["results"][0]["file_path"] == "docs/socket.cpp"
    assert result["results"][0]["document_id"] == 42
    assert result["results"][0]["source_uri"] == "/project/docs/socket.cpp"


class MoveCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        text = str(query)
        self.connection.calls.append((text, tuple(params)))
        if text.startswith("SELECT id FROM public.kb_documents"):
            assert "k.visibility = %s" in text
            assert "k.agent_id = %s" in text
            assert "k.session_id = %s" in text
            self.rows = [(11,)]
        elif "UPDATE public.kb_documents" in text:
            self.rows = [(11,)]
        elif "UPDATE public.project_knowledge" in text:
            assert "k.visibility = %s" in text
            self.rows = [(21,)]
        elif text.startswith("UPDATE public.knowledge_provenance SET file_path"):
            self.rows = [(31,)]
        elif text.startswith("SELECT id, knowledge_id"):
            self.rows = [(31, 21, 11, str(self.connection.destination), "code", "new.py", "0", "1")]
        elif text.startswith("UPDATE public.knowledge_provenance SET reference_key"):
            self.connection.reference_key = params[0]
            self.rows = []
        elif "UPDATE public.kb_entities" in text:
            assert "visibility = %s" in text
            assert "agent_id = %s" in text
            assert "session_id = %s" in text
            self.rows = [(41,)]

    def fetchall(self):
        return self.rows

    @property
    def rowcount(self):
        return 0


class MoveConnection:
    def __init__(self, destination):
        self.destination = destination
        self.calls = []
        self.reference_key = None
        self.commits = 0

    def cursor(self):
        return MoveCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_move_recomputes_provenance_in_owner_scoped_transaction(tmp_path):
    source = tmp_path / "old.py"
    destination = tmp_path / "new.py"
    destination.write_text("fixture", encoding="utf-8")
    connection = MoveConnection(destination)
    engine = object.__new__(rag_engine.RagEngine)
    engine._get_conn = lambda: connection
    engine._put_conn = lambda *_args, **_kwargs: None

    result = engine.move_document(make_context(tmp_path), str(source), str(destination))

    material = "\x1f".join(
        ("project-a", "11", str(destination), "code", "new.py", "0", "1")
    )
    assert result["status"] == "success"
    assert result["moved_count"] == 4
    assert connection.reference_key == hashlib.sha256(material.encode("utf-8")).hexdigest()
    assert connection.commits == 1


def test_search_dedup_fallback_hash_is_fips_safe(monkeypatch, tmp_path):
    """The in-memory dedup key must never call a FIPS-disabled digest."""
    calls = []
    real_sha256 = hashlib.sha256

    def guarded_md5(*_args, **_kwargs):
        raise AssertionError("md5 must not be used on the search path")

    def tracking_sha256(data=b"", **kwargs):
        calls.append(kwargs)
        return real_sha256(data, **kwargs)

    monkeypatch.setattr(rag_engine.hashlib, "md5", guarded_md5)
    monkeypatch.setattr(rag_engine.hashlib, "sha256", tracking_sha256)

    class NoHashCursor(SearchCursor):
        def execute(self, query, params=()):
            super().execute(query, params)
            # Simulate rows whose server-side content_hash is absent.
            self.rows = [row[:4] + (None,) + row[5:] for row in self.rows]

    engine = object.__new__(rag_engine.RagEngine)
    engine._get_conn = lambda: type("C", (), {"cursor": lambda _self: NoHashCursor()})()
    engine._put_conn = lambda *_args, **_kwargs: None
    engine.encode = lambda _text, **_kw: np.array([1.0, 0.0], dtype=np.float32)
    engine._expand_query = lambda query: [query]
    engine.reranker = ReferenceReranker()

    result = engine._retrieve_candidates(make_context(tmp_path), "query", limit=1)

    assert result["status"] == "success"
    assert calls, "the dedup fallback digest was never computed"
    assert all(call.get("usedforsecurity") is False for call in calls)


def test_save_writes_configured_embedding_provenance(monkeypatch, tmp_path):
    """embedding_model/version/dimension must be bound parameters, not SQL literals."""
    monkeypatch.setattr(rag_engine, "EMBEDDING_MODEL_NAME", "fixture-model")
    monkeypatch.setattr(rag_engine, "EMBEDDING_VERSION", "9.9")
    monkeypatch.setattr(rag_engine, "EMBEDDING_DIM", 8)

    captured = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=()):
            text = str(query)
            if "INSERT INTO public.project_knowledge" in text:
                assert "all-MiniLM-L6-v2" not in text
                assert text.count("%s") == len(params)
                captured["knowledge"] = tuple(params)

        def fetchone(self):
            # (id, inserted) — the upsert reports whether it actually created a
            # row, because a conflict on dedup_key means this exact content was
            # already stored and no cached search needs retiring.
            return (5, True)

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            return None

        def rollback(self):
            return None

    engine = object.__new__(rag_engine.RagEngine)
    engine._get_conn = Connection
    engine._put_conn = lambda *_args, **_kwargs: None
    engine.encode = lambda _content, **_kw: np.array([0.1, 0.2], dtype=np.float32)

    result = engine.save(make_context(tmp_path), "long enough fixture content")

    assert result["status"] == "success"
    params = captured["knowledge"]
    assert "fixture-model" in params
    assert "9.9" in params
    assert 8 in params


def test_strict_gpu_forbids_sentence_transformer_cpu_fallback(monkeypatch, tmp_path):
    """Intel-only inference must fail closed instead of silently degrading to CPU."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    monkeypatch.setattr(rag_engine, "MODEL_PATH", str(model_dir))

    fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False))
    fake_st = types.SimpleNamespace(SentenceTransformer=lambda *a, **k: object())
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    engine = object.__new__(rag_engine.RagEngine)
    with pytest.raises(RuntimeError, match="Intel XPU"):
        engine._load_model()


class RecordingCursor:
    """Cursor that records statements and serves canned rows per matcher."""

    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        text = str(query)
        self.connection.calls.append((text, tuple(params)))
        assert text.count("%s") == len(params), text
        for needle, rows in self.connection.responses:
            if needle in text:
                self.rows = list(rows)
                return
        self.rows = []

    def fetchall(self):
        return self.rows


class RecordingConnection:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _engine_on(connection):
    engine = object.__new__(rag_engine.RagEngine)
    engine._get_conn = lambda: connection
    engine._put_conn = lambda *_args, **_kwargs: None
    return engine


def test_consolidate_matches_episodes_in_a_single_set_based_statement(tmp_path):
    """Matching must not degrade to O(episodes x entities) round trips in Python."""
    connection = RecordingConnection([("RETURNING k.id", [(11,), (12,)])])
    engine = _engine_on(connection)

    result = engine.consolidate(make_context(tmp_path))

    assert result["status"] == "success"
    assert result["linked"] == 2
    matching = [call for call in connection.calls if "CROSS JOIN LATERAL" in call[0]]
    assert len(matching) == 1, "matching must be one set-based statement, not a loop"
    # Two statements total, and the second one is bookkeeping that does not scale
    # with the episode count: consolidate links episodes to entities, which
    # changes what search returns, so it retires the cached answers. Spelled out
    # rather than counted loosely - a third statement appearing here is either a
    # reintroduced loop or a write nobody accounted for, and both are worth a
    # failing test.
    assert len(connection.calls) == 2, "the matching statement, plus one cache bump"
    assert "rag_cache_generation" in connection.calls[1][0]
    sql, params = matching[0]
    assert "CROSS JOIN LATERAL" in sql
    assert "position(lower(ent.name) in lower(ep.content))" in sql
    # Episode read, entity read and update target are each project-scoped.
    assert params.count("project-a") == 3
    for scoped in ("pk.visibility", "ent.visibility", "k.visibility"):
        assert scoped in sql


def test_delete_document_limits_orphan_cleanup_to_the_deleted_file(tmp_path):
    """Orphan cleanup must target rows that lost evidence, not the whole project."""
    connection = RecordingConnection(
        [
            ("UPDATE public.project_knowledge", [(1,)]),
            ("RETURNING relation_id", [(70,), (70,), (71,)]),
            ("RETURNING entity_id", [(80,), (81,)]),
        ]
    )
    engine = _engine_on(connection)

    result = engine.delete_document(make_context(tmp_path), str(tmp_path / "target.py"))

    assert result["status"] == "success"
    statements = [sql for sql, _ in connection.calls]
    relation_delete = next(s for s in statements if "DELETE FROM public.kb_relations" in s)
    entity_delete = next(s for s in statements if "DELETE FROM public.kb_entities" in s)

    # The pre-fix statements scanned every graph row sharing the project_id.
    assert "kr.id = ANY(%s)" in relation_delete
    assert "ke.id = ANY(%s)" in entity_delete
    relation_params = next(p for s, p in connection.calls if "DELETE FROM public.kb_relations" in s)
    entity_params = next(p for s, p in connection.calls if "DELETE FROM public.kb_entities" in s)
    assert relation_params == ("project-a", [70, 71])
    assert entity_params == ("project-a", [80, 81])

    # Evidence deletion itself must stay inside the caller project/root scope.
    for sql, params in connection.calls:
        if "DELETE FROM public.relation_evidence" in sql or "DELETE FROM public.entity_evidence" in sql:
            assert "project_id = %s AND root_id = %s" in sql
            assert params[:2] == ("project-a", "root-a")


def test_delete_document_neutralises_like_wildcards_in_paths(tmp_path):
    """A path holding LIKE metacharacters must not widen the evidence match."""
    connection = RecordingConnection([("UPDATE public.project_knowledge", [(1,)])])
    engine = _engine_on(connection)

    engine.delete_document(make_context(tmp_path), str(tmp_path / "we_ird%name.py"))

    evidence = [
        (sql, params)
        for sql, params in connection.calls
        if "DELETE FROM public.entity_evidence" in sql
    ]
    assert evidence, "entity evidence must be pruned"
    sql, params = evidence[0]
    assert "ESCAPE '\\'" in sql
    assert params[-1] == "%:we\\_ird\\%name.py:%"


def test_upsert_entity_and_relation_clear_owner_for_shared_visibility(tmp_path):
    """Shared/public entities and relations must store NULL agent_id/session_id."""
    connection = RecordingConnection([("INSERT INTO public.kb_entities", [(10,)]), ("INSERT INTO public.kb_relations", [(20,)])])

    class SingleRowCursor(RecordingCursor):
        def fetchone(self):
            return self.rows[0] if self.rows else (10,)

    connection.cursor = lambda: SingleRowCursor(connection)
    engine = _engine_on(connection)
    ctx = make_context(tmp_path, visibility=WriteVisibility.SHARED)

    with connection.cursor() as cur:
        ent_id = engine._upsert_entity(cur, ctx, "SharedEnt", "k1", "class", "desc", {})
        rel_id = engine._upsert_relation(cur, ctx, ent_id, ent_id, "defines")

    assert ent_id == 10
    assert rel_id == 20
    ent_insert_params = next(p for s, p in connection.calls if "INSERT INTO public.kb_entities" in s)
    rel_insert_params = next(p for s, p in connection.calls if "INSERT INTO public.kb_relations" in s)
    assert ent_insert_params[:4] == ("project-a", "shared", None, None)
    assert rel_insert_params[:4] == ("project-a", "shared", None, None)


def test_vmc_flush_and_page_fault_round_trip_and_cross_session_pinned_eviction(monkeypatch, tmp_path):
    """ram_flush_page and ram_page_fault must agree on stable_key and never double-prefix page_id."""
    from dataclasses import replace

    stored = {}

    class VmcCursor:
        def __init__(self):
            self.row = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=()):
            text = str(query)
            if "INSERT INTO public.kb_entities" in text:
                # params: project_id, vis, agent, session, name, stable_key, type, description, metadata
                stored[params[5]] = (params[6], params[7], params[8])
                self.row = (101,)
            elif "SELECT type, description, metadata FROM public.kb_entities" in text:
                keys = params[1]
                self.row = next((stored[k] for k in keys if k in stored), None)
            else:
                self.row = (1,)

        def fetchone(self):
            return self.row

    class VmcConn:
        def cursor(self):
            return VmcCursor()

        def commit(self):
            pass

        def rollback(self):
            pass

    conn = VmcConn()
    engine = _engine_on(conn)
    engine._ram_lock = threading.RLock()
    engine.ram_pages = {}
    engine.count_tokens = len
    monkeypatch.setattr(rag_engine, "MAX_RAM_TOKENS", 12)

    ctx1 = make_context(tmp_path)
    ctx2 = replace(ctx1, session_id="session-b")

    # Allocate a dirty page in ctx1, then evict it via a pinned guideline in ctx1 and ctx2
    engine.ram_allocate(ctx1, "MyClass", "body1", "skeleton", importance=5, pinned=False)
    scoped_key = f"{ctx1.project_id}:{ctx1.agent_id}:{ctx1.session_id}:MyClass"
    engine.ram_pages[scoped_key].dirty = True

    # Pinned system_guideline pages across sessions must not brick RAM when MAX_RAM_TOKENS is reached
    engine.ram_allocate(ctx1, "cognitive_shield", "shield1234", "system_guideline", importance=10, pinned=True)
    engine.ram_allocate(ctx2, "cognitive_shield", "shield5678", "system_guideline", importance=10, pinned=True)

    assert scoped_key not in engine.ram_pages
    assert stored, "dirty victim page must have been flushed to kb_entities"

    # Page fault must recover the flushed page without double-prefixing its key
    engine.ram_pages.clear()
    assert engine.ram_page_fault(ctx1, "MyClass") is True
    assert scoped_key in engine.ram_pages
    assert engine.ram_pages[scoped_key].page_type == "skeleton"
    assert engine.ram_pages[scoped_key].content == "body1"


@pytest.mark.parametrize("fail_write", [False, True])
def test_index_code_extracts_async_functions_and_indexes_vector_chunks(tmp_path, fail_write):
    """index_code must index async def functions/methods and write entity-linked chunks via save()."""
    source = tmp_path / "service.py"
    source.write_text(
        "class Worker:\n"
        "    async def run_async(self, x: int) -> int:\n"
        "        '''Async method doc.'''\n"
        "        return x + 1\n\n"
        "async def top_async() -> None:\n"
        "    '''Top async doc.'''\n"
        "    return None\n",
        encoding="utf-8",
    )

    class AstCursor:
        def __init__(self):
            self._next_id = 1
            self.rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=()):
            if "SELECT stable_key, name, id FROM public.kb_entities" in str(query):
                self.rows = []

        def fetchone(self):
            val = self._next_id
            self._next_id += 1
            return (val,)

        def fetchall(self):
            return self.rows

    class AstConn:
        def cursor(self):
            return AstCursor()

        def commit(self):
            pass

        def rollback(self):
            pass

    engine = _engine_on(AstConn())
    engine._ram_lock = threading.RLock()
    engine.ram_pages = {}
    engine.encode = lambda *_a, **_kw: np.zeros(4, dtype=np.float32)
    engine._live_chunk_ids = lambda *_a: {999}
    saved = []
    retired = []
    invalidated = []
    def save(**kw):
        saved.append(kw)
        return {"status": "error" if fail_write and len(saved) == 2 else "success", "chunk_id": len(saved)}
    engine.save = save
    engine._retire_superseded_chunks = lambda _ctx, ids: retired.append(ids) or len(ids)
    engine._invalidate_search_cache = lambda _ctx: invalidated.append(True)

    res = engine.index_code(make_context(tmp_path), str(source))

    if fail_write:
        assert res["status"] == "error"
        assert retired == []
        assert invalidated == [True]
        return

    assert res["status"] == "success"
    assert "Worker" in res["extracted_entities"]
    assert "Worker.run_async" in res["extracted_entities"]
    assert "top_async" in res["extracted_entities"]
    assert len(saved) == 3
    assert all(item["metadata"].get("entity_id") is not None for item in saved)
    assert retired == [{999}]
    assert invalidated == [True]

