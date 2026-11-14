import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from config import SecretString, settings
from gpu_rag_server import app

pytestmark = pytest.mark.integration


class ReadyEngine:
    def check_readiness(self):
        return True

    def close(self):
        return None


def test_liveness_is_process_only_and_exact(monkeypatch):
    monkeypatch.setattr(
        app.state,
        "engine_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("fixture")),
        raising=False,
    )
    with TestClient(app) as client:
        response = client.get("/liveness")
        assert response.status_code == 200
        assert response.json() == {"status": "success"}


def test_readiness_reports_safe_503_when_engine_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        app.state,
        "engine_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("sensitive fixture")),
        raising=False,
    )
    with TestClient(app) as client:
        response = client.get("/readiness")
        assert response.status_code == 503
        assert response.json() == {
            "status": "unavailable",
            "ready": False,
            "reason": "RAG engine initialization is incomplete",
        }
        assert "sensitive fixture" not in response.text


def test_readiness_is_200_only_after_database_schema_check(monkeypatch):
    monkeypatch.setattr(app.state, "engine_factory", ReadyEngine, raising=False)
    with TestClient(app) as client:
        response = client.get("/readiness")
        assert response.status_code == 200
        assert response.json() == {"status": "success", "ready": True}


def test_configured_bearer_token_is_required_without_leaking_it(monkeypatch):
    monkeypatch.setattr(
        app.state,
        "engine_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("fixture")),
        raising=False,
    )
    monkeypatch.setattr(settings, "_bearer_token", SecretString("fixture-secret-token"))
    with TestClient(app) as client:
        denied = client.get("/liveness")
        allowed = client.get(
            "/liveness", headers={"Authorization": "Bearer fixture-secret-token"}
        )
    assert denied.status_code == 401
    assert "fixture-secret-token" not in denied.text
    assert allowed.status_code == 200


def test_runtime_failure_returns_safe_unavailable(monkeypatch):
    class BrokenRuntime(ReadyEngine):
        _pool = object()

        def runtime_status(self):
            raise RuntimeError("sensitive internal fixture")

    monkeypatch.setattr(app.state, "engine_factory", BrokenRuntime, raising=False)
    with TestClient(app) as client:
        response = client.get("/runtime")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert "sensitive" not in response.text
