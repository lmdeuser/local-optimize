"""Unit tests covering audit remediation v16 regressions across server, client, router, hooks, and installer."""

from __future__ import annotations

from collections import OrderedDict
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from config import settings  # noqa: E402
import fast_router  # noqa: E402
import gpu_rag_server  # noqa: E402
from hooks import deny_git, rag_harvest  # noqa: E402
from installer_context import InstallationContext, ManagedTarget, deduplicate_targets  # noqa: E402
from project_context import ProjectContext, ProjectDefinition, ProjectRegistry, ReadScope, WriteVisibility  # noqa: E402
from rag_client import MAX_INJECTED_HASHES, RagClient  # noqa: E402

pytestmark = pytest.mark.unit

SESSION_HEADERS = {"X-Local-RAG-Session": "test-session-1234567890"}


def _test_registry(tmp_path: Path) -> ProjectRegistry:
    return ProjectRegistry([ProjectDefinition("proj-a", "Project A", {"primary": tmp_path})])


def _ctx_payload(tmp_path: Path) -> dict[str, object]:
    return {
        "working_directory": str(tmp_path),
        "write_visibility": "shared",
        "read_scope": "shared",
    }


def _fake_project_context(tmp_path: Path) -> ProjectContext:
    return ProjectContext(
        "proj-a",
        "Project A",
        "primary",
        str(tmp_path),
        (str(tmp_path),),
        WriteVisibility.SHARED,
        ReadScope.SHARED,
        "agent-a",
        "session-a",
    )


def test_server_cache_invalidate_consolidate_404_and_agent_header(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    class ServerEngine:
        _pool = object()

        def invalidate_caches(self, project_id=None, *, purge=False):
            captured["invalidate"] = (project_id, purge)
            return {"status": "success", "generation": 42, "rows_removed": 3}

        def consolidate(self, context, *, decay_days=30.0, min_importance=1):
            captured["consolidate_agent"] = context.agent_id
            if min_importance > 5:
                return {"status": "error"}
            return {"status": "success", "consolidated": 2, "decay_days": decay_days}

        def move_document(self, context, source, destination):
            raise FileNotFoundError("disappeared")

        def close(self):
            return None

    engine = ServerEngine()
    monkeypatch.setattr(gpu_rag_server, "get_project_registry", lambda _s: _test_registry(tmp_path))
    monkeypatch.setattr(gpu_rag_server.app.state, "engine_factory", lambda: engine, raising=False)
    gpu_rag_server.app.dependency_overrides[gpu_rag_server.get_rag_engine] = lambda: engine
    try:
        with TestClient(gpu_rag_server.app) as client:
            inv = client.post(
                "/cache/invalidate",
                json={"project_id": "proj-a", "purge": True},
                headers=SESSION_HEADERS,
            )
            assert inv.status_code == 200
            assert inv.json() == {"status": "success", "generation": 42, "rows_removed": 3}
            assert captured["invalidate"] == ("proj-a", True)

            ok = client.post(
                "/consolidate",
                json={"context": _ctx_payload(tmp_path), "decay_days": 14.0, "min_importance": 2},
                headers={**SESSION_HEADERS, "X-Local-RAG-Agent": "cursor-agent_1"},
            )
            assert ok.status_code == 200
            assert ok.json()["status"] == "success"
            assert captured["consolidate_agent"] == "cursor-agent_1"

            default_agent = client.post(
                "/consolidate",
                json={"context": _ctx_payload(tmp_path), "decay_days": 14.0, "min_importance": 2},
                headers=SESSION_HEADERS,
            )
            assert default_agent.status_code == 200
            assert captured["consolidate_agent"] == settings.server_agent_id

            err = client.post(
                "/consolidate",
                json={"context": _ctx_payload(tmp_path), "min_importance": 9},
                headers=SESSION_HEADERS,
            )
            assert err.status_code == 503
            assert err.json()["detail"]["error"]["code"] == "CONSOLIDATE_ERROR"

            src = tmp_path / "src.py"
            src.write_text("x = 1\n", encoding="utf-8")
            missing_dst = tmp_path / "missing_dst.py"
            invalid_dst = client.post(
                "/move_document",
                json={
                    "context": _ctx_payload(tmp_path),
                    "source_path": str(src),
                    "destination_path": str(missing_dst),
                },
                headers=SESSION_HEADERS,
            )
            assert invalid_dst.status_code == 403
            assert invalid_dst.json()["detail"]["error"]["code"] == "INVALID_PATH"

            existing_dst = tmp_path / "existing_dst.py"
            existing_dst.write_text("y = 2\n", encoding="utf-8")
            nf = client.post(
                "/move_document",
                json={
                    "context": _ctx_payload(tmp_path),
                    "source_path": str(src),
                    "destination_path": str(existing_dst),
                },
                headers=SESSION_HEADERS,
            )
            assert nf.status_code == 404
            assert nf.json()["detail"]["error"]["code"] == "PATH_NOT_FOUND"

            unexposed = client.post("/execute_and_learn", json={}, headers=SESSION_HEADERS)
            assert unexposed.status_code == 404

            bad_agent = client.post(
                "/consolidate",
                json={"context": _ctx_payload(tmp_path)},
                headers={**SESSION_HEADERS, "X-Local-RAG-Agent": "bad agent spaces!"},
            )
            assert bad_agent.status_code == 400
            assert bad_agent.json()["detail"]["error"]["code"] == "MALFORMED_AGENT"
    finally:
        gpu_rag_server.app.dependency_overrides.clear()


def test_rag_client_forwards_agent_header_and_bounds_injected_hashes(tmp_path, monkeypatch):
    ctx = _fake_project_context(tmp_path)
    client = RagClient(ctx, server_url="http://127.0.0.1:8181")
    seen_headers: list[dict[str, str]] = []

    class FakeResp:
        def __init__(self, payload: dict[str, object]):
            self._data = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_open(req, *, timeout):
        seen_headers.append(dict(req.header_items()))
        if req.full_url.endswith("/liveness"):
            return FakeResp({"status": "success"})
        return FakeResp({"status": "success", "results": [], "chunk_id": 1})

    monkeypatch.setattr("rag_client._open_url", fake_open)
    assert client.health()["status"] == "success"
    assert client.save("durable fact")["status"] == "success"
    assert any(
        any(k.lower() == "x-local-rag-agent" and v == "agent-a" for k, v in h.items())
        for h in seen_headers
    )

    assert isinstance(client._injected_hashes, OrderedDict)
    for idx in range(MAX_INJECTED_HASHES + 25):
        client._record_injected_hash(("proj-a", "shared"), f"hash-{idx}")
    assert len(client._injected_hashes) == MAX_INJECTED_HASHES
    assert (("proj-a", "shared"), "hash-0") not in client._injected_hashes
    assert (("proj-a", "shared"), f"hash-{MAX_INJECTED_HASHES + 24}") in client._injected_hashes


@pytest.mark.parametrize("bad_answer", ["Смотри [Evidence 1, 2].", "Ответ [Evidence 1] [1]."])
def test_fast_router_rejects_malformed_citations(monkeypatch, bad_answer):
    evidence = fast_router.build_retrieved_evidence(
        [
            {"content": "Alpha definition in core.py", "source_type": "code", "source_uri": "core.py"},
            {"content": "Beta definition in util.py", "source_type": "code", "source_uri": "util.py"},
        ]
    )
    monkeypatch.setattr(fast_router, "query_local_ollama", Mock(return_value=bad_answer))

    escalated = fast_router.route_query("где Alpha и Beta?", evidence, 0.9, privacy_mode=False)
    assert escalated["decision"] == "escalate"
    assert escalated["answer_diagnostics"]["reason"] == "malformed_citation"

    private = fast_router.route_query("где Alpha и Beta?", evidence, 0.9, privacy_mode=True)
    assert private["response_kind"] == "policy_fallback"
    assert private["answer_diagnostics"]["reason"] == "malformed_citation"


def test_deny_git_blocks_git_glob_and_allows_non_git_grep_and_gitignore():
    blocked = deny_git.evaluate("Glob", {"pattern": ".git/**"})
    assert blocked == deny_git.DENY_MESSAGE

    assert deny_git.evaluate("Grep", {"pattern": "git"}) is None
    assert deny_git.evaluate("Bash", {"command": "cat .gitignore"}) is None


def test_rag_harvest_symlink_state_dir_secrets_and_incremental_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_OPTIMIZE_STATE_DIR", str(tmp_path / "state"))
    assert Path(rag_harvest._state_hooks_dir()) == tmp_path / "state" / "hooks"

    real_transcript = tmp_path / "transcript.jsonl"
    real_transcript.write_text(json.dumps({"content": "hello"}) + "\n", encoding="utf-8")
    link_transcript = tmp_path / "link.jsonl"
    link_transcript.symlink_to(real_transcript)
    assert rag_harvest.extract_recent_text(str(link_transcript), session_id="sess-symlink") == ""

    for secret in (
        "ghp_" + "A" * 24,
        "xoxb-" + "12345-67890-abcdef",
        "-----BEGIN PRIVATE KEY-----",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
    ):
        assert rag_harvest.SECRET_PATTERN.search(secret), f"Missed secret pattern: {secret}"

    # Incremental cursor: holds back partial trailing non-JSON line until completed.
    stream_file = tmp_path / "stream.jsonl"
    line1 = json.dumps({"content": "first complete fact"}) + "\n"
    partial = '{"content": "incomplete'
    stream_file.write_text(line1 + partial, encoding="utf-8")
    out1 = rag_harvest.extract_recent_text(str(stream_file), session_id="sess-cursor")
    assert out1 == "first complete fact"
    cursor_file = rag_harvest._cursor_path("sess-cursor", str(stream_file))
    assert rag_harvest._read_cursor(cursor_file) == len(line1.encode("utf-8"))

    with stream_file.open("a", encoding="utf-8") as handle:
        handle.write(' tail"}\n')
    out2 = rag_harvest.extract_recent_text(str(stream_file), session_id="sess-cursor")
    assert out2 == "incomplete tail"

    # Single complete JSON line without trailing newline is still accepted.
    no_nl = tmp_path / "no_nl.jsonl"
    no_nl.write_text(json.dumps({"content": "single line without nl"}), encoding="utf-8")
    assert rag_harvest.extract_recent_text(str(no_nl), session_id="sess-nonl") == "single line without nl"


def test_installer_context_rejects_symlink_targets_and_parent_components(tmp_path):
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    real_file = real_dir / "config.json"
    real_file.write_text("{}\n", encoding="utf-8")

    sym_parent = tmp_path / "sym_dir"
    sym_parent.symlink_to(real_dir, target_is_directory=True)
    sym_child = sym_parent / "config.json"

    sym_leaf = tmp_path / "leaf.json"
    sym_leaf.symlink_to(real_file)

    with pytest.raises(RuntimeError, match="Symlink path component is not permitted"):
        deduplicate_targets([ManagedTarget("sym-parent", sym_child)])

    ctx = InstallationContext(
        dry_run=False,
        backup_root=tmp_path / "backups",
        targets=(ManagedTarget("sym-leaf", sym_leaf),),
    )
    with pytest.raises(RuntimeError, match="Symlink path component is not permitted"):
        ctx.create_backup()
