"""Trusted project registry and immutable production security context."""

from __future__ import annotations

import os
import json
import hashlib
from threading import Lock
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping


class WriteVisibility(str, Enum):
    PUBLIC = "public"
    SHARED = "shared"
    AGENT_PRIVATE = "agent-private"
    SESSION_PRIVATE = "session-private"


class ReadScope(str, Enum):
    PUBLIC = "public"
    SHARED = "shared"
    AGENT = "agent"
    SESSION = "session"


class ValidationStatus(str, Enum):
    UNVERIFIED = "unverified"
    VALIDATED = "validated"
    AUTHORITATIVE = "authoritative"
    DEPRECATED = "deprecated"
    INVALIDATED = "invalidated"


def _required(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 512 or "\x00" in normalized:
        raise ValueError(f"{field_name} is required and must be valid")
    return normalized


def _canonical_root(value: str | Path, field_name: str) -> Path:
    raw = Path(_required(str(value), field_name)).expanduser()
    try:
        resolved = raw.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise ValueError(f"{field_name} must be an existing directory") from exc
    if not resolved.is_dir():
        raise ValueError(f"{field_name} must be an existing directory")
    return resolved


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class ProjectDefinition:
    project_id: str
    project_name: str
    roots: Mapping[str, Path]

    def __post_init__(self) -> None:
        project_id = _required(self.project_id, "project_id")
        project_name = _required(self.project_name, "project_name")
        normalized = {
            _required(root_id, "root_id"): _canonical_root(path, "project_root")
            for root_id, path in self.roots.items()
        }
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "project_name", project_name)
        object.__setattr__(self, "roots", normalized)


class ProjectRegistry:
    REGISTRY_VERSION = 1

    def roots_for(self, project_id: str) -> tuple[Path, ...]:
        """Return only this registered project's roots for provenance checks."""
        project = self._projects.get(project_id)
        if project is None:
            raise ValueError("Unknown project_id")
        return tuple(project.roots.values())

    def __init__(self, projects: Iterable[ProjectDefinition], *, version: int = 1) -> None:
        if int(version) != self.REGISTRY_VERSION:
            raise ValueError("Unsupported projects registry version")
        self.version = int(version)
        project_list = list(projects)
        self._projects = {project.project_id: project for project in project_list}
        if len(self._projects) != len(project_list):
            raise ValueError("Duplicate project_id in projects registry")
        if not self._projects:
            raise ValueError("Project registry must not be empty")
        self.fingerprint = self._compute_fingerprint()

    def _canonical_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "projects": [
                {
                    "project_id": project.project_id,
                    "project_name": project.project_name,
                    "roots": {
                        root_id: str(project.roots[root_id])
                        for root_id in sorted(project.roots)
                    },
                }
                for project in sorted(self._projects.values(), key=lambda item: item.project_id)
            ],
        }

    def _compute_fingerprint(self) -> str:
        encoded = json.dumps(
            self._canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded, usedforsecurity=False).hexdigest()

    @classmethod
    def from_file(cls, path: str | Path) -> "ProjectRegistry":
        registry_path = Path(path).expanduser()
        try:
            payload = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Projects registry cannot be read or parsed") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("projects"), list):
            raise ValueError("Projects registry has an invalid structure")
        projects = []
        for entry in payload["projects"]:
            if not isinstance(entry, dict):
                raise ValueError("Projects registry entry has an invalid structure")
            roots = entry.get("roots", {})
            if not isinstance(roots, dict):
                raise ValueError("Projects registry entry has an invalid structure")
            projects.append(
                ProjectDefinition(
                    entry.get("project_id", ""),
                    entry.get("project_name", ""),
                    roots,
                )
            )
        registry = cls(projects, version=payload.get("version", 0))
        declared = payload.get("fingerprint")
        if declared is not None and declared != registry.fingerprint:
            raise ValueError("Projects registry fingerprint mismatch")
        return registry

    @classmethod
    def from_settings(cls, settings: Any) -> "ProjectRegistry":
        registry_file = Path(settings.projects_file).expanduser()
        if registry_file.exists():
            return cls.from_file(registry_file)
        project_root = _canonical_root(settings.project_root, "built_in_project_root")
        project_id = os.getenv("PROJECT_ID", project_root.name)
        project_name = os.getenv("PROJECT_NAME", project_root.name)
        roots: dict[str, Path] = {os.getenv("ROOT_ID", "primary"): project_root}
        for index, candidate in enumerate(settings.allowed_index_roots, start=1):
            resolved = _canonical_root(candidate, "allowed_index_root")
            if resolved not in roots.values():
                roots[f"root-{index}"] = resolved
        return cls([ProjectDefinition(project_id, project_name, roots)])

    def resolve(
        self,
        *,
        project_id: str,
        root_id: str,
        write_visibility: str,
        read_scope: str,
        agent_id: str,
        session_id: str,
        source_types: Iterable[str] = (),
        language: str = "en",
        validation_status: str = "unverified",
        min_score: float = 0.0,
        include_metadata: bool = True,
        project_root_hint: str | None = None,
        allowed_roots_hint: Iterable[str] | None = None,
        registry_fingerprint_hint: str | None = None,
    ) -> "ProjectContext":
        if registry_fingerprint_hint is not None and registry_fingerprint_hint != self.fingerprint:
            raise ValueError("Projects registry fingerprint mismatch")
        project = self._projects.get(project_id)
        if project is None:
            raise ValueError("Unknown project_id")
        active_root = project.roots.get(root_id)
        if active_root is None:
            raise ValueError("Unknown root_id")
        if project_root_hint:
            hint = _canonical_root(project_root_hint, "project_root_hint")
            if hint != active_root:
                raise ValueError("Client project_root cannot replace the registered active root")
        if allowed_roots_hint is not None:
            for hint_value in allowed_roots_hint:
                hint = _canonical_root(hint_value, "allowed_root_hint")
                if not _is_within(hint, active_root):
                    raise ValueError("Client allowed_roots cannot expand the registered active root")
        return ProjectContext(
            project_id=project.project_id,
            project_name=project.project_name,
            root_id=root_id,
            project_root=str(active_root),
            allowed_roots=tuple(str(path) for path in project.roots.values()),
            write_visibility=WriteVisibility(write_visibility),
            read_scope=ReadScope(read_scope),
            agent_id=agent_id,
            session_id=session_id,
            source_types=tuple(source_types),
            language=language,
            validation_status=ValidationStatus(validation_status),
            min_score=min_score,
            include_metadata=include_metadata,
            registry_fingerprint=self.fingerprint,
        )

    def resolve_knowledge(
        self,
        *,
        project_id: str,
        read_scope: str,
        agent_id: str,
        session_id: str,
        source_types: Iterable[str] = (),
        language: str = "en",
        min_score: float = 0.0,
        include_metadata: bool = True,
        registry_fingerprint_hint: str | None = None,
    ) -> "KnowledgeContext":
        """Resolve a memory/search scope without requiring a filesystem root."""
        if registry_fingerprint_hint is not None and registry_fingerprint_hint != self.fingerprint:
            raise ValueError("Projects registry fingerprint mismatch")
        project = self._projects.get(project_id)
        if project is None:
            raise ValueError("Unknown project_id")
        return KnowledgeContext(
            project_id=project.project_id,
            project_name=project.project_name,
            read_scope=ReadScope(read_scope),
            agent_id=agent_id,
            session_id=session_id,
            source_types=tuple(source_types),
            language=language,
            min_score=min_score,
            include_metadata=include_metadata,
            registry_fingerprint=self.fingerprint,
        )


@dataclass(frozen=True, slots=True)
class ProjectContext:
    project_id: str
    project_name: str
    root_id: str
    project_root: str
    allowed_roots: tuple[str, ...]
    write_visibility: WriteVisibility
    read_scope: ReadScope
    agent_id: str
    session_id: str
    source_types: tuple[str, ...] = field(default_factory=tuple)
    language: str = "en"
    validation_status: ValidationStatus = ValidationStatus.UNVERIFIED
    min_score: float = 0.0
    include_metadata: bool = True
    registry_fingerprint: str = ""

    def __post_init__(self) -> None:
        for name in ("project_id", "project_name", "root_id", "agent_id", "session_id"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        try:
            active_root = _canonical_root(self.project_root, "project_root")
        except ValueError as exc:
            raise ValueError("Active project_root must be an existing registered root") from exc
        registered_roots = tuple(
            str(_canonical_root(root, "allowed_root")) for root in self.allowed_roots
        )
        if str(active_root) not in registered_roots:
            raise ValueError("Active project_root must be a registered root")
        object.__setattr__(self, "project_root", str(active_root))
        object.__setattr__(self, "allowed_roots", registered_roots)
        object.__setattr__(self, "write_visibility", WriteVisibility(self.write_visibility))
        object.__setattr__(self, "read_scope", ReadScope(self.read_scope))
        object.__setattr__(self, "validation_status", ValidationStatus(self.validation_status))
        object.__setattr__(self, "source_types", tuple(sorted(str(item) for item in self.source_types)))
        object.__setattr__(self, "language", str(self.language).strip().lower() or "en")
        object.__setattr__(self, "min_score", float(self.min_score))
        if not -1.0 <= self.min_score <= 1.0:
            raise ValueError("min_score must be between -1 and 1")
        if self.registry_fingerprint:
            object.__setattr__(
                self, "registry_fingerprint", _required(self.registry_fingerprint, "registry_fingerprint")
            )

    def validate_path(self, value: str | Path, *, must_exist: bool = False) -> Path:
        raw = str(value)
        if ".." in raw.replace("\\", "/").split("/"):
            raise ValueError("Path traversal is forbidden")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = Path(self.project_root) / candidate
        try:
            if must_exist or candidate.exists() or candidate.is_symlink():
                candidate = candidate.resolve(strict=True)
            else:
                parent = candidate.parent.resolve(strict=True)
                if not parent.is_dir():
                    raise ValueError("Path parent must be an existing directory")
                candidate = parent / candidate.name
        except (FileNotFoundError, RuntimeError) as exc:
            raise ValueError("Path or its parent does not exist") from exc
        active_root = Path(self.project_root)
        if not _is_within(candidate, active_root):
            raise ValueError("Path is outside the active registered root")
        return candidate

    @property
    def cache_key(self) -> tuple[Any, ...]:
        return (
            self.project_id,
            self.project_name,
            self.root_id,
            self.project_root,
            tuple(self.allowed_roots),
            self.read_scope.value,
            self.write_visibility.value,
            self.agent_id,
            self.session_id,
            tuple(self.source_types),
            self.language,
            self.validation_status.value,
            self.min_score,
            self.include_metadata,
            self.registry_fingerprint,
        )

    def search_cache_key(
        self,
        query: str,
        *,
        limit: int,
        score_threshold: float | None = None,
        search_mode: str = "search",
        parameters: Iterable[Any] = (),
    ) -> tuple[Any, ...]:
        return (
            *self.cache_key,
            str(query).strip(),
            int(limit),
            self.min_score if score_threshold is None else float(score_threshold),
            str(search_mode),
            tuple(parameters),
        )

    def to_request_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "root_id": self.root_id,
            "write_visibility": self.write_visibility.value,
            "read_scope": self.read_scope.value,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "source_types": list(self.source_types),
            "language": self.language,
            "validation_status": self.validation_status.value,
            "min_score": self.min_score,
            "include_metadata": self.include_metadata,
            "registry_fingerprint": self.registry_fingerprint,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_request_dict(),
            "project_name": self.project_name,
            "project_root": self.project_root,
            "allowed_roots": list(self.allowed_roots),
        }


@dataclass(frozen=True, slots=True)
class KnowledgeContext:
    """Validated project namespace and ACL for retrieval, with no filesystem root."""

    project_id: str
    project_name: str
    read_scope: ReadScope
    agent_id: str
    session_id: str
    source_types: tuple[str, ...] = field(default_factory=tuple)
    language: str = "en"
    min_score: float = 0.0
    include_metadata: bool = True
    registry_fingerprint: str = ""

    def __post_init__(self) -> None:
        for name in ("project_id", "project_name", "agent_id", "session_id"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "read_scope", ReadScope(self.read_scope))
        object.__setattr__(self, "source_types", tuple(sorted(str(item) for item in self.source_types)))
        object.__setattr__(self, "language", str(self.language).strip().lower() or "en")
        object.__setattr__(self, "min_score", float(self.min_score))
        if not -1.0 <= self.min_score <= 1.0:
            raise ValueError("min_score must be between -1 and 1")
        if self.registry_fingerprint:
            object.__setattr__(
                self, "registry_fingerprint", _required(self.registry_fingerprint, "registry_fingerprint")
            )

    def to_request_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "read_scope": self.read_scope.value,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "source_types": list(self.source_types),
            "language": self.language,
            "min_score": self.min_score,
            "include_metadata": self.include_metadata,
            "registry_fingerprint": self.registry_fingerprint,
        }

    def search_cache_key(
        self,
        query: str,
        *,
        limit: int,
        score_threshold: float | None = None,
        search_mode: str = "search",
        parameters: Iterable[Any] = (),
    ) -> tuple[Any, ...]:
        identifies_agent = self.read_scope in (ReadScope.AGENT, ReadScope.SESSION)
        identifies_session = self.read_scope is ReadScope.SESSION
        return (
            self.project_id,
            self.read_scope.value,
            self.agent_id if identifies_agent else "",
            self.session_id if identifies_session else "",
            tuple(self.source_types),
            self.min_score if score_threshold is None else float(score_threshold),
            self.include_metadata,
            self.registry_fingerprint,
            str(query).strip(),
            int(limit),
            str(search_mode),
            tuple(parameters),
        )


def build_default_project_context(
    settings: Any,
    *,
    agent_id: str,
    session_id: str,
    write_visibility: str = "session-private",
    read_scope: str = "session",
    root_id: str | None = None,
    source_types: Iterable[str] = (),
    language: str = "en",
    validation_status: str = "unverified",
    min_score: float = 0.0,
    include_metadata: bool = True,
) -> ProjectContext:
    registry = get_project_registry(settings)
    project_root = _canonical_root(settings.project_root, "built_in_project_root")
    return registry.resolve(
        project_id=os.getenv("PROJECT_ID", project_root.name),
        root_id=root_id or os.getenv("ROOT_ID", "primary"),
        write_visibility=write_visibility,
        read_scope=read_scope,
        agent_id=agent_id,
        session_id=session_id,
        source_types=source_types,
        language=language,
        validation_status=validation_status,
        min_score=min_score,
        include_metadata=include_metadata,
    )


_registry_lock = Lock()
_registry_instances: dict[str, tuple[int, ProjectRegistry]] = {}


def get_project_registry(settings: Any) -> ProjectRegistry:
    """Return the process-wide registry instance for a configured registry path."""
    registry_path = Path(settings.projects_file).expanduser().absolute()
    key = str(registry_path)
    try:
        current_mtime = registry_path.stat().st_mtime_ns if registry_path.exists() else -1
    except OSError:
        current_mtime = -1

    with _registry_lock:
        cached = _registry_instances.get(key)
        if cached is None or cached[0] != current_mtime:
            registry = ProjectRegistry.from_settings(settings)
            _registry_instances[key] = (current_mtime, registry)
            return registry
        return cached[1]



def configured_context(
    *,
    project_id: str,
    project_name: str,
    root_id: str,
    project_root: str | Path,
    allowed_roots: Iterable[str | Path],
    write_visibility: str,
    read_scope: str,
    agent_id: str,
    session_id: str,
    source_types: Iterable[str] = (),
    language: str = "en",
    validation_status: str = "unverified",
    min_score: float = 0.0,
    include_metadata: bool = True,
) -> ProjectContext:
    roots = {root_id: Path(project_root)}
    for index, root in enumerate(allowed_roots, start=1):
        resolved = _canonical_root(root, "allowed_root")
        if resolved not in [_canonical_root(path, "project_root") for path in roots.values()]:
            roots[f"root-{index}"] = resolved
    registry = ProjectRegistry([ProjectDefinition(project_id, project_name, roots)])
    return registry.resolve(
        project_id=project_id,
        root_id=root_id,
        write_visibility=write_visibility,
        read_scope=read_scope,
        agent_id=agent_id,
        session_id=session_id,
        source_types=source_types,
        language=language,
        validation_status=validation_status,
        min_score=min_score,
        include_metadata=include_metadata,
    )
