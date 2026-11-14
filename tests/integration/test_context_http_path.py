import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

TEST_SESSION_HEADERS = {"X-Local-RAG-Session": "test-session-1234567890"}

import gpu_rag_server
from config import settings
from gpu_rag_server import app, get_rag_engine
from project_context import ProjectDefinition, ProjectRegistry

pytestmark = pytest.mark.integration


@pytest.fixture
def registry(tmp_path, monkeypatch):
    project_root = tmp_path / "fixture-project"
    project_root.mkdir()
    value = ProjectRegistry([
        ProjectDefinition("fixture-project", "Fixture Project", {"primary": project_root}),
    ])
    monkeypatch.setattr(gpu_rag_server, "get_project_registry", lambda _settings: value)
    return value


def context_payload(registry, **overrides):
    payload = {
        "project_id": "fixture-project",
        "root_id": "primary",
        "write_visibility": "session-private",
        "read_scope": "shared",
        "agent_id": "agent-a",
        "session_id": "session-a",
        "source_types": ["code"],
        "language": "en",
        "validation_status": "validated",
        "min_score": 0.2,
        "include_metadata": True,
        "registry_fingerprint": registry.fingerprint,
    }
    payload.update(overrides)
    return payload


def knowledge_context_payload(registry, **overrides):
    payload = {
        "project_id": "fixture-project",
        "read_scope": "shared",
        "agent_id": "agent-a",
        "session_id": "session-a",
        "source_types": ["code"],
        "language": "en",
        "min_score": 0.2,
        "include_metadata": True,
        "registry_fingerprint": registry.fingerprint,
    }
    payload.update(overrides)
    return payload


class CapturingEngine:
    def __init__(self):
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "success", "evidence_coverage": 0.6, "results": []}

    def save(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "success", "chunk_id": 1}


def test_http_agentic_search_forwards_engine_hits_to_results(registry, monkeypatch):
    """The HTTP layer must read the engine's own "results" key.

    It previously consumed a non-existent "vector_hits" key, so every hit was
    dropped and "results" was overwritten with an empty list: callers saw graph
    context with no source chunks.
    """

    class AgenticEngine:
        def agentic_search(self, **kwargs):
            return {
                "status": "success",
                "results": [
                    {"content": "first retrieved chunk", "score": 0.9, "metadata": {}},
                    {"content": "second retrieved chunk", "score": 0.8, "metadata": {}},
                ],
                "graph_context": [{"entity_id": 1, "entity": "Thing", "type": "class"}],
            }

    engine = AgenticEngine()
    monkeypatch.setattr(
        app.state,
        "engine_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("fixture")),
        raising=False,
    )
    app.dependency_overrides[get_rag_engine] = lambda: engine
    try:
        with TestClient(app) as client:
            response = client.post(
                "/agentic-search",
                json={"context": knowledge_context_payload(registry), "query": "fixture", "limit": 2},
                headers=TEST_SESSION_HEADERS,
            )
        assert response.status_code == 200
        body = response.json()
        assert len(body["results"]) == 2
        assert body["results"][0]["content"] == "first retrieved chunk"
        assert len(body["graph_context"]) == 1
        assert body["tokens_used"] > 0
        # Every returned chunk carries a delta hash so clients can dedupe.
        assert all(hit["content_hash"] for hit in body["results"])
    finally:
        app.dependency_overrides.clear()


def test_http_search_passes_server_authorized_context_to_engine(registry, monkeypatch):
    engine = CapturingEngine()
    monkeypatch.setattr(
        app.state,
        "engine_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("fixture")),
        raising=False,
    )
    app.dependency_overrides[get_rag_engine] = lambda: engine
    try:
        with TestClient(app) as client:
            response = client.post(
                "/search",
                json={"context": knowledge_context_payload(registry), "query": "fixture", "limit": 2},
                headers=TEST_SESSION_HEADERS,
            )
        assert response.status_code == 200
        assert response.json()["evidence_coverage"] == 0.0  # No returned evidence.
        context = engine.calls[0]["context"]
        assert context.project_id == "fixture-project"
        assert not hasattr(context, "project_root")
        assert context.agent_id == settings.server_agent_id
        assert context.session_id == TEST_SESSION_HEADERS["X-Local-RAG-Session"]
    finally:
        app.dependency_overrides.clear()


def test_http_backend_error_status_is_503(registry, monkeypatch):
    class ErrorEngine:
        _pool = object()

        def search(self, **_kwargs):
            return {"status": "error", "results": []}

    monkeypatch.setattr(
        app.state,
        "engine_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("fixture")),
        raising=False,
    )
    app.dependency_overrides[get_rag_engine] = ErrorEngine
    try:
        with TestClient(app) as client:
            response = client.post(
                "/search",
                json={"context": knowledge_context_payload(registry), "query": "fixture", "limit": 2},
                headers=TEST_SESSION_HEADERS,
            )
        assert response.status_code == 503
        assert response.json()["detail"]["error"]["code"] == "SEARCH_FAILED"
    finally:
        app.dependency_overrides.clear()


def test_http_agentic_response_uses_canonical_results(registry, monkeypatch):
    class AgenticEngine:
        def agentic_search(self, **_kwargs):
            return {"status": "success", "vector_hits": [], "entities": [], "relations": []}

    monkeypatch.setattr(
        app.state,
        "engine_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("fixture")),
        raising=False,
    )
    app.dependency_overrides[get_rag_engine] = AgenticEngine
    try:
        with TestClient(app) as client:
            response = client.post(
                "/agentic-search",
                json={"context": knowledge_context_payload(registry), "query": "fixture", "limit": 2},
                headers=TEST_SESSION_HEADERS,
            )
        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert response.json()["results"] == []
    finally:
        app.dependency_overrides.clear()


def test_http_requires_registry_fingerprint(registry, monkeypatch):
    engine = CapturingEngine()
    payload = knowledge_context_payload(registry)
    payload.pop("registry_fingerprint")
    monkeypatch.setattr(
        app.state,
        "engine_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("fixture")),
        raising=False,
    )
    app.dependency_overrides[get_rag_engine] = lambda: engine
    try:
        with TestClient(app) as client:
            response = client.post(
                "/search", json={"context": payload, "query": "fixture", "limit": 2}, headers=TEST_SESSION_HEADERS
            )
        assert response.status_code == 422
        assert engine.calls == []
    finally:
        app.dependency_overrides.clear()


def test_http_rejects_visibility_validation_confusion_before_engine(registry, monkeypatch):
    engine = CapturingEngine()
    monkeypatch.setattr(
        app.state,
        "engine_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("fixture")),
        raising=False,
    )
    app.dependency_overrides[get_rag_engine] = lambda: engine
    try:
        with TestClient(app) as client:
            response = client.post(
                "/save", json={
                    "context": context_payload(registry, write_visibility="authoritative"),
                    "content": "long enough fixture content",
                }, headers=TEST_SESSION_HEADERS
            )
        assert response.status_code == 422
        assert engine.calls == []
    finally:
        app.dependency_overrides.clear()


def test_http_rejects_client_controlled_root_before_engine(registry, monkeypatch):
    engine = CapturingEngine()
    monkeypatch.setattr(
        app.state,
        "engine_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("fixture")),
        raising=False,
    )
    app.dependency_overrides[get_rag_engine] = lambda: engine
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/index_code", json={
                    "context": context_payload(registry, project_root="/", allowed_roots=["/"]),
                    "path": "fixture.py",
                }, headers=TEST_SESSION_HEADERS
            )
        assert response.status_code == 403
        assert engine.calls == []
    finally:
        app.dependency_overrides.clear()
