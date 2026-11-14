import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

import gpu_rag_server
from gpu_rag_server import app, get_rag_engine
from project_context import ProjectDefinition, ProjectRegistry


pytestmark = pytest.mark.integration


class SearchEngine:
    def __init__(self):
        self.calls = []
        self._pool = object()

    def list_readable_projects(self, _context):
        return [("first", "First"), ("second", "Second"), ("memory", "Memory"), ("unregistered", "Unregistered")]

    def search(self, context, query, limit=5):
        self.calls.append((context.project_id, query, limit))
        return {
            "status": "success",
            "results": [{
                "chunk_id": len(self.calls),
                "content": f"evidence for {context.project_id}",
                "final_score": 0.9,
                "source_uri": "fixture://source",
                "metadata": {},
            }],
            "evidence_coverage": 1.0,
        }

    def close(self):
        return None


@pytest.fixture
def registry(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    value = ProjectRegistry([
        ProjectDefinition("first", "First", {"primary": first}),
        ProjectDefinition("second", "Second", {"primary": second}),
        ProjectDefinition("memory", "Memory", {}),
    ])
    monkeypatch.setattr(gpu_rag_server, "get_project_registry", lambda _settings: value)
    return value


def run_search(registry, engine, context, monkeypatch):
    monkeypatch.setattr(app.state, "engine_factory", lambda: engine, raising=False)
    app.dependency_overrides[get_rag_engine] = lambda: engine
    try:
        with TestClient(app) as client:
            return client.post(
                "/search",
                json={"context": context, "query": "fixture", "limit": 3},
                headers={"X-Local-RAG-Session": "test-session-1234567890"},
            )
    finally:
        app.dependency_overrides.clear()


def test_http_resolves_client_working_directory_without_project(registry, tmp_path, monkeypatch):
    engine = SearchEngine()
    response = run_search(registry, engine, {
        "working_directory": str(tmp_path / "second"),
        "read_scope": "shared",
    }, monkeypatch)
    assert response.status_code == 200, response.text
    assert [call[0] for call in engine.calls] == ["second"]
    assert response.json()["results"][0]["project_id"] == "second"


def test_http_accepts_unregistered_workspace(registry, tmp_path, monkeypatch):
    workspace = tmp_path / "unregistered"
    workspace.mkdir()
    engine = SearchEngine()
    response = run_search(
        registry,
        engine,
        {"working_directory": str(workspace), "read_scope": "shared"},
        monkeypatch,
    )
    assert response.status_code == 200, response.text
    assert engine.calls[0][0].startswith("ws:")


def test_http_global_save_does_not_authorize_file_indexing(registry, monkeypatch):
    class Engine(SearchEngine):
        def save(self, context, **_kwargs):
            self.calls.append(context.project_id)
            return {"status": "success"}

    engine = Engine()
    monkeypatch.setattr(app.state, "engine_factory", lambda: engine, raising=False)
    app.dependency_overrides[get_rag_engine] = lambda: engine
    headers = {"X-Local-RAG-Session": "test-session-1234567890"}
    try:
        with TestClient(app) as client:
            saved = client.post("/save", json={"context": {}, "content": "A rootless knowledge fixture"}, headers=headers)
            indexed = client.post("/index_code", json={"context": {}, "path": "/etc/passwd"}, headers=headers)
    finally:
        app.dependency_overrides.clear()
    assert saved.status_code == 200
    assert engine.calls == ["__global_knowledge__"]
    assert indexed.status_code == 422


def test_http_global_search_is_explicit_bounded_and_has_origins(registry, monkeypatch):
    engine = SearchEngine()
    response = run_search(registry, engine, {
        "global_search": True,
        "read_scope": "shared",
    }, monkeypatch)
    assert response.status_code == 200, response.text
    body = response.json()
    assert {call[0] for call in engine.calls} == {"first", "second", "memory", "unregistered"}
    assert {hit["project_id"] for hit in body["results"]} <= {"first", "second", "memory", "unregistered"}
    assert body["partial"] is False
    assert len(body["results"]) <= 3


def test_http_plain_search_defaults_to_global(registry, monkeypatch):
    engine = SearchEngine()
    response = run_search(registry, engine, {"read_scope": "shared"}, monkeypatch)
    assert response.status_code == 200
    assert {call[0] for call in engine.calls} == {"first", "second", "memory", "unregistered"}


def test_http_global_search_marks_failed_project_partial(registry, monkeypatch):
    class PartialEngine(SearchEngine):
        def search(self, context, query, limit=5):
            if context.project_id == "second":
                return {"status": "error", "results": []}
            return super().search(context, query, limit)

    response = run_search(registry, PartialEngine(), {
        "global_search": True,
        "read_scope": "shared",
    }, monkeypatch)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["partial"] is True
    assert body["route"] == "llm_needed"
    assert body["omitted_project_ids"] == ["second"]


def test_http_global_search_has_at_most_two_active_projects(registry, monkeypatch):
    import threading
    import time

    class BoundedEngine(SearchEngine):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def search(self, context, query, limit=5):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.03)
                return super().search(context, query, limit)
            finally:
                with self.lock:
                    self.active -= 1

    engine = BoundedEngine()
    response = run_search(registry, engine, {
        "global_search": True,
        "read_scope": "shared",
    }, monkeypatch)
    assert response.status_code == 200, response.text
    assert engine.max_active <= 2


def test_http_global_agentic_search_has_origins_and_global_limits(registry, monkeypatch):
    class AgenticEngine:
        _pool = object()

        def list_readable_projects(self, _context):
            return [("first", "First"), ("second", "Second"), ("unregistered", "Unregistered")]

        def agentic_search(self, context, **_kwargs):
            return {
                "status": "success",
                "partial": False,
                "results": [
                    {"chunk_id": index, "content": str(index), "score": 0.9, "metadata": {}}
                    for index in range(4)
                ],
                "graph_context": [
                    {"entity_id": index, "content": str(index), "relation_depth": 1}
                    for index in range(5)
                ],
                "traversed_relations": 3,
            }

        def close(self):
            return None

    monkeypatch.setattr(app.state, "engine_factory", AgenticEngine, raising=False)
    app.dependency_overrides[get_rag_engine] = AgenticEngine
    try:
        with TestClient(app) as client:
            response = client.post(
                "/agentic-search",
                json={
                    "context": {"global_search": True, "read_scope": "shared"},
                    "query": "fixture",
                    "limit": 2,
                    "max_entities": 2,
                    "max_relations": 3,
                },
                headers={"X-Local-RAG-Session": "test-session-1234567890"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["results"]) <= 2
    assert len(body["graph_context"]) <= 2
    assert body["traversed_relations"] <= 3
    assert {hit["project_id"] for hit in body["results"]}
    assert {node["project_id"] for node in body["graph_context"]}


def test_http_cwd_rejects_traversal_and_symlink_escape(registry, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "second" / "escape"
    link.symlink_to(outside, target_is_directory=True)
    for candidate in (str(tmp_path / "second" / ".." / "first"), str(link)):
        engine = SearchEngine()
        response = run_search(registry, engine, {
            "working_directory": candidate,
            "read_scope": "shared",
        }, monkeypatch)
        assert response.status_code == 403
        assert engine.calls == []


def test_http_search_accepts_filesystem_project_context(registry, tmp_path, monkeypatch):
    context = registry.resolve(
        project_id="first",
        root_id="primary",
        write_visibility="shared",
        read_scope="shared",
        agent_id="agent-a",
        session_id="session-a",
    ).to_request_dict()
    engine = SearchEngine()
    response = run_search(registry, engine, context, monkeypatch)
    assert response.status_code == 200, response.text
    assert engine.calls[0][0] == "first"
