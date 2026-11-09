import hashlib
import asyncio
import json
import pytest
from unittest.mock import MagicMock

pytestmark = pytest.mark.unit

from rag_client import RagClient
from project_context import ProjectDefinition, ProjectRegistry
from mcp_server import mcp
import mcp_server
from rag_engine import RagEngine


@pytest.fixture
def sample_context(tmp_path):
    primary_path = tmp_path / "project_root"
    primary_path.mkdir(parents=True, exist_ok=True)
    registry = ProjectRegistry([ProjectDefinition("proj-1", "Project 1", {"primary": primary_path})])
    return registry.resolve(
        project_id="proj-1",
        root_id="primary",
        write_visibility="session-private",
        read_scope="session",
        agent_id="test-agent",
        session_id="test-session",
        registry_fingerprint_hint=registry.fingerprint,
    )


def test_hashlib_sha256_usedforsecurity_support():
    h = hashlib.sha256(b"test data", usedforsecurity=False).hexdigest()
    assert len(h) == 64


def test_rag_client_consolidate_and_memory_optimize(sample_context, monkeypatch):
    client = RagClient(sample_context, server_url="http://fixture.invalid")
    captured = {}

    def fake_post(endpoint, payload, session_id=None):
        captured[endpoint] = payload
        return {"status": "success"}

    monkeypatch.setattr(client, "_post", fake_post)

    res_c = client.consolidate(decay_days=15.0, min_importance=2)
    assert res_c["status"] == "success"
    assert captured["/consolidate"]["decay_days"] == 15.0
    assert captured["/consolidate"]["min_importance"] == 2

    res_m = client.memory_optimize(sim_threshold=0.88)
    assert res_m["status"] == "success"
    assert captured["/memory_optimize"]["sim_threshold"] == 0.88


def test_mcp_gap_tools_registered():
    tools = [t.name for t in mcp._tool_manager.list_tools()]
    assert "rag_consolidate" in tools
    assert "rag_memory_optimize" in tools
    assert "rag_feedback" in tools


def test_rag_search_reports_unregistered_project_explicitly(monkeypatch):
    def unknown_project(**kwargs):
        raise ValueError("Unknown project_id")

    monkeypatch.setattr(mcp_server, "build_knowledge_context", unknown_project)
    result = asyncio.run(mcp_server.rag_search("query", project_id="missing-project"))

    assert json.loads(result)["code"] == "UNKNOWN_PROJECT"


def test_execute_and_learn_disabled_by_default(sample_context, monkeypatch):
    monkeypatch.delenv("ENABLE_EXECUTE_AND_LEARN", raising=False)
    engine = object.__new__(RagEngine)
    result = engine.execute_and_learn(sample_context, "echo test")
    assert result["status"] == "disabled"
    assert "disabled for safety" in result["message"]


def test_engine_consolidate_and_memory_optimize_signatures(sample_context):
    engine = object.__new__(RagEngine)
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = []
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    engine._get_conn = lambda: mock_conn
    engine._put_conn = lambda conn, error=False: None

    # Test consolidate alias and parameter accepting
    assert hasattr(engine, "consolidate_episodic_to_semantic")
    assert engine.consolidate_episodic_to_semantic == engine.consolidate

    res1 = engine.consolidate(sample_context, decay_days=15.0, min_importance=2)
    assert res1["status"] == "success"

    res2 = engine.consolidate_episodic_to_semantic(sample_context, decay_days=10.0, min_importance=3)
    assert res2["status"] == "success"

    res3 = engine.memory_optimize(sample_context, apply=False, sim_threshold=0.85)
    assert res3["status"] == "success"
    assert res3["sim_threshold"] == 0.85
