import json
from types import SimpleNamespace

import pytest

import config
from project_context import (
    ProjectContext,
    ProjectDefinition,
    ProjectRegistry,
    ReadScope,
    WriteVisibility,
    get_project_registry,
)
from rag_client import RagClient, RagProtocolError

pytestmark = pytest.mark.unit


def _context(root, *, fingerprint=""):
    return ProjectContext(
        "project-a",
        "Project A",
        "primary",
        str(root),
        (str(root),),
        WriteVisibility.SESSION_PRIVATE,
        ReadScope.SESSION,
        "server-agent",
        "server-session",
        registry_fingerprint=fingerprint,
    )


def test_versioned_registry_has_stable_fingerprint_and_rejects_mismatch(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    registry_file = tmp_path / "projects.json"
    payload = {
        "version": 1,
        "projects": [
            {"project_name": "Project A", "roots": {"primary": str(root)}, "project_id": "project-a"}
        ],
    }
    registry_file.write_text(json.dumps(payload), encoding="utf-8")
    first = ProjectRegistry.from_file(registry_file)
    registry_file.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    second = ProjectRegistry.from_file(registry_file)
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint.startswith("sha256:")

    payload["fingerprint"] = "sha256:" + "0" * 64
    registry_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        ProjectRegistry.from_file(registry_file)


def test_registry_loader_is_singleton_and_missing_file_has_no_fallback(tmp_path):
    root = tmp_path / "builtin"
    root.mkdir()
    settings = SimpleNamespace(
        projects_file=tmp_path / "missing-projects.json",
        project_root=root,
        allowed_index_roots=[root],
    )
    registry = get_project_registry(settings)
    assert registry is get_project_registry(settings)
    assert registry.project_ids == ()
    with pytest.raises(ValueError, match="Unknown project_id"):
        registry.resolve(
            project_id="builtin",
            root_id="primary",
            write_visibility="session-private",
            read_scope="session",
            agent_id="server-agent",
            session_id="server-session",
        )


def test_registry_requires_existing_directory_and_fails_closed_on_hint(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="existing directory"):
        ProjectDefinition("project-a", "Project A", {"primary": missing})

    root = tmp_path / "root"
    root.mkdir()
    registry = ProjectRegistry([ProjectDefinition("project-a", "Project A", {"primary": root})])
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        registry.resolve(
            project_id="project-a",
            root_id="primary",
            write_visibility="session-private",
            read_scope="session",
            agent_id="server-agent",
            session_id="server-session",
            registry_fingerprint_hint="sha256:stale",
        )


def test_validate_path_allows_missing_ancestors_and_rejects_broken_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    context = _context(root)
    assert context.validate_path(root / "missing" / "new.py") == root / "missing" / "new.py"
    (root / "link").symlink_to(tmp_path / "outside" / "new.py")
    with pytest.raises(ValueError, match="does not exist"):
        context.validate_path(root / "link")


def test_settings_require_token_for_non_loopback_and_propagate_ssl(monkeypatch):
    monkeypatch.setattr(config, "load_secrets_file", lambda *_args, **_kwargs: {})
    for key in config.OWNED_SECRET_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DB_SSLMODE", "verify-full")
    monkeypatch.setenv("DB_SSLROOTCERT", "/certs/root.pem")
    monkeypatch.setenv("DB_SSLCERT", "/certs/client.pem")
    monkeypatch.setenv("DB_SSLKEY", "/certs/client.key")
    settings = config.Settings()
    settings.require_safe_bind("127.0.0.1")
    with pytest.raises(RuntimeError, match="bearer"):
        settings.require_safe_bind("0.0.0.0")
    params = settings.get_db_params()
    assert params["sslmode"] == "verify-full"
    assert params["sslrootcert"] == "/certs/root.pem"
    assert params["sslcert"] == "/certs/client.pem"
    assert params["sslkey"] == "/certs/client.key"


def test_settings_reject_invalid_sslmode(monkeypatch):
    monkeypatch.setattr(config, "load_secrets_file", lambda *_args, **_kwargs: {})
    monkeypatch.setenv("DB_SSLMODE", "unsafe")
    with pytest.raises(ValueError, match="DB_SSLMODE"):
        config.Settings()


def test_rag_client_rejects_malformed_or_incomplete_protocol(tmp_path):
    client = RagClient(_context(tmp_path), server_url="http://fixture.invalid")
    with pytest.raises(RagProtocolError, match="invalid JSON"):
        client._validate_response("/search", b"not-json")
    with pytest.raises(RagProtocolError, match="missing required fields"):
        client._validate_response("/search", b'{"status":"success","results":[]}')
    with pytest.raises(RagProtocolError, match="invalid or missing status"):
        client._validate_response("/search", b'{"status":"ok","results":[]}')


def test_rag_client_enforces_shared_limits_before_transport(tmp_path):
    client = RagClient(_context(tmp_path), server_url="http://fixture.invalid")
    with pytest.raises(ValueError, match="query length"):
        client.search("x" * (config.settings.max_query_chars + 1))
    with pytest.raises(ValueError, match="limit"):
        client.search("query", limit=config.settings.max_search_limit + 1)


def test_mcp_correlation_ignores_client_identity():
    import mcp_server

    correlation = mcp_server.build_correlation_context(
        "untrusted-agent", "untrusted-session", "fixture"
    )
    assert correlation["agent_id"] == mcp_server.MCP_AGENT_ID
    assert correlation["session_id"] == mcp_server.MCP_SESSION_ID
    assert correlation["session_id"] != "untrusted-session"



def test_mcp_network_transports_are_fail_closed():
    import mcp_server

    assert mcp_server.validate_mcp_transport("stdio") == "stdio"
    for transport in ("http", "sse", "streamable-http", ""):
        with pytest.raises(RuntimeError, match="network transports are disabled"):
            mcp_server.validate_mcp_transport(transport)



def test_load_skills_returns_bounded_skill_content():
    import asyncio
    import mcp_server

    result = json.loads(asyncio.run(mcp_server.load_skills(
        ["python-engineering"], agent_id="test-agent", session_id="test-session"
    )))

    assert result["status"] == "success"
    loaded = result["loaded"][0]
    assert loaded["skill"] == "python-engineering"
    assert loaded["status"] == "active"
    assert loaded["content"].startswith("# Python Architecture and Engineering Standards")
    assert len(loaded["content"].encode("utf-8")) < 500 * 1024
