from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("numpy")
pytest.importorskip("psycopg2")
pytest.importorskip("transformers")
from fastapi.testclient import TestClient

from config import settings
from gpu_rag_server import app, get_rag_engine
from project_context import ProjectContext, get_project_registry
from rag_client import RagClient
from rag_engine import RagEngine

pytestmark = pytest.mark.integration

SERVER_PROJECT_REGISTRY = get_project_registry(settings)

class MockEngine:
    def __init__(self):
        self.save_called_with = None
        self.search_called_with = None
        self.agent_called_with = None
        self.saved_data = {}

    def check_readiness(self):
        return True

    def close(self):
        pass

    def save(self, context, content, metadata=None, source_uri=None, source_type=None,
             file_path=None, document_id=None, start_locator=None, end_locator=None, importance=5, pinned=False):
        self.save_called_with = context.session_id
        self.agent_called_with = context.agent_id
        self.saved_data[context.session_id] = content
        return {"status": "success"}

    def search(self, context, query, limit=5):
        self.search_called_with = context.session_id
        if context.session_id in self.saved_data:
            return {"status": "success", "results": [{"content": self.saved_data[context.session_id], "score": 0.99}], "evidence_coverage": 1.0}
        return {"status": "success", "results": [], "evidence_coverage": 0.0}

@pytest.fixture
def engine():
    return MockEngine()

@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setattr(app.state, "engine_factory", lambda: engine, raising=False)
    app.dependency_overrides[get_rag_engine] = lambda: engine
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()

def test_missing_session_header(client):
    r = client.post("/save", json={
        "context": {
            "project_id": "local_optimize",
            "root_id": "primary",
            "write_visibility": "session-private",
            "read_scope": "session",
            "registry_fingerprint": SERVER_PROJECT_REGISTRY.fingerprint
        },
        "content": "test"
    })
    assert r.status_code == 400
    assert "MISSING_SESSION" in r.text

@pytest.mark.parametrize("bad_session", [
    "short",  # too short
    "a" * 129,  # too long
    "session with spaces1",  # space
    "session\r\n1234567",  # CR LF
    "session!@#$%",  # forbidden chars
    "session\x00test123",  # unicode control char
])
def test_malformed_session_headers(client, bad_session):
    r = client.post("/save", headers={"X-Local-RAG-Session": bad_session}, json={
        "context": {
            "project_id": "local_optimize",
            "root_id": "primary",
            "write_visibility": "session-private",
            "read_scope": "session",
            "registry_fingerprint": SERVER_PROJECT_REGISTRY.fingerprint
        },
        "content": "test"
    })
    assert r.status_code == 400
    assert "MALFORMED_SESSION" in r.text
    assert bad_session not in r.text

def test_liveness_readiness_options_no_header(client):
    r = client.get("/liveness")
    assert r.status_code == 200

    r = client.get("/readiness")
    assert r.status_code in [200, 503]

    r = client.options("/save", headers={
        "Origin": "http://localhost:8181",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-local-rag-session"
    })
    assert r.status_code == 200, r.text
    assert "x-local-rag-session" in r.headers.get("access-control-allow-headers", "").lower()

def test_session_isolation(client, engine):
    session_a = "session-a-1234567"
    session_b = "session-b-1234567"
    
    payload_a = {
        "context": {
            "project_id": "local_optimize",
            "root_id": "primary",
            "write_visibility": "session-private",
            "read_scope": "session",
            "registry_fingerprint": SERVER_PROJECT_REGISTRY.fingerprint,
            "agent_id": settings.server_agent_id
        },
        "content": "This is a long enough content A",
        "metadata": {"importance": 5}
    }

    r = client.post("/save", headers={"X-Local-RAG-Session": session_a}, json=payload_a)
    assert r.status_code == 200, r.text
    assert engine.save_called_with == session_a

    r = client.post("/search", headers={"X-Local-RAG-Session": session_a}, json={
        "context": payload_a["context"],
        "query": "find private",
        "limit": 5
    })
    assert r.status_code == 200, r.text
    assert len(r.json()["results"]) > 0
    assert engine.search_called_with == session_a

    assert engine.save_called_with == engine.search_called_with

    r = client.post("/search", headers={"X-Local-RAG-Session": session_b}, json={
        "context": payload_a["context"],
        "query": "find private",
        "limit": 5
    })
    assert r.status_code == 200, r.text
    assert len(r.json()["results"]) == 0
    assert engine.search_called_with == session_b

def test_json_identity_override_fails(client, engine):
    session_a = "session-a-1234567"
    payload = {
        "context": {
            "project_id": "local_optimize",
            "root_id": "primary",
            "write_visibility": "session-private",
            "read_scope": "session",
            "registry_fingerprint": SERVER_PROJECT_REGISTRY.fingerprint,
            "session_id": "hacked-session-id",
            "agent_id": "hacked-agent-id"
        },
        "content": "This is a long enough content for overriding"
    }
    r = client.post("/save", headers={"X-Local-RAG-Session": session_a}, json=payload)
    assert r.status_code == 200, r.text
    assert engine.save_called_with == session_a
    assert engine.agent_called_with == settings.server_agent_id
    assert engine.save_called_with != "hacked-session-id"
    assert engine.agent_called_with != "hacked-agent-id"

@patch('rag_engine.pgpool.ThreadedConnectionPool')
@patch('rag_engine.RagEngine._ensure_schema')
@patch('rag_engine.AutoTokenizer')
@patch('rag_engine.XpuReranker')
@patch('rag_engine.RagEngine._require_local_path')
def test_warm_up_sentence_transformer(mock_require, mock_reranker, mock_tokenizer, mock_ensure, mock_pool):
    mock_require.side_effect = lambda path, name: MagicMock(is_dir=MagicMock(return_value=True))
    
    mock_st_class = MagicMock()
    mock_model_instance = MagicMock()
    mock_model_instance.device.type = "xpu"
    mock_tensor = MagicMock()
    mock_tensor.device.type = "xpu"
    mock_model_instance.parameters.return_value = [mock_tensor]
    mock_model_instance.encode.return_value = [0.1] * 384
    mock_st_class.return_value = mock_model_instance
    
    with patch('rag_engine.EvidenceRanker') as evidence, patch.dict('sys.modules', {'sentence_transformers': MagicMock(SentenceTransformer=mock_st_class), 'torch': MagicMock()}):
        # Let RagEngine.encode execute normally, it will delegate to mock_model_instance.encode
        engine = RagEngine()
        evidence.return_value.warm_up.assert_called_once()
        
        assert mock_st_class.call_args.kwargs["device"] == "xpu"
        assert engine._st_model is mock_model_instance, "Model object not created via mock"
        assert engine._model_warmed_up is True, "_model_warmed_up did not become True"
        assert mock_model_instance.encode.call_count == 1, "encode should be called exactly once during warm-up"
        
        engine._warm_up_model()
        assert mock_model_instance.encode.call_count == 1, "Repeated warm-up should not call encode again"
        
        engine.check_readiness()
        assert mock_model_instance.encode.call_count == 1, "readiness should not trigger warm-up again"

@patch('rag_engine.pgpool.ThreadedConnectionPool')
@patch('rag_engine.RagEngine._ensure_schema')
@patch('rag_engine.AutoTokenizer')
@patch('rag_engine.XpuReranker')
@patch('rag_engine.RagEngine._require_local_path')
def test_file_model_rejected(mock_require, mock_reranker, mock_tokenizer, mock_ensure, mock_pool):
    mock_require.side_effect = lambda path, name: MagicMock(is_dir=MagicMock(return_value=(name == "TOKENIZER_PATH")))
    with pytest.raises(RuntimeError, match="directory"):
        RagEngine()
    mock_pool.return_value.closeall.assert_called_once()

@patch('rag_engine.pgpool.ThreadedConnectionPool')
@patch('rag_engine.RagEngine._ensure_schema')
@patch('rag_engine.AutoTokenizer')
@patch('rag_engine.XpuReranker')
@patch('rag_engine.RagEngine._require_local_path')
def test_warm_up_failure_halts_initialization(mock_require, mock_reranker, mock_tokenizer, mock_ensure, mock_pool):
    mock_require.side_effect = lambda path, name: MagicMock(is_dir=MagicMock(return_value=True))
    
    mock_st_class = MagicMock()
    mock_model_instance = MagicMock()
    mock_model_instance.device.type = "xpu"
    mock_tensor = MagicMock()
    mock_tensor.device.type = "xpu"
    mock_model_instance.parameters.return_value = [mock_tensor]
    # Bad dimensionality: 10 instead of 384
    mock_model_instance.encode.return_value = [0.1] * 10
    mock_st_class.return_value = mock_model_instance
    
    with patch.dict('sys.modules', {'sentence_transformers': MagicMock(SentenceTransformer=mock_st_class), 'torch': MagicMock()}):
        with pytest.raises(RuntimeError, match="Engine initialization failure during warm-up"):
            RagEngine()

def test_rag_client_passes_header_from_context():
    context_a = MagicMock(spec=ProjectContext)
    context_a.session_id = "sessionA-1234567"
    context_a.to_request_dict.return_value = {}
    context_a.__class__ = ProjectContext
    
    context_b = MagicMock(spec=ProjectContext)
    context_b.session_id = "sessionB-1234567"
    context_b.to_request_dict.return_value = {}
    context_b.__class__ = ProjectContext
    client = RagClient(context=context_a, server_url="http://fake")

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"status": "success"}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        client.save(content="test content long enough", context=context_a)
        req = mock_urlopen.call_args[0][0]
        assert req.headers.get("X-local-rag-session") == "sessionA-1234567"

        client.save(content="test content long enough", context=context_b)
        req = mock_urlopen.call_args[0][0]
        assert req.headers.get("X-local-rag-session") == "sessionB-1234567"



def test_search_is_blocked_when_readiness_fails(client, engine, monkeypatch):
    monkeypatch.setattr(
        engine,
        "require_readiness",
        lambda: (_ for _ in ()).throw(RuntimeError("RETRIEVAL_NOT_READY")),
        raising=False,
    )
    payload = {
        "context": {
            "project_id": "local_optimize",
            "root_id": "primary",
            "write_visibility": "session-private",
            "read_scope": "session",
            "registry_fingerprint": SERVER_PROJECT_REGISTRY.fingerprint,
            "agent_id": settings.server_agent_id,
        },
        "query": "must not run",
        "limit": 1,
    }

    response = client.post("/search", headers={"X-Local-RAG-Session": "session-test-123456"}, json=payload)

    assert response.status_code == 503
    assert engine.search_called_with is None


def test_partial_index_uses_canonical_status_and_mode(client, engine, monkeypatch):
    monkeypatch.setattr(
        engine,
        "index_code",
        lambda *_args, **_kwargs: {"status": "partial", "partial": True},
        raising=False,
    )
    payload = {
        "context": {
            "project_id": "local_optimize",
            "root_id": "primary",
            "write_visibility": "session-private",
            "read_scope": "session",
            "registry_fingerprint": SERVER_PROJECT_REGISTRY.fingerprint,
            "agent_id": settings.server_agent_id,
        },
        "path": "tests/integration/test_session_contract.py",
    }

    response = client.post("/index_code", headers={"X-Local-RAG-Session": "session-test-123456"}, json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["index_mode"] == "partial"
    assert response.json()["completed"] is False
    assert RagClient.__new__(RagClient)._validate_response("/index_code", response.content) == response.json()
