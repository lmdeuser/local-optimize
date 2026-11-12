import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("psycopg2")
pytest.importorskip("transformers")

from project_context import ProjectContext, ReadScope, ValidationStatus, WriteVisibility
from rag_engine import RagEngine

pytestmark = pytest.mark.integration


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.last = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        text = str(query)
        self.last = text
        if "INSERT INTO public.project_knowledge" in text:
            assert text.count("%s") == len(params)
            self.connection.knowledge_params.append(tuple(params))
        elif "INSERT INTO public.knowledge_provenance" in text:
            assert text.count("%s") == len(params)
            self.connection.provenance_params.append(tuple(params))

    def fetchone(self):
        if "rag_cache_generation" in self.last:
            # The write retires the cached answers it invalidated, on this same
            # connection. Modelled rather than ignored: reaching for a second
            # connection here is the defect that guard exists to prevent.
            return (7,)
        # RETURNING id, (xmax = 0) AS inserted - the second column is how save
        # tells a genuine insert from an upsert that changed nothing, and so
        # whether the corpus moved at all.
        return (42, True)


class Connection:
    def __init__(self):
        self.knowledge_params = []
        self.provenance_params = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def context(tmp_path):
    return ProjectContext(
        "project-a",
        "Project A",
        "root-a",
        str(tmp_path),
        (str(tmp_path),),
        WriteVisibility.SESSION_PRIVATE,
        ReadScope.SESSION,
        "agent-a",
        "session-a",
        validation_status=ValidationStatus.VALIDATED,
    )


def engine_with(connection):
    engine = object.__new__(RagEngine)
    engine._get_conn = lambda: connection
    engine._put_conn = lambda _conn, error=False: None
    engine.encode = lambda _content, **_kw: np.array([0.1, 0.2], dtype=np.float32)
    return engine


def test_same_content_keeps_distinct_provenance_references(tmp_path):
    connection = Connection()
    engine = engine_with(connection)
    ctx = context(tmp_path)

    first = engine.save(
        ctx,
        "same fixture content from two documents",
        source_uri="fixture://one",
        source_type="document",
        file_path="one.md",
    )
    second = engine.save(
        ctx,
        "same fixture content from two documents",
        source_uri="fixture://two",
        source_type="document",
        file_path="two.md",
    )

    assert first["chunk_id"] == second["chunk_id"] == 42
    assert connection.knowledge_params[0][7] == connection.knowledge_params[1][7]
    assert connection.provenance_params[0][-2] != connection.provenance_params[1][-2]
    assert connection.provenance_params[0][2] == "project-a"
    assert connection.provenance_params[1][2] == "project-a"


def test_session_private_owner_is_written_with_both_ids(tmp_path):
    connection = Connection()
    engine = engine_with(connection)
    result = engine.save(context(tmp_path), "session private fixture content")
    assert result["status"] == "success"
    params = connection.knowledge_params[0]
    assert "agent-a" in params
    assert "session-a" in params
    assert "session-private" in params
