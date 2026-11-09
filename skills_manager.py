#!/usr/bin/env python3
"""Strict bounded Skills catalog and per-session cache."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from collections import OrderedDict
from pathlib import Path
from threading import RLock
from typing import Any

from config import settings

PRIMARY_SKILLS_DIR = settings.skills_dir.resolve()
MANIFEST_FILE = settings.manifest_file.resolve()
SECONDARY_SKILLS_DIRS = [
    Path.home() / ".gemini" / "antigravity" / "skills",
    Path.home() / ".gemini" / "skills",
    Path.home() / ".gemini" / "antigravity-cli" / "skills",
    Path.home() / ".cursor" / "skills",
]

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MANIFEST_DEPTH = 8
MAX_SKILLS = 256
MAX_ALIASES_PER_SKILL = 64
MAX_TOTAL_ALIASES = 1024
MAX_SKILLS_PER_SESSION = 10
MAX_SKILL_BYTES = 256 * 1024
MAX_SESSION_BYTES = 1024 * 1024
MAX_SESSIONS = 100
SESSION_TTL_SECONDS = 3600


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > MAX_MANIFEST_DEPTH:
        return depth
    if isinstance(value, dict):
        return max([depth, *(_json_depth(item, depth + 1) for item in value.values())])
    if isinstance(value, list):
        return max([depth, *(_json_depth(item, depth + 1) for item in value)])
    return depth


def load_manifest(strict: bool = True) -> dict[str, Any]:
    """Load the production catalog; lenient mode is diagnostic-only."""
    if not MANIFEST_FILE.is_file():
        if strict:
            raise FileNotFoundError(f"Skills manifest not found: {MANIFEST_FILE.name}")
        return {"version": "diagnostic", "skills": [], "warnings": ["manifest missing"]}
    if MANIFEST_FILE.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Skills manifest exceeds maximum size")
    try:
        data = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        if strict:
            raise ValueError("Invalid Skills manifest JSON") from exc
        return {"version": "diagnostic", "skills": [], "warnings": ["invalid JSON"]}
    if _json_depth(data) > MAX_MANIFEST_DEPTH:
        raise ValueError("Skills manifest exceeds maximum nesting depth")
    if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
        raise ValueError("Skills manifest must contain a skills list")
    if len(data["skills"]) > MAX_SKILLS:
        raise ValueError("Skills manifest exceeds maximum skill count")

    namespace: dict[str, str] = {}
    total_aliases = 0
    conflicts: dict[str, set[str]] = {}
    for item in data["skills"]:
        if not isinstance(item, dict):
            raise ValueError("Skill metadata must be an object")
        skill_id = item.get("id")
        filename = item.get("filename")
        aliases = item.get("aliases", [])
        if not isinstance(skill_id, str) or not skill_id or len(skill_id) > 100:
            raise ValueError("Skill id is invalid")
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".md"):
            raise ValueError(f"Unsafe skill filename for {skill_id}")
        if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
            raise ValueError(f"Aliases must be strings for {skill_id}")
        if len(aliases) > MAX_ALIASES_PER_SKILL:
            raise ValueError(f"Alias limit exceeded for {skill_id}")
        total_aliases += len(aliases)
        target = (PRIMARY_SKILLS_DIR / filename).resolve(strict=False)
        try:
            target.relative_to(PRIMARY_SKILLS_DIR)
        except ValueError as exc:
            raise ValueError(f"Skill path escapes catalog: {skill_id}") from exc
        if item.get("required", False) and (not target.is_file() or target.is_symlink()):
            raise FileNotFoundError(f"Required skill file missing or unsafe: {skill_id}")

        names = [skill_id, Path(filename).stem, *aliases]
        for name in names:
            if not name or len(name) > 100 or any(part in name for part in ("/", "\\", "..")):
                raise ValueError(f"Unsafe skill name/alias for {skill_id}")
            previous = namespace.get(name)
            if previous is not None and previous != skill_id:
                conflicts.setdefault(name, {previous}).add(skill_id)
            namespace[name] = skill_id
    if total_aliases > MAX_TOTAL_ALIASES:
        raise ValueError("Skills manifest exceeds total alias limit")
    if conflicts:
        details = "; ".join(
            f"{alias}: {', '.join(sorted(ids))}" for alias, ids in sorted(conflicts.items())
        )
        raise ValueError(f"Skills alias collisions: {details}")
    return data


def _catalog_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in load_manifest(strict=True)["skills"]:
        for name in (item["id"], Path(item["filename"]).stem, *item.get("aliases", [])):
            index[name] = item
    return index


def resolve_skill_alias(skill_name_or_alias: str) -> str:
    name = str(skill_name_or_alias).strip()
    if name.endswith(".md"):
        name = name[:-3]
    item = _catalog_index().get(name)
    if item is None:
        raise KeyError(f"Skill is not listed in the strict manifest: {name}")
    return item["filename"]


def safe_skills_path(rel_or_filename: str) -> Path:
    filename = resolve_skill_alias(rel_or_filename)
    target = (PRIMARY_SKILLS_DIR / filename).resolve(strict=True)
    try:
        target.relative_to(PRIMARY_SKILLS_DIR)
    except ValueError as exc:
        raise ValueError("Skill path traversal detected") from exc
    if target.is_symlink() or not target.is_file():
        raise FileNotFoundError(f"Skill file is not a safe regular file: {filename}")
    return target


def get_file_hash(filepath: Path) -> str:
    return hashlib.sha256(filepath.read_bytes(), usedforsecurity=False).hexdigest()


def get_core_skills() -> list[str]:
    return [item["filename"] for item in load_manifest(strict=True)["skills"] if item.get("required")]


def get_conditional_skills(query: str = "") -> list[str]:
    if not query:
        return []
    lowered = query.lower()
    return [
        item["filename"]
        for item in load_manifest(strict=True)["skills"]
        if any(str(keyword).lower() in lowered for keyword in item.get("activation_keywords", []))
    ]


class SessionSkillsManager:
    def __init__(
        self,
        ttl_seconds: int = SESSION_TTL_SECONDS,
        max_sessions: int = MAX_SESSIONS,
        clock=time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or not 1 <= max_sessions <= MAX_SESSIONS:
            raise ValueError("Invalid session cache limits")
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._clock = clock
        self._sessions: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._lock = RLock()

    @staticmethod
    def _session_key(agent_id: str, session_id: str) -> tuple[str, str]:
        agent = str(agent_id or "").strip()
        session = str(session_id or "").strip()
        if not agent or not session or len(agent) > 256 or len(session) > 256:
            raise ValueError("agent_id and session_id are required and bounded")
        return agent, session

    def _cleanup(self, now: float) -> None:
        for key in list(self._sessions):
            if now - self._sessions[key]["last_accessed"] > self.ttl_seconds:
                del self._sessions[key]

    def load_skill_for_session(self, agent_id: str, session_id: str, skill_alias: str) -> str:
        key = self._session_key(agent_id, session_id)
        path = safe_skills_path(skill_alias)
        size = path.stat().st_size
        if size > MAX_SKILL_BYTES:
            raise ValueError(f"Skill exceeds per-file size limit: {path.name}")
        content = path.read_text(encoding="utf-8")
        encoded_size = len(content.encode("utf-8"))
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            if key not in self._sessions:
                while len(self._sessions) >= self.max_sessions:
                    self._sessions.popitem(last=False)
                self._sessions[key] = {"skills": OrderedDict(), "bytes": 0, "last_accessed": now}
            session = self._sessions[key]
            existing = session["skills"].get(path.name)
            next_bytes = session["bytes"] - (existing["bytes"] if existing else 0) + encoded_size
            next_count = len(session["skills"]) + (0 if existing else 1)
            if next_count > MAX_SKILLS_PER_SESSION:
                raise ValueError("Session skill count limit exceeded")
            if next_bytes > MAX_SESSION_BYTES:
                raise ValueError("Session skill content limit exceeded")
            session["skills"][path.name] = {
                "content": content,
                "bytes": encoded_size,
                "loaded_at": now,
            }
            session["bytes"] = next_bytes
            session["last_accessed"] = now
            self._sessions.move_to_end(key)
        return content

    def get_session_skills(self, agent_id: str, session_id: str) -> dict[str, str]:
        key = self._session_key(agent_id, session_id)
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            session = self._sessions.get(key)
            if session is None:
                return {}
            session["last_accessed"] = now
            self._sessions.move_to_end(key)
            return {name: value["content"] for name, value in session["skills"].items()}


session_skills_manager = SessionSkillsManager()


def inventory_skills() -> dict[str, Any]:
    manifest = load_manifest(strict=True)
    result = {}
    for item in manifest["skills"]:
        path = safe_skills_path(item["id"])
        result[path.name] = {
            "name": item["id"],
            "filename": path.name,
            "path": str(path),
            "size": path.stat().st_size,
            "checksum": get_file_hash(path),
            "version": item.get("version", "1.0.0"),
            "required": item.get("required", False),
            "aliases": list(item.get("aliases", [])),
            "status": "active",
        }
    return result


def check_discrepancies() -> dict[str, Any]:
    primary = inventory_skills()
    result = {}
    for target_dir in SECONDARY_SKILLS_DIRS:
        target = {path.name: path for path in target_dir.glob("*.md")} if target_dir.exists() else {}
        result[str(target_dir)] = {
            "missing_in_target": sorted(set(primary) - set(target)),
            "extra_in_target": sorted(set(target) - set(primary)),
            "mismatched_hash": sorted(
                name for name in set(primary) & set(target)
                if primary[name]["checksum"] != get_file_hash(target[name])
            ),
        }
    return result


def sync_to_targets(dry_run: bool = False, clean_stale: bool = True) -> dict[str, Any]:
    primary = inventory_skills()
    result = {}
    for target_dir in SECONDARY_SKILLS_DIRS:
        details = {"copied": [], "skipped": [], "removed": [], "errors": []}
        try:
            if not dry_run:
                target_dir.mkdir(parents=True, exist_ok=True)
            for filename, metadata in primary.items():
                destination = target_dir / filename
                if destination.exists() and get_file_hash(destination) == metadata["checksum"]:
                    details["skipped"].append(filename)
                    continue
                if not dry_run:
                    shutil.copy2(metadata["path"], destination)
                details["copied"].append(filename)
            if clean_stale and target_dir.exists():
                for existing_file in target_dir.glob("*.md"):
                    if existing_file.name not in primary and not existing_file.is_symlink():
                        if not dry_run:
                            existing_file.unlink()
                        details["removed"].append(existing_file.name)
        except Exception as exc:
            details["errors"].append(str(exc))
        result[str(target_dir)] = details
    return result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--diagnostic-lenient"]:
        print(json.dumps(load_manifest(strict=False), indent=2, ensure_ascii=False))
        return 0
    if argv == ["--discrepancies"]:
        print(json.dumps(check_discrepancies(), indent=2, ensure_ascii=False))
        return 0
    if argv == ["--sync"]:
        print(json.dumps(sync_to_targets(), indent=2, ensure_ascii=False))
        return 0
    print(json.dumps(inventory_skills(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
