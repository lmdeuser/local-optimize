import inspect
from types import SimpleNamespace

import pytest

from project_context import ProjectDefinition, ProjectRegistry, get_project_registry


pytestmark = pytest.mark.unit


def make_registry(parent, nested=None, sibling=None):
    projects = [ProjectDefinition("parent", "Parent", {"primary": parent})]
    if nested is not None:
        projects.append(ProjectDefinition("nested", "Nested", {"primary": nested}))
    if sibling is not None:
        projects.append(ProjectDefinition("sibling", "Sibling", {"primary": sibling}))
    return ProjectRegistry(projects)


def resolve(registry, project_id="parent", root_id="primary", **kwargs):
    return registry.resolve(
        project_id=project_id,
        root_id=root_id,
        write_visibility="shared",
        read_scope="shared",
        agent_id="agent-a",
        session_id="session-a",
        **kwargs,
    )


def resolve_knowledge(registry, **kwargs):
    return registry.resolve_knowledge(
        project_id="parent",
        read_scope="shared",
        agent_id="agent-a",
        session_id="session-a",
        **kwargs,
    )


def test_empty_registry_is_valid_and_missing_file_has_no_builtin_project(tmp_path):
    assert ProjectRegistry([]).project_ids == ()

    settings = SimpleNamespace(
        projects_file=tmp_path / "missing.json",
        project_root=tmp_path,
        allowed_index_roots=[tmp_path],
    )
    registry = get_project_registry(settings)
    with pytest.raises(ValueError, match="Unknown project_id"):
        resolve(registry, project_id=tmp_path.name)


def test_scoped_fingerprint_ignores_sibling_but_tracks_nested_ownership(tmp_path):
    parent = tmp_path / "parent"
    nested = parent / "nested"
    sibling = tmp_path / "sibling"
    nested.mkdir(parents=True)
    sibling.mkdir()

    original = make_registry(parent)
    original_context = resolve(original)
    original_knowledge = resolve_knowledge(original)
    with_sibling = make_registry(parent, sibling=sibling)
    assert resolve(with_sibling).context_fingerprint == original_context.context_fingerprint
    assert resolve_knowledge(with_sibling).context_fingerprint == original_knowledge.context_fingerprint

    with_nested = make_registry(parent, nested=nested)
    assert resolve(with_nested).context_fingerprint != original_context.context_fingerprint
    assert resolve_knowledge(with_nested).context_fingerprint != original_knowledge.context_fingerprint
    with pytest.raises(ValueError, match="Context fingerprint mismatch"):
        resolve(with_nested, context_fingerprint_hint=original_context.context_fingerprint)


def test_working_directory_uses_longest_registered_root(tmp_path):
    parent = tmp_path / "parent"
    nested = parent / "nested"
    working = nested / "src"
    working.mkdir(parents=True)
    registry = make_registry(parent, nested=nested)

    context = registry.resolve(
        working_directory=str(working),
        write_visibility="shared",
        read_scope="shared",
        agent_id="agent-a",
        session_id="session-a",
    )
    assert context.project_id == "nested"
    assert context.project_root == str(nested.resolve())


def test_nested_root_has_one_owner_for_indexing_and_stale_results(tmp_path):
    parent = tmp_path / "parent"
    nested = parent / "nested"
    source = nested / "source.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    registry = make_registry(parent, nested=nested)

    owner = registry.owner_for_path(source)
    assert (owner.project_id, owner.root_id) == ("nested", "primary")
    assert not registry.is_owned_by(source, project_id="parent", root_id="primary")


def test_mcp_and_lmstudio_have_no_engine_project_default():
    import lmstudio_rag_mcp
    import mcp_server

    assert inspect.signature(mcp_server.rag_search).parameters["project_id"].default == ""
    parameters = inspect.signature(lmstudio_rag_mcp.search_local_rag).parameters
    assert "working_directory" in parameters
    assert "global_search" in parameters


def test_nested_registration_invalidates_parent_local_answer_scope(tmp_path):
    import rag_engine

    parent = tmp_path / "parent"
    nested = parent / "nested"
    nested.mkdir(parents=True)
    original = make_registry(parent)
    original_context = resolve_knowledge(original)
    with_nested = make_registry(parent, nested=nested)
    nested_context = resolve_knowledge(with_nested)
    engine = object.__new__(rag_engine.RagEngine)

    old_scope = engine._search_scope_key(original_context, 1) + ("local-answer-v1",)
    new_scope = engine._search_scope_key(nested_context, 1) + ("local-answer-v1",)
    assert old_scope != new_scope


def test_cached_parent_hit_is_rejected_after_nested_registration(tmp_path, monkeypatch):
    import rag_engine

    parent = tmp_path / "parent"
    nested = parent / "nested"
    source = nested / "source.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        rag_engine,
        "get_project_registry",
        lambda _settings: make_registry(parent, nested=nested),
    )
    cached = {
        "status": "success",
        "results": [{
            "chunk_id": 1,
            "project_id": "parent",
            "root_id": "primary",
            "source_uri": str(source),
            "content": "nested content",
            "metadata": {},
            "final_score": 0.9,
        }],
    }
    checked = rag_engine.RagEngine._check_source_versions("parent", cached)
    assert checked["results"] == []
    assert checked["reason"] == "ownership_changed"


def test_engine_indexing_rejects_file_owned_by_nested_project(tmp_path, monkeypatch):
    import rag_engine

    parent = tmp_path / "parent"
    nested = parent / "nested"
    source = nested / "source.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    registry = make_registry(parent, nested=nested)
    monkeypatch.setattr(rag_engine, "get_project_registry", lambda _settings: registry)
    parent_context = resolve(registry, project_id="parent")

    with pytest.raises(ValueError, match="more specific"):
        rag_engine.RagEngine._validate_owned_path(parent_context, source)


def test_revoked_or_changed_root_rejects_old_scoped_fingerprint(tmp_path):
    parent = tmp_path / "parent"
    moved = tmp_path / "moved"
    parent.mkdir()
    moved.mkdir()
    original = make_registry(parent)
    context = resolve(original)
    changed = ProjectRegistry([ProjectDefinition("parent", "Parent", {"primary": moved})])
    with pytest.raises(ValueError, match="Context fingerprint mismatch"):
        resolve(changed, context_fingerprint_hint=context.context_fingerprint)
    with pytest.raises(ValueError, match="Unknown project_id"):
        resolve(ProjectRegistry([]), context_fingerprint_hint=context.context_fingerprint)


def test_duplicate_root_owner_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError, match="Ambiguous duplicate"):
        ProjectRegistry([
            ProjectDefinition("first", "First", {"primary": root}),
            ProjectDefinition("second", "Second", {"primary": root}),
        ])


@pytest.mark.asyncio
async def test_mcp_roots_are_workspace_hint_not_implicit_multi_root_choice(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import mcp_server

    root = tmp_path / "client"
    root.mkdir()
    roots = [SimpleNamespace(uri=root.as_uri())]

    class Session:
        async def list_roots(self):
            return SimpleNamespace(roots=roots)

    monkeypatch.setattr(mcp_server.mcp, "get_context", lambda: SimpleNamespace(session=Session()))
    assert await mcp_server.client_working_directory() == str(root)

    roots.append(SimpleNamespace(uri=(tmp_path / "other").as_uri()))
    with pytest.raises(mcp_server.ClientContextError) as error:
        await mcp_server.client_working_directory()
    assert error.value.code == "AMBIGUOUS_CLIENT_CONTEXT"


def test_rag_client_sends_cwd_and_global_without_fake_project(tmp_path, monkeypatch):
    from project_context import GlobalKnowledgeContext, WorkingDirectoryContext
    from rag_client import RagClient

    tmp_path.mkdir(exist_ok=True)
    contexts = [
        WorkingDirectoryContext(
            working_directory=str(tmp_path), read_scope="shared",
            agent_id="agent-a", session_id="session-a",
        ),
        GlobalKnowledgeContext(read_scope="shared", agent_id="agent-a", session_id="session-a"),
    ]
    for context in contexts:
        client = RagClient(context, server_url="http://fixture.invalid")
        captured = {}

        def fake_post(_endpoint, payload, *_args, **_kwargs):
            captured.update(payload)
            return {"status": "success", "results": []}

        monkeypatch.setattr(client, "_post", fake_post)
        client.search("query")
        assert "project_id" not in captured["context"]
        assert ("global_search" in captured["context"]) == isinstance(context, GlobalKnowledgeContext)


@pytest.mark.asyncio
async def test_mcp_mutation_reports_unknown_explicit_project(monkeypatch):
    import json
    import mcp_server

    monkeypatch.setattr(mcp_server, "get_project_registry", lambda _settings: ProjectRegistry([]))
    result = json.loads(await mcp_server.rag_save(
        "long enough isolated content",
        project_id="missing",
        root_id="primary",
    ))
    assert result["code"] == "UNKNOWN_PROJECT"
    assert result["status"] == "error"
