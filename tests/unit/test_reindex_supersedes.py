"""Re-indexing an edited file must retire the chunks it replaced.

Editing a file shifts chunk boundaries, so the new text hashes differently, misses
the `dedup_key` conflict, and lands as *additional* rows. Without retirement a
file accumulates one full set of chunks per edit — all live, all retrievable, each
an equally confident answer about a file that has since changed.

Observed on the live store: `migrations/manifest.json` had grown to 39 live
chunks across three ingestions, making it the second-largest document in a
knowledge base of skill documentation.
"""
import pytest

import rag_engine
from project_context import ProjectContext, ReadScope, WriteVisibility


pytestmark = pytest.mark.unit


def make_context(tmp_path):
    return ProjectContext(
        "project-a", "Project A", "root-a", str(tmp_path), (str(tmp_path),),
        WriteVisibility.SHARED, ReadScope.SHARED, "agent-a", "session-a",
    )


class Cursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        self.connection.calls.append((" ".join(str(query).split()), list(params)))

    def fetchall(self):
        return [(cid,) for cid in self.connection.existing]

    def fetchone(self):
        return (99,)

    @property
    def rowcount(self):
        return len(self.connection.existing)


class Connection:
    def __init__(self, existing=()):
        self.calls = []
        self.existing = list(existing)
        self.commits = 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def build_engine(connection):
    engine = object.__new__(rag_engine.RagEngine)
    engine._get_conn = lambda: connection
    engine._put_conn = lambda *_args, **_kwargs: None
    return engine


def test_superseded_chunks_are_retired_not_deleted(tmp_path):
    """Provenance must survive: the row is marked, never removed."""
    connection = Connection()
    engine = build_engine(connection)

    retired = engine._retire_superseded_chunks(make_context(tmp_path), {11, 12})

    query, params = connection.calls[0]
    assert "UPDATE public.project_knowledge" in query
    assert "invalidated" in query
    assert "DELETE" not in query
    assert "project_id = %s" in query, "retirement must not cross project scope"
    assert retired == 0 or retired >= 0


def test_failed_reindex_preserves_previous_chunks(tmp_path):
    source = tmp_path / "a.md"
    source.write_text("New text " * 20)
    engine = build_engine(Connection())
    engine._chunk_text = lambda *a, **k: ["New text " * 20]
    engine._live_chunk_ids = lambda *a: {11, 12}
    engine.save = lambda **k: {"status": "error"}
    retired = []
    engine._retire_superseded_chunks = lambda *a: retired.append(a) or 2
    engine._invalidate_search_cache = lambda *a: None
    result = engine.index_document(make_context(tmp_path), str(source))
    assert result["status"] == "error"
    assert retired == []


def test_chunks_that_survived_the_edit_are_not_retired(tmp_path):
    """Unchanged text collides on dedup_key and keeps its id, so it is untouched."""
    connection = Connection()
    engine = build_engine(connection)

    engine._retire_superseded_chunks(make_context(tmp_path), set())

    assert connection.calls == [], "an empty supersession set must not issue SQL"


def test_lookup_of_existing_chunks_ignores_already_retired_rows(tmp_path):
    connection = Connection(existing=[5, 6])
    engine = build_engine(connection)

    ids = engine._live_chunk_ids(make_context(tmp_path), "docs/a.md", "document")

    query, params = connection.calls[0]
    assert "validation_status NOT IN ('invalidated', 'deprecated')" in query
    assert "file_path = %s" in query and "source_type = %s" in query
    assert ids == {5, 6}


def test_index_document_retires_the_previous_generation(tmp_path, monkeypatch):
    """The whole point: a second indexing pass supersedes the first."""
    source = tmp_path / "note.md"
    source.write_text("x" * 400, encoding="utf-8")

    engine = object.__new__(rag_engine.RagEngine)
    engine._chunk_text = lambda text, max_tokens=400: [text]
    engine._validate_index_file = lambda ctx, path, exts: (source, None)
    engine._live_chunk_ids = lambda ctx, rel, st: {1, 2, 3}
    engine.save = lambda **kwargs: {"status": "success", "chunk_id": 2}
    retired_with = {}
    engine._retire_superseded_chunks = lambda ctx, ids: retired_with.setdefault("ids", ids) and 0

    result = engine.index_document(make_context(tmp_path), str(source))

    assert result["status"] == "success"
    assert retired_with["ids"] == {1, 3}, (
        "chunk 2 was rewritten and must survive; 1 and 3 are superseded"
    )
