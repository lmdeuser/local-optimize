import pytest

pytest.importorskip("numpy")
pytest.importorskip("psycopg2")
pytest.importorskip("transformers")

from project_context import ProjectContext, ReadScope, WriteVisibility
from rag_engine import RagEngine

pytestmark = pytest.mark.integration


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.last = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        text = str(query)
        values = tuple(params)
        self.connection.calls.append((text, values))
        self.last = text
        if "rag_cache_generation" not in text:
            # Scope assertions belong to the consolidation statements. The cache
            # bump is keyed by project on its own table and has no visibility
            # predicate to carry.
            assert "project_id = %s" in text
            assert "visibility" in text
            assert "project-a" in values
        self.rows = [(11,)] if "RETURNING k.id" in text else []

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return (7,) if "rag_cache_generation" in self.last else None


class Connection:
    def __init__(self):
        self.calls = []
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
        "primary",
        str(tmp_path),
        (str(tmp_path),),
        WriteVisibility.SESSION_PRIVATE,
        ReadScope.SESSION,
        "agent-a",
        "session-a",
    )


def test_consolidate_scopes_episode_entity_and_update_queries(tmp_path):
    connection = Connection()
    engine = object.__new__(RagEngine)
    engine._get_conn = lambda: connection
    engine._put_conn = lambda _connection, error=False: None

    result = engine.consolidate(context(tmp_path))

    assert result["status"] == "success"
    assert result["linked"] == 1
    # Two commits: consolidation's own, then the cache bump's. The bump has to
    # commit for itself - putconn() rolls a returned connection back, so an
    # uncommitted bump would be silently discarded.
    assert connection.commits == 2

    # Matching must stay a single set-based statement: no per-episode or per-entity
    # round trips, otherwise the O(episodes x entities) regression is back.
    matching = [call for call in connection.calls if "RETURNING k.id" in call[0]]
    assert len(matching) == 1
    # Plus the cache bump: consolidation changes what search returns, so it
    # retires the cached answers - on this connection, not a second one.
    assert len(connection.calls) == 2
    assert "rag_cache_generation" in connection.calls[1][0]
    sql, params = connection.calls[0]
    assert "CROSS JOIN LATERAL" in sql
    assert "position(lower(ent.name) in lower(ep.content))" in sql

    # All three scoped surfaces (episode read, entity read, update owner) carry the
    # caller scope; project_id is bound once per surface.
    assert params.count("project-a") == 3
    for alias in ("pk.visibility", "ent.visibility", "k.visibility"):
        assert alias in sql
    assert "k.project_id = %s" in sql
    assert sql.count("session-a") == 0  # scope values travel as parameters only
    assert params[-3:] == ("agent-a", "agent-a", "session-a")
