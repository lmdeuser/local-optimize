"""Agents receive accepted evidence, including on hits from older caches."""

import pytest
from fastapi.testclient import TestClient

from config import settings
from gpu_rag_server import app, get_rag_engine, SERVER_PROJECT_REGISTRY

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("scores", [[0.01, 0.02], [0.9, 0.01]])
def test_http_drops_below_floor_hits(monkeypatch, scores):
    class Engine:
        def search(self, **kwargs):
            return {
                "status": "success", "evidence_coverage": 1.0,
                "cache": {"layer": "l2-semantic", "matched_query": "original fixture"},
                "results": [{"content": f"fixture-{i}", "final_score": score} for i, score in enumerate(scores)],
            }

        def close(self):
            return None

    engine = Engine()
    monkeypatch.setattr(app.state, "engine_factory", lambda: engine, raising=False)
    app.dependency_overrides[get_rag_engine] = lambda: engine
    try:
        with TestClient(app) as client:
            response = client.post("/search", headers={"X-Local-RAG-Session": "relevance-fixture-session"}, json={
                "context": {
                    "project_id": settings.project_root.name, "root_id": "primary",
                    "read_scope": "shared", "write_visibility": "shared",
                    "registry_fingerprint": SERVER_PROJECT_REGISTRY.fingerprint,
                },
                "query": "fixture question", "limit": 5,
            })
        assert response.status_code == 200, response.text
        result = response.json()
        expected = sum(score >= settings.relevance_floor for score in scores)
        assert len(result["results"]) == expected
        assert result["route"] == ("rag_answer" if expected else "llm_needed")
        assert result["cache"] == {"layer": "l2-semantic", "matched_query": "original fixture"}
        assert result["evidence_coverage"] == round(expected / 15, 2)
        if not expected:
            assert result["evidence_coverage"] == 0
            assert result["tokens_used"] == 0
    finally:
        app.dependency_overrides.clear()
