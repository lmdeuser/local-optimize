import json
import re

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


def test_production_skill_files_are_accessible():
    manifest = manager.load_manifest(strict=True)
    assert len(manifest["skills"]) == 39
    for item in manifest["skills"]:
        assert manager.safe_skills_path(item["id"]).is_file()


def test_production_conditional_skills_have_activation_keywords():
    manifest = manager.load_manifest(strict=True)
    missing = [
        item["id"]
        for item in manifest["skills"]
        if not item.get("required") and not item.get("activation_keywords")
    ]
    assert missing == []


def test_production_markdown_links_resolve():
    link_pattern = re.compile(r"\[[^]]+\]\(([^)]+)\)")
    for skill_path in manager.PRIMARY_SKILLS_DIR.glob("*.md"):
        for link in link_pattern.findall(skill_path.read_text(encoding="utf-8")):
            if "://" in link or link.startswith(("#", "mailto:")):
                continue
            target = link.split("#", 1)[0]
            if target:
                assert (skill_path.parent / target).resolve().exists(), (skill_path.name, link)


def test_core_skills_match_required_manifest_entries():
    manifest = manager.load_manifest(strict=True)
    expected = {item["filename"] for item in manifest["skills"] if item.get("required")}
    assert set(manager.get_core_skills()) == expected


def _use_activation_keywords(monkeypatch, keywords):
    monkeypatch.setattr(
        manager,
        "load_manifest",
        lambda strict=True: {
            "skills": [
                {
                    "id": "sample",
                    "filename": "sample.md",
                    "required": False,
                    "activation_keywords": keywords,
                }
            ]
        },
    )


@pytest.mark.parametrize("query", ["requirements", "storage", "open performance review"])
def test_keywords_do_not_match_inside_other_words(monkeypatch, query):
    _use_activation_keywords(monkeypatch, ["ts", "rag", "pe"])
    assert manager.get_conditional_skills(query) == []


@pytest.mark.parametrize(
    ("query", "keywords"),
    [
        ("TypeScript", ["typescript"]),
        ("RAG", ["rag"]),
        ("C++", ["c++"]),
        ("reverse   engineering", ["reverse engineering"]),
    ],
)
def test_exact_words_and_phrases_match(monkeypatch, query, keywords):
    _use_activation_keywords(monkeypatch, keywords)
    assert manager.get_conditional_skills(query) == ["sample.md"]


def test_explicit_prefix_matches_unicode_word_forms(monkeypatch):
    _use_activation_keywords(monkeypatch, ["миграц*"])
    assert manager.get_conditional_skills("проверь миграции") == ["sample.md"]
    assert manager.get_conditional_skills("проверь данные") == []


@pytest.mark.parametrize(
    ("query", "unexpected"),
    [
        ("проверь requirements", "javascript-typescript-engineering.md"),
        ("опиши storage", "llm-rag-engineering.md"),
        ("open performance review", "reverse-engineering.md"),
    ],
)
def test_production_router_rejects_known_false_positives(query, unexpected):
    assert unexpected not in manager.get_conditional_skills(query)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("исправь баг в Python", "python-engineering.md"),
        ("проверь миграции PostgreSQL", "postgres-engineering.md"),
        ("RAG и TypeScript с C++", "llm-rag-engineering.md"),
        ("RAG и TypeScript с C++", "javascript-typescript-engineering.md"),
        ("RAG и TypeScript с C++", "cpp-engineering.md"),
        ("проверь совместимость C++20", "cpp-engineering.md"),
    ],
)
def test_production_router_matches_bilingual_queries(query, expected):
    assert expected in manager.get_conditional_skills(query)



def _configure_sync_target(tmp_path, monkeypatch):
    skills_dir, manifest = _write_catalog(
        tmp_path,
        [{"id": "listed", "filename": "listed.md", "required": False, "aliases": []}],
    )
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(manager, "PRIMARY_SKILLS_DIR", skills_dir)
    monkeypatch.setattr(manager, "MANIFEST_FILE", manifest)
    monkeypatch.setattr(manager, "SECONDARY_SKILLS_DIRS", [target])
    return skills_dir, manifest, target


def test_sync_preserves_unmanaged_markdown_files(tmp_path, monkeypatch):
    _skills_dir, _manifest, target = _configure_sync_target(tmp_path, monkeypatch)
    custom = target / "user-custom.md"
    custom.write_text("user-owned", encoding="utf-8")

    result = manager.sync_to_targets()[str(target)]

    assert custom.read_text(encoding="utf-8") == "user-owned"
    assert result["removed"] == []
    assert result["preserved"] == ["user-custom.md"]
    assert (target / "listed.md").is_file()


def test_sync_removes_only_unchanged_stale_files_it_owns(tmp_path, monkeypatch):
    skills_dir, manifest, target = _configure_sync_target(tmp_path, monkeypatch)
    custom = target / "user-custom.md"
    custom.write_text("user-owned", encoding="utf-8")
    manager.sync_to_targets()
    (skills_dir / "listed.md").unlink()
    manifest.write_text(json.dumps({"version": "1", "skills": []}), encoding="utf-8")

    dry_run = manager.sync_to_targets(dry_run=True)[str(target)]
    assert dry_run["removed"] == ["listed.md"]
    assert (target / "listed.md").is_file()

    result = manager.sync_to_targets()[str(target)]
    assert result["removed"] == ["listed.md"]
    assert not (target / "listed.md").exists()
    assert custom.read_text(encoding="utf-8") == "user-owned"


def test_sync_preserves_stale_file_modified_by_user(tmp_path, monkeypatch):
    skills_dir, manifest, target = _configure_sync_target(tmp_path, monkeypatch)
    manager.sync_to_targets()
    managed = target / "listed.md"
    managed.write_text("user changed the deployed copy", encoding="utf-8")
    (skills_dir / "listed.md").unlink()
    manifest.write_text(json.dumps({"version": "1", "skills": []}), encoding="utf-8")

    result = manager.sync_to_targets()[str(target)]

    assert result["removed"] == []
    assert result["preserved"] == ["listed.md"]
    assert managed.read_text(encoding="utf-8") == "user changed the deployed copy"
    state = json.loads((target / manager.SYNC_STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["files"] == {}



def test_sync_preserves_unmanaged_active_name_conflict(tmp_path, monkeypatch):
    _skills_dir, _manifest, target = _configure_sync_target(tmp_path, monkeypatch)
    conflicting = target / "listed.md"
    conflicting.write_text("user-owned active skill", encoding="utf-8")

    result = manager.sync_to_targets()[str(target)]

    assert result["copied"] == []
    assert result["preserved"] == ["listed.md"]
    assert conflicting.read_text(encoding="utf-8") == "user-owned active skill"
    state = json.loads((target / manager.SYNC_STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["files"] == {}


def test_sync_preserves_active_file_modified_after_previous_sync(tmp_path, monkeypatch):
    skills_dir, _manifest, target = _configure_sync_target(tmp_path, monkeypatch)
    manager.sync_to_targets()
    conflicting = target / "listed.md"
    conflicting.write_text("user changed active skill", encoding="utf-8")
    (skills_dir / "listed.md").write_text("upstream v2", encoding="utf-8")

    result = manager.sync_to_targets()[str(target)]

    assert result["copied"] == []
    assert result["preserved"] == ["listed.md"]
    assert conflicting.read_text(encoding="utf-8") == "user changed active skill"
    state = json.loads((target / manager.SYNC_STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["files"] == {}


def test_sync_updates_unchanged_managed_active_file(tmp_path, monkeypatch):
    skills_dir, _manifest, target = _configure_sync_target(tmp_path, monkeypatch)
    manager.sync_to_targets()
    (skills_dir / "listed.md").write_text("upstream v2", encoding="utf-8")

    result = manager.sync_to_targets()[str(target)]

    assert result["copied"] == ["listed.md"]
    assert (target / "listed.md").read_text(encoding="utf-8") == "upstream v2"
