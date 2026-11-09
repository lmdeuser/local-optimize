import json

import pytest

import skills_manager as manager

pytestmark = pytest.mark.unit


def _write_catalog(tmp_path, skills):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    for item in skills:
        (skills_dir / item["filename"]).write_text("fixture skill", encoding="utf-8")
    manifest = skills_dir / "manifest.json"
    manifest.write_text(json.dumps({"version": "1", "skills": skills}), encoding="utf-8")
    return skills_dir, manifest


def test_production_manifest_is_strict_and_collision_free():
    manifest = manager.load_manifest(strict=True)
    assert manifest["skills"]


def test_alias_collision_lists_all_canonical_ids(tmp_path, monkeypatch):
    skills = [
        {"id": "first", "filename": "first.md", "required": True, "aliases": ["collision"]},
        {"id": "second", "filename": "second.md", "required": True, "aliases": ["collision"]},
    ]
    skills_dir, manifest = _write_catalog(tmp_path, skills)
    monkeypatch.setattr(manager, "PRIMARY_SKILLS_DIR", skills_dir)
    monkeypatch.setattr(manager, "MANIFEST_FILE", manifest)

    with pytest.raises(ValueError) as error:
        manager.load_manifest(strict=True)

    assert "first" in str(error.value)
    assert "second" in str(error.value)


def test_unlisted_file_cannot_bypass_manifest(tmp_path, monkeypatch):
    skills = [{"id": "listed", "filename": "listed.md", "required": True, "aliases": []}]
    skills_dir, manifest = _write_catalog(tmp_path, skills)
    (skills_dir / "unlisted.md").write_text("not allowed", encoding="utf-8")
    monkeypatch.setattr(manager, "PRIMARY_SKILLS_DIR", skills_dir)
    monkeypatch.setattr(manager, "MANIFEST_FILE", manifest)
    with pytest.raises(KeyError, match="strict manifest"):
        manager.safe_skills_path("unlisted")


def test_tuple_session_key_has_no_delimiter_collision():
    assert manager.SessionSkillsManager._session_key("a:b", "c") != manager.SessionSkillsManager._session_key("a", "b:c")


def test_ttl_and_lru_keep_cache_within_session_limit(tmp_path, monkeypatch):
    skill = tmp_path / "skill.md"
    skill.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(manager, "safe_skills_path", lambda _name: skill)
    clock = [0.0]
    cache = manager.SessionSkillsManager(ttl_seconds=10, max_sessions=2, clock=lambda: clock[0])
    cache.load_skill_for_session("agent", "one", "skill")
    clock[0] = 1.0
    cache.load_skill_for_session("agent", "two", "skill")
    clock[0] = 2.0
    cache.get_session_skills("agent", "one")  # one becomes MRU
    cache.load_skill_for_session("agent", "three", "skill")
    assert len(cache._sessions) == 2
    assert ("agent", "two") not in cache._sessions
    clock[0] = 20.0
    assert cache.get_session_skills("agent", "one") == {}
    assert len(cache._sessions) == 0


def test_cumulative_session_bytes_are_bounded(tmp_path, monkeypatch):
    skill = tmp_path / "large.md"
    skill.write_bytes(b"x" * (manager.MAX_SESSION_BYTES + 1))
    monkeypatch.setattr(manager, "safe_skills_path", lambda _name: skill)
    monkeypatch.setattr(manager, "MAX_SKILL_BYTES", manager.MAX_SESSION_BYTES + 2)
    cache = manager.SessionSkillsManager()
    with pytest.raises(ValueError, match="content limit"):
        cache.load_skill_for_session("agent", "session", "large")


def test_manifest_depth_limit_is_enforced(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    nested = value = {}
    for _ in range(manager.MAX_MANIFEST_DEPTH + 2):
        value["child"] = {}
        value = value["child"]
    manifest = skills_dir / "manifest.json"
    manifest.write_text(json.dumps({"skills": [], "nested": nested}), encoding="utf-8")
    monkeypatch.setattr(manager, "PRIMARY_SKILLS_DIR", skills_dir)
    monkeypatch.setattr(manager, "MANIFEST_FILE", manifest)
    with pytest.raises(ValueError, match="nesting depth"):
        manager.load_manifest(strict=True)
