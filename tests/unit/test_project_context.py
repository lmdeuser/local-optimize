
import pytest

from project_context import (
    KnowledgeContext,
    ProjectContext,
    ProjectDefinition,
    ProjectRegistry,
    ReadScope,
    ValidationStatus,
    WriteVisibility,
)
from rag_client import RagClient

pytestmark = pytest.mark.unit


def make_context(tmp_path, **overrides):
    values = {
        "project_id": "project-a",
        "project_name": "Project A",
        "root_id": "root-a",
        "project_root": str(tmp_path),
        "allowed_roots": (str(tmp_path),),
        "write_visibility": WriteVisibility.SESSION_PRIVATE,
        "read_scope": ReadScope.SESSION,
        "agent_id": "agent-a",
        "session_id": "session-a",
        "validation_status": ValidationStatus.VALIDATED,
    }
    values.update(overrides)
    return ProjectContext(**values)


def test_invalid_security_scope_and_validation_status_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        make_context(tmp_path, write_visibility="authoritative")
    with pytest.raises(ValueError):
        make_context(tmp_path, read_scope="authoritative")
    with pytest.raises(ValueError):
        make_context(tmp_path, validation_status="shared")


def test_project_root_and_paths_cannot_escape_allowed_roots(tmp_path):
    outside = tmp_path.parent / "outside"
    with pytest.raises(ValueError, match="registered root"):
        make_context(tmp_path, project_root=str(outside))
    context = make_context(tmp_path)
    with pytest.raises(ValueError, match="traversal"):
        context.validate_path("../outside/file.py")


def test_cache_key_separates_sessions_for_same_agent(tmp_path):
    first = make_context(tmp_path, session_id="one")
    second = make_context(tmp_path, session_id="two")
    assert first.cache_key != second.cache_key


def test_search_cache_key_includes_query_limit_threshold_and_filters(tmp_path):
    first = make_context(tmp_path, source_types=("code",), min_score=0.2)
    second = make_context(tmp_path, source_types=("document",), min_score=0.2)
    baseline = first.search_cache_key("query one", limit=5)
    assert baseline != first.search_cache_key("query two", limit=5)
    assert baseline != first.search_cache_key("query one", limit=7)
    assert baseline != first.search_cache_key(
        "query one", limit=5, score_threshold=0.8
    )
    assert baseline != second.search_cache_key("query one", limit=5)


def test_rag_client_propagates_complete_context(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    client = RagClient(context, server_url="http://fixture.invalid")
    captured = {}

    def fake_post(endpoint, payload, session_id=None, **kwargs):
        captured.update({"endpoint": endpoint, "payload": payload, "session_id": session_id})
        return {"status": "success", "results": []}

    monkeypatch.setattr(client, "_post", fake_post)
    client.search("query", limit=3)

    assert captured["endpoint"] == "/search"
    assert captured["payload"]["context"] == context.to_request_dict()
    assert "project_root" not in captured["payload"]["context"]
    assert "allowed_roots" not in captured["payload"]["context"]
    assert captured["payload"]["query"] == "query"


def test_rag_client_requires_validated_context():
    with pytest.raises(TypeError, match="ProjectContext"):
        RagClient(None)  # type: ignore[arg-type]


def test_registry_rejects_client_controlled_root_expansion(tmp_path):
    registry = ProjectRegistry(
        [ProjectDefinition("project-a", "Project A", {"primary": tmp_path})]
    )
    with pytest.raises(ValueError, match="cannot replace"):
        registry.resolve(
            project_id="project-a",
            root_id="primary",
            write_visibility="shared",
            read_scope="shared",
            agent_id="agent-a",
            session_id="session-a",
            project_root_hint="/",
        )
    with pytest.raises(ValueError, match="cannot expand"):
        registry.resolve(
            project_id="project-a",
            root_id="primary",
            write_visibility="shared",
            read_scope="shared",
            agent_id="agent-a",
            session_id="session-a",
            allowed_roots_hint=["/"],
        )


def test_active_root_is_selected_by_root_id_and_cannot_cross_to_sibling(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    registry = ProjectRegistry(
        [ProjectDefinition("project-a", "Project A", {"first": first, "second": second})]
    )
    context = registry.resolve(
        project_id="project-a",
        root_id="first",
        write_visibility="shared",
        read_scope="shared",
        agent_id="agent-a",
        session_id="session-a",
    )
    assert context.validate_path(first / "new.py") == first / "new.py"
    with pytest.raises(ValueError, match="outside the active"):
        context.validate_path(second / "new.py")


def test_validate_path_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    context = make_context(root)
    with pytest.raises(ValueError, match="outside the active"):
        context.validate_path(root / "link" / "new.py")


def test_unregistered_workspace_is_stable_and_registered_root_keeps_identity(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    kwargs = dict(write_visibility="shared", read_scope="shared", agent_id="agent", session_id="session")
    empty = ProjectRegistry([])
    first = empty.resolve(working_directory=str(root), **kwargs)
    second = empty.resolve(working_directory=str(root), **kwargs)
    assert first.project_id.startswith("ws:")
    assert first.project_id == second.project_id
    assert first.ad_hoc and first.to_request_dict()["working_directory"] == str(root)
    knowledge = empty.resolve_knowledge(working_directory=str(root), read_scope="shared", agent_id="agent", session_id="session")
    assert knowledge.project_id == first.project_id
    registered = ProjectRegistry([ProjectDefinition("legacy", "Legacy", {"primary": root})])
    assert registered.resolve(working_directory=str(root), **kwargs).project_id == "legacy"


@pytest.mark.parametrize("path", ["/", "/etc", "/proc", "/sys", "/root"])
def test_ad_hoc_workspace_rejects_system_paths(path):
    from project_context import workspace_definition
    with pytest.raises(ValueError):
        workspace_definition(path)


def test_ad_hoc_workspace_rejects_symlinks(tmp_path):
    from project_context import workspace_definition
    root = tmp_path / "root"
    root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        workspace_definition(link)


def test_rootless_write_context_cannot_be_used_for_indexing():
    from fastapi import HTTPException
    from gpu_rag_server import ProjectContextRequest
    from project_context import GlobalWriteContext

    request = ProjectContextRequest(global_write=True)
    context = request.to_context(agent_id="agent", session_id="session", allow_rootless=True)
    assert isinstance(context, GlobalWriteContext)
    client = RagClient(context, server_url="http://fixture.invalid")
    with pytest.raises(TypeError, match="filesystem"):
        client.index_code("/etc/passwd")
    with pytest.raises(HTTPException) as error:
        request.to_context(agent_id="agent", session_id="session")
    assert error.value.status_code == 422


def test_unknown_project_id_fails_loudly_rather_than_returning_nothing():
    """The `project_id` trap has to be loud, since it cannot be made safe.

    `project_id` is a hard equality filter in every retrieval predicate, so a
    search under the wrong one matches nothing. If resolution silently fell back
    to a default, that would be indistinguishable from "the store has no answer"
    — the worst possible failure for a knowledge base, because the caller would
    conclude the knowledge was never saved.

    It raises instead, which the HTTP layer turns into a 403. This test exists to
    stop anyone "helpfully" adding a fallback.
    """
    import config
    from project_context import get_project_registry

    registry = get_project_registry(config.settings)

    with pytest.raises(ValueError, match="Unknown project_id"):
        registry.resolve(
            project_id="a-project-that-was-never-registered",
            root_id="primary",
            write_visibility="shared",
            read_scope="shared",
            agent_id="agent-a",
            session_id="session-a",
        )


def test_knowledge_scope_resolves_project_without_filesystem_roots():
    registry = ProjectRegistry([ProjectDefinition("memory-only", "Memory only", {})])
    context = registry.resolve_knowledge(
        project_id="memory-only",
        read_scope="shared",
        agent_id="agent-a",
        session_id="session-a",
    )

    assert isinstance(context, KnowledgeContext)
    assert context.project_id == "memory-only"
    assert "root_id" not in context.to_request_dict()
    assert "project_root" not in context.to_request_dict()


def test_http_search_contract_accepts_rootless_knowledge_scope(monkeypatch):
    import gpu_rag_server

    registry = ProjectRegistry([ProjectDefinition("memory-only", "Memory only", {})])
    monkeypatch.setattr(gpu_rag_server, "get_project_registry", lambda _settings: registry)
    request = gpu_rag_server.SearchRequest.model_validate({
        "context": {
            "project_id": "memory-only",
            "read_scope": "shared",
            "registry_fingerprint": registry.fingerprint,
        },
        "query": "find the stored note",
    })

    context = request.context.to_context(agent_id="server-agent", session_id="server-session")
    assert isinstance(context, KnowledgeContext)
    assert context.project_id == "memory-only"
    assert not hasattr(context, "project_root")


def test_rag_client_accepts_knowledge_scope_and_uses_configured_search_timeout(monkeypatch):
    import config

    registry = ProjectRegistry([ProjectDefinition("memory-only", "Memory only", {})])
    context = registry.resolve_knowledge(
        project_id="memory-only",
        read_scope="shared",
        agent_id="agent-a",
        session_id="session-a",
    )
    client = RagClient(context, server_url="http://fixture.invalid")
    captured = {}

    def fake_post(endpoint, payload, session_id=None, *, timeout=None):
        captured.update(endpoint=endpoint, payload=payload, timeout=timeout)
        return {"status": "success", "results": []}

    monkeypatch.setattr(client, "_post", fake_post)
    client.search("query")

    assert captured["endpoint"] == "/search"
    assert captured["payload"]["context"] == context.to_request_dict()
    assert captured["timeout"] == config.settings.rag_search_timeout_seconds


def test_rag_client_converts_socket_timeout_to_unavailable(monkeypatch, tmp_path):
    import urllib.request

    context = make_context(tmp_path)
    client = RagClient(context, server_url="http://fixture.invalid")
    def timeout(*_args, **_kwargs):
        raise TimeoutError()

    monkeypatch.setattr(urllib.request, "urlopen", timeout)

    assert client._post("/search", {}) == {
        "status": "unavailable",
        "message": "RAG search request timed out",
    }
