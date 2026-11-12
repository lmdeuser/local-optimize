#!/usr/bin/env python3
"""
Unit tests for deterministic stable_key generation, build_dedup_scope,
Path Traversal protection, and Secret masking.
"""

import pytest
from config import build_entity_stable_key, build_dedup_scope, SecretString
from skills_manager import SessionSkillsManager

pytestmark = pytest.mark.unit


def test_build_entity_stable_key_basic():
    key = build_entity_stable_key("proj_a", "src/main.py", "function", "main", root_id="primary")
    assert key == "proj_a:primary:shared:::src/main.py:function:main"


def test_build_entity_stable_key_separates_roots_and_owner_scopes():
    arguments = ("proj_a", "src/main.py", "function", "main")
    first = build_entity_stable_key(*arguments, root_id="backend")
    second = build_entity_stable_key(*arguments, root_id="frontend")
    private = build_entity_stable_key(
        *arguments,
        root_id="backend",
        visibility="session-private",
        agent_id="agent-a",
        session_id="session-a",
    )
    assert first != second
    assert first != private
    assert private.startswith("proj_a:backend:session-private:agent-a:session-a:")


def test_build_entity_stable_key_requires_root():
    with pytest.raises(ValueError, match="root_id is required"):
        build_entity_stable_key("proj_a", "src/main.py", "function", "main")


def test_build_entity_stable_key_path_traversal_rejection():
    with pytest.raises(ValueError, match="Path traversal '..' forbidden"):
        build_entity_stable_key("proj_a", "../etc/passwd", "file", "passwd", root_id="primary")


def test_build_dedup_scope_tuple():
    scope = build_dedup_scope(
        project_name="local_optimize",
        source_uri="http://example.com",
        file_path="app.py",
        content_hash="abc123hash",
        source_type="code",
        visibility="shared",
        agent_id="agent1",
        session_id="session1",
        validation_status="validated"
    )
    assert scope == (
        "local_optimize",
        "http://example.com",
        "app.py",
        "abc123hash",
        "code",
        "shared",
        "agent1",
        "session1",
        "validated"
    )


def test_secret_string_masking():
    secret = SecretString("super_secret_password_123")
    assert str(secret) == "**********"
    assert repr(secret) == "SecretString('**********')"
    assert secret.get_secret_value() == "super_secret_password_123"


def test_session_skills_manager_isolation():
    mgr = SessionSkillsManager()
    mgr.load_skill_for_session("agent_x", "sess_y", "senior_macos_expert")
    active = mgr.get_session_skills("agent_x", "sess_y")
    assert len(active) >= 1
