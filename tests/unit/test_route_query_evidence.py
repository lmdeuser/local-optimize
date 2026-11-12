import json

import pytest

import mcp_server

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def mock_client_context(monkeypatch):
    async def context(**_kwargs):
        return object()

    monkeypatch.setattr(mcp_server, "build_client_knowledge_context", context)


@pytest.mark.asyncio
async def test_route_query_uses_retrieved_evidence_not_caller_context(monkeypatch):
    captured = {}

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    class FakeClient:
        def __init__(self, _context):
            pass

        def search(self, *_args, **_kwargs):
            return {
                "status": "success",
                "top_score": 0.95,
                "evidence_coverage": 0.8,
                "results": [
                    {
                        "content": "retrieved production fact",
                        "chunk_id": 55,
                        "document_id": 7,
                        "metadata": {"relative_path": "docs/fact.md"},
                        "source_uri": "fixture://document",
                        "source_type": "document",
                        "start_locator": "section-1",
                        "validation_status": "unverified",
                        "content_hash": "fixture-hash",
                    },
                    {
                        "content": "generated summary without a primary reference",
                        "chunk_id": 56,
                        "source_uri": "agent_generated",
                        "source_type": "llm_output",
                        "validation_status": "validated",
                    }
                ],
            }

    def fake_route(query, evidence, score, **kwargs):
        captured.update(query=query, evidence=evidence, score=score, **kwargs)
        return {
            "decision": "respond",
            "response": "grounded [Evidence 1]",
            "response_kind": "local_model",
            "cited_evidence_ids": [1],
            "escalation_reason": None,
        }

    monkeypatch.setattr(mcp_server, "RagClient", FakeClient)
    monkeypatch.setattr(mcp_server, "route_query", fake_route)
    monkeypatch.setattr(mcp_server.asyncio, "to_thread", inline_to_thread)

    raw = await mcp_server.route_query_tool(
        "fixture question", context="caller supplied text must stay separate"
    )
    result = json.loads(raw)

    assert "retrieved production fact" in captured["evidence"]
    assert "fixture://document" in captured["evidence"]
    assert "chunk_id=55" in captured["evidence"]
    assert "validation_status=unverified" in captured["evidence"]
    assert "caller supplied text" not in captured["evidence"]
    assert captured["score"] == 0.95
    assert result["retrieval_coverage"] == 0.8
    assert result["evidence_coverage"] == 0.2
    assert result["evidence_sources"][0]["source_uri"] == "fixture://document"
    assert result["response_sources"][0]["chunk_id"] == 55
    assert result["response_sources"][0]["document_id"] == 7
    assert result["response_sources"][0]["validation_status"] == "unverified"
    assert result["response_sources"][0]["source_is_primary"] is True
    generated = next(source for source in result["evidence_sources"] if source["chunk_id"] == 56)
    assert generated["validation_status"] == "validated"
    assert generated["source_is_primary"] is False
    assert result["source_warnings"][0]["code"] == "SOURCE_FRESHNESS_UNCONFIRMED"
    assert result["contradiction_check"] == "not_independently_verified"
    assert result["missing_evidence"] == []
    assert result["supplemental_context_supplied"] is True


@pytest.mark.asyncio
async def test_policy_fallback_does_not_report_search_fill_as_answer_coverage(monkeypatch):
    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    class FakeClient:
        def __init__(self, _context):
            pass

        def search(self, *_args, **_kwargs):
            return {
                "status": "success",
                "top_score": 0.95,
                "evidence_coverage": 1.0,
                "results": [{
                    "content": "retrieved production fact",
                    "chunk_id": 55,
                    "source_uri": "fixture://document",
                    "source_type": "document",
                    "validation_status": "unverified",
                }],
            }

    def fake_route(*_args, **_kwargs):
        return {
            "decision": "respond",
            "response": "НЕДОСТАТОЧНО ДАННЫХ",
            "response_kind": "policy_fallback",
            "cited_evidence_ids": [],
        }

    monkeypatch.setattr(mcp_server, "RagClient", FakeClient)
    monkeypatch.setattr(mcp_server, "route_query", fake_route)
    monkeypatch.setattr(mcp_server.asyncio, "to_thread", inline_to_thread)

    result = json.loads(await mcp_server.route_query_tool("fixture question"))

    assert result["route"] == "fallback"
    assert result["response_kind"] == "policy_fallback"
    assert result["answer_diagnostics"] is None
    assert result["confidence"] == 0.0
    assert result["confidence_components"]["server_score"] == 0.95
    assert result["retrieval_coverage"] == 1.0
    assert result["evidence_coverage"] == 0.0
    assert result["confidence_components"]["answer_evidence_coverage"] == 0.0
    assert result["response_sources"] == []
    assert result["missing_evidence"]


@pytest.mark.asyncio
async def test_unsupported_route_policy_fails_closed():
    result = json.loads(
        await mcp_server.route_query_tool("fixture question", policy="caller_decides")
    )
    assert result["route"] == "escalate"
    assert result["reason"] == "Unsupported routing policy"


@pytest.mark.asyncio
async def test_route_query_builds_knowledge_context_without_root_id(monkeypatch, tmp_path):
    from project_context import KnowledgeContext, ProjectDefinition, ProjectRegistry
    import project_context as pc

    registry = ProjectRegistry([ProjectDefinition("proj-a", "Project A", {"root-a": tmp_path})])
    monkeypatch.undo()
    monkeypatch.setattr(pc, "get_project_registry", lambda *_a, **_kw: registry)
    monkeypatch.setattr(mcp_server, "get_project_registry", lambda *_a, **_kw: registry)

    captured_contexts = []

    class FakeClient:
        def __init__(self, ctx):
            captured_contexts.append(ctx)

        def search(self, *_args, **_kwargs):
            return {"status": "success", "top_score": 0.9, "evidence_coverage": 0.5, "results": []}

        def lookup_local_answer(self, *_args, **_kwargs):
            return {"hit": False, "generation": 0}

        def store_local_answer(self, *_args, **_kwargs):
            return {"stored": False}

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(mcp_server, "RagClient", FakeClient)
    monkeypatch.setattr(mcp_server.asyncio, "to_thread", inline_to_thread)
    monkeypatch.setattr(
        mcp_server,
        "route_query",
        lambda *_a, **_kw: {"decision": "escalate", "response_kind": "escalation", "cited_evidence_ids": []},
    )

    result = json.loads(
        await mcp_server.route_query_tool("fixture question", project_id="proj-a")
    )
    assert result["status"] == "success"
    assert isinstance(captured_contexts[0], KnowledgeContext)
