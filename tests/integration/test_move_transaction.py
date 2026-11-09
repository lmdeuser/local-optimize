import pytest

pytest.importorskip("numpy")
pytest.importorskip("psycopg2")
pytest.importorskip("transformers")

from project_context import ProjectContext, ReadScope, WriteVisibility
from rag_engine import RagEngine

pytestmark = pytest.mark.integration


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
        if self.connection.fail_on and self.connection.fail_on in text:
            raise RuntimeError("fixture failure")
        if text.startswith("SELECT id FROM public.kb_documents"):
            self.rows = [(11,)]
        elif "UPDATE public.kb_documents" in text:
            self.rows = [(11,)]
        elif "UPDATE public.project_knowledge" in text:
            self.rows = [(21,)]
        elif "UPDATE public.knowledge_provenance" in text:
            self.rows = [(31,)]
        elif "UPDATE public.kb_entities" in text:
            self.rows = [(41,)]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        # Only the cache bump reads a single row on this path.
        return (7,)


class MoveConnection:
    def __init__(self, fail_on=""):
        self.fail_on = fail_on
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return MoveCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def context(tmp_path):
    return ProjectContext(
        "project-a", "Project A", "root-a", str(tmp_path), (str(tmp_path),),
        WriteVisibility.SHARED, ReadScope.SHARED, "agent-a", "session-a",
    )


def engine(connection):
    instance = object.__new__(RagEngine)
    instance._get_conn = lambda: connection
    instance._put_conn = lambda _connection, error=False: None
    return instance


def test_server_move_updates_metadata_in_one_transaction_and_preserves_ids(tmp_path):
    source = tmp_path / "old.py"
    destination = tmp_path / "new.py"
    destination.write_text("fixture", encoding="utf-8")
    connection = MoveConnection()

    result = engine(connection).move_document(
        context(tmp_path), str(source), str(destination)
    )

    assert result["status"] == "success"
    assert result["moved_count"] == 4
    # Two commits, and the second one is the point: the move commits its own
    # transaction, then retires the cached answers it invalidated using the
    # same connection. That bump needs its own commit - putconn() rolls a
    # returned connection back, so an uncommitted bump would be discarded and
    # every other process would keep serving the pre-move paths.
    assert connection.commits == 2
    assert connection.rollbacks == 0
    assert not any("DELETE " in query for query, _params in connection.calls)


def test_server_move_failure_rolls_back_without_deleting_source(tmp_path):
    source = tmp_path / "old.py"
    destination = tmp_path / "new.py"
    destination.write_text("fixture", encoding="utf-8")
    connection = MoveConnection(fail_on="UPDATE public.project_knowledge")

    result = engine(connection).move_document(
        context(tmp_path), str(source), str(destination)
    )

    assert result == {"status": "error", "message": "Document move failed", "moved_count": 0}
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not any("DELETE " in query for query, _params in connection.calls)
