import threading

import pytest

pytest.importorskip("numpy")
pytest.importorskip("psycopg2")
pytest.importorskip("transformers")

from project_context import ProjectContext, ReadScope, WriteVisibility
from rag_engine import RagEngine

pytestmark = pytest.mark.integration


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
        if "FROM public.kb_entities e" in text and "SELECT e.id, e.name" in text:
            assert "e.project_id = %s" in text
            assert "e.id = ANY(%s)" in text
            # Seed lookup must be gated by the entity evidence ACL, never by name scan.
            assert "FROM public.entity_evidence ev" in text
            assert "ev.is_active = true" in text
            assert "ev.visibility" in text
            assert params[0] == "project-a"
            assert params[1] == [1]
            self.rows = [(1, "SameName", "class", "seed", "project-a:a.py:class:SameName")]
        elif "FROM public.kb_relations" in text:
            assert text.count("r.project_id = %s") == 2
            assert "r.visibility" in text
            assert text.count("e1.project_id = %s") == 2
            assert text.count("e2.project_id = %s") == 2
            assert "e1.visibility" in text
            assert "e2.visibility" in text
            assert params.count("project-a") == 6
            self.rows = [
                (2, "SameName", "function", "neighbor", "project-a:b.py:function:SameName", "defines", 1.0, 1.0)
            ]
        else:
            self.rows = []

    def fetchall(self):
        return self.rows


class GraphConnection:
    def __init__(self):
        self.calls = []

    def cursor(self):
        return GraphCursor(self)


def test_graphrag_traverses_entity_ids_with_full_security_scope(tmp_path):
    context = ProjectContext(
        "project-a",
        "Project A",
        "root-a",
        str(tmp_path),
        (str(tmp_path),),
        WriteVisibility.SESSION_PRIVATE,
        ReadScope.SESSION,
        "agent-a",
        "session-a",
    )
    connection = GraphConnection()
    engine = object.__new__(RagEngine)
    engine._get_conn = lambda: connection
    engine._put_conn = lambda _connection, error=False: None
    engine.search = lambda *_args, **_kwargs: {
        "status": "success",
        "results": [
            {
                "content": "SameName fixture",
                "metadata": {"entity_id": 1},
                "score": 1.0,
            }
        ],
    }
    engine.ram_pages = {}
    engine._ram_lock = threading.RLock()
    engine.ram_allocate = lambda *_args, **_kwargs: None
    engine._sanitize_context = lambda value: value
    engine.minimize_context_tokens = lambda hits, graph, **_kwargs: (hits, graph)

    result = engine.agentic_search(
        context,
        "SameName",
        limit=2,
        max_depth=2,
        max_entities=5,
        max_relations=5,
    )

    assert result["status"] == "success"
    assert [item["entity_id"] for item in result["graph_context"]] == [1, 2]
    assert result["security_scope"] == {
        "project_id": "project-a",
        "read_scope": "session",
    }
