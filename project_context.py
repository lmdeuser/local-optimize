"""Trusted source registry and immutable request security contexts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from threading import Lock
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
    raw_text = _required(str(value), field_name)
    if ".." in raw_text.replace("\\", "/").split("/"):
        raise ValueError("Path traversal is forbidden")
    raw = Path(raw_text).expanduser()
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{field_name} must be an existing directory") from exc
    if not resolved.is_dir():
        raise ValueError(f"{field_name} must be an existing directory")
    return resolved


def _canonical_path(value: str | Path, field_name: str, *, must_exist: bool = False) -> Path:
    raw = str(value or "")
    if ".." in raw.replace("\\", "/").split("/"):
        raise ValueError("Path traversal is forbidden")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{field_name} must be absolute")
    try:
        if must_exist or candidate.exists() or candidate.is_symlink():
            return candidate.resolve(strict=True)
        ancestor = candidate.parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        parent = ancestor.resolve(strict=True)
        if not parent.is_dir():
            raise ValueError("Path parent must be an existing directory")
        return parent.joinpath(*candidate.parts[len(parent.parts):])
    except (OSError, RuntimeError) as exc:
        raise ValueError("Path or its parent does not exist") from exc


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def workspace_definition(value: str | Path) -> "ProjectDefinition":
    """Derive a stable workspace identity without granting system directories."""
    raw = Path(value).expanduser()
    if not raw.is_absolute() or ".." in raw.parts:
        raise ValueError("Workspace must be an absolute path without traversal")
    root = _canonical_root(raw, "working_directory")
    if raw != root:
        raise ValueError("Workspace path must not contain symbolic links")
    forbidden = tuple(Path(path) for path in ("/etc", "/proc", "/sys", "/root", "/dev", "/boot", "/usr", "/var", "/run"))
    if root == Path("/") or any(_is_within(root, path) for path in forbidden):
        raise ValueError("System directories cannot be workspace roots")
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return ProjectDefinition("ws:" + digest, root.name or "workspace", {"workspace": root})


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded, usedforsecurity=False).hexdigest()


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


@dataclass(frozen=True, slots=True)
class RootOwner:
    project_id: str
    project_name: str
    root_id: str
    project_root: str


class ProjectRegistry:
    REGISTRY_VERSION = 1

    def __init__(self, projects: Iterable[ProjectDefinition], *, version: int = 1) -> None:
        if int(version) != self.REGISTRY_VERSION:
            raise ValueError("Unsupported projects registry version")
        self.version = int(version)
        project_list = list(projects)
        self._projects = {project.project_id: project for project in project_list}
        if len(self._projects) != len(project_list):
            raise ValueError("Duplicate project_id in projects registry")
        owners: dict[Path, tuple[str, str]] = {}
        for project in project_list:
            for root_id, path in project.roots.items():
                owner = (project.project_id, root_id)
                if path in owners and owners[path] != owner:
                    raise ValueError("Ambiguous duplicate registered root")
                owners[path] = owner
        self.fingerprint = self._compute_fingerprint()

    @property
    def project_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._projects))

    @property
    def projects(self) -> tuple[ProjectDefinition, ...]:
        return tuple(self._projects[project_id] for project_id in self.project_ids)

    def roots_for(self, project_id: str) -> tuple[Path, ...]:
        project = self._projects.get(project_id)
        if project is None:
            raise ValueError("Unknown project_id")
        return tuple(project.roots.values())

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
                for project in self.projects
            ],
        }

    def _compute_fingerprint(self) -> str:
        return _fingerprint(self._canonical_payload())

    def _nested_exclusions(self, root: Path) -> list[dict[str, str]]:
        excluded = []
        for project in self.projects:
            for root_id, candidate in project.roots.items():
                if candidate != root and _is_within(candidate, root):
                    excluded.append({
                        "project_id": project.project_id,
                        "root_id": root_id,
                        "path": str(candidate),
                    })
        return sorted(excluded, key=lambda item: (item["path"], item["project_id"], item["root_id"]))

    def root_fingerprint(self, project_id: str, root_id: str) -> str:
        project = self._projects.get(project_id)
        if project is None:
            raise ValueError("Unknown project_id")
        root = project.roots.get(root_id)
        if root is None:
            raise ValueError("Unknown root_id")
        return _fingerprint({
            "scope": "root",
            "project_id": project.project_id,
            "project_name": project.project_name,
            "root_id": root_id,
            "project_root": str(root),
            "excluded_roots": self._nested_exclusions(root),
        })

    def project_fingerprint(self, project_id: str) -> str:
        project = self._projects.get(project_id)
        if project is None:
            raise ValueError("Unknown project_id")
        roots = []
        for root_id, root in sorted(project.roots.items()):
            roots.append({
                "root_id": root_id,
                "project_root": str(root),
                "excluded_roots": self._nested_exclusions(root),
            })
        return _fingerprint({
            "scope": "knowledge",
            "project_id": project.project_id,
            "project_name": project.project_name,
            "roots": roots,
        })

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
            project_id = str(entry.get("project_id", ""))
            if project_id == "__global_knowledge__" or project_id.startswith("ws:"):
                raise ValueError("Projects registry uses a reserved project identifier")
            projects.append(ProjectDefinition(
                entry.get("project_id", ""), entry.get("project_name", ""), roots,
            ))
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
        return cls([])

    def owner_for_path(self, value: str | Path, *, must_exist: bool = False) -> RootOwner:
        candidate = _canonical_path(value, "registered_path", must_exist=must_exist)
        matches = []
        for project in self.projects:
            for root_id, root in project.roots.items():
                if _is_within(candidate, root):
                    matches.append((len(root.parts), project, root_id, root))
        if not matches:
            raise ValueError("Path is outside all registered roots")
        deepest = max(item[0] for item in matches)
        selected = [item for item in matches if item[0] == deepest]
        if len(selected) != 1:
            raise ValueError("Ambiguous registered root ownership")
        _, project, root_id, root = selected[0]
        return RootOwner(project.project_id, project.project_name, root_id, str(root))

    def is_owned_by(self, value: str | Path, *, project_id: str, root_id: str) -> bool:
        try:
            owner = self.owner_for_path(value)
        except (OSError, ValueError):
            return False
        return (owner.project_id, owner.root_id) == (project_id, root_id)

    def owns_context_path(self, context: "ProjectContext", value: str | Path, *, must_exist: bool = False) -> bool:
        try:
            path = context.validate_path(value, must_exist=must_exist)
            try:
                owner = self.owner_for_path(path, must_exist=must_exist)
            except ValueError as exc:
                if str(exc) != "Path is outside all registered roots" or not context.ad_hoc:
                    return False
                workspace = workspace_definition(context.project_root)
                return (workspace.project_id, "workspace") == (context.project_id, context.root_id)
            return (owner.project_id, owner.root_id) == (context.project_id, context.root_id)
        except (OSError, RuntimeError, ValueError):
            return False

    def _validate_fingerprint(
        self,
        actual: str,
        *,
        context_fingerprint_hint: str | None,
        registry_fingerprint_hint: str | None,
    ) -> None:
        if context_fingerprint_hint is not None and context_fingerprint_hint != actual:
            raise ValueError("Context fingerprint mismatch")
        if context_fingerprint_hint is None and registry_fingerprint_hint is not None:
            if registry_fingerprint_hint != self.fingerprint:
                raise ValueError("Projects registry fingerprint mismatch")

    def resolve(
        self,
        *,
        project_id: str | None = None,
        root_id: str | None = None,
        working_directory: str | None = None,
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
        context_fingerprint_hint: str | None = None,
    ) -> "ProjectContext":
        if working_directory is not None:
            if project_id or root_id:
                raise ValueError("working_directory cannot be combined with project_id/root_id")
            requested = workspace_definition(working_directory).roots["workspace"]
            try:
                owner = self.owner_for_path(requested)
            except ValueError as exc:
                if str(exc) != "Path is outside all registered roots":
                    raise
                workspace = workspace_definition(working_directory)
                if workspace.project_id in self._projects:
                    raise ValueError("Workspace identity conflicts with registered project")
                temporary = ProjectRegistry([*self.projects, workspace])
                resolved = temporary.resolve(
                    project_id=workspace.project_id, root_id="workspace",
                    write_visibility=write_visibility, read_scope=read_scope,
                    agent_id=agent_id, session_id=session_id, source_types=source_types,
                    language=language, validation_status=validation_status,
                    min_score=min_score, include_metadata=include_metadata,
                    project_root_hint=project_root_hint, allowed_roots_hint=allowed_roots_hint,
                    registry_fingerprint_hint=registry_fingerprint_hint,
                    context_fingerprint_hint=context_fingerprint_hint,
                )
                return replace(resolved, ad_hoc=True)
            project_id, root_id = owner.project_id, owner.root_id
        project = self._projects.get(str(project_id or ""))
        if project is None:
            raise ValueError("Unknown project_id")
        active_root = project.roots.get(str(root_id or ""))
        if active_root is None:
            raise ValueError("Unknown root_id")
        scoped_fingerprint = self.root_fingerprint(project.project_id, str(root_id))
        self._validate_fingerprint(
            scoped_fingerprint,
            context_fingerprint_hint=context_fingerprint_hint,
            registry_fingerprint_hint=registry_fingerprint_hint,
        )
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
            root_id=str(root_id),
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
            context_fingerprint=scoped_fingerprint,
        )

    def resolve_knowledge(
        self,
        *,
        project_id: str | None = None,
        working_directory: str | None = None,
        read_scope: str,
        agent_id: str,
        session_id: str,
        source_types: Iterable[str] = (),
        language: str = "en",
        min_score: float = 0.0,
        include_metadata: bool = True,
        registry_fingerprint_hint: str | None = None,
        context_fingerprint_hint: str | None = None,
    ) -> "KnowledgeContext":
        if working_directory is not None:
            if project_id:
                raise ValueError("working_directory cannot be combined with project_id")
            requested = workspace_definition(working_directory).roots["workspace"]
            try:
                project_id = self.owner_for_path(requested).project_id
            except ValueError as exc:
                if str(exc) != "Path is outside all registered roots":
                    raise
                workspace = workspace_definition(working_directory)
                if workspace.project_id in self._projects:
                    raise ValueError("Workspace identity conflicts with registered project")
                return ProjectRegistry([*self.projects, workspace]).resolve_knowledge(
                    project_id=workspace.project_id, read_scope=read_scope,
                    agent_id=agent_id, session_id=session_id, source_types=source_types,
                    language=language, min_score=min_score, include_metadata=include_metadata,
                    registry_fingerprint_hint=registry_fingerprint_hint,
                    context_fingerprint_hint=context_fingerprint_hint,
                )
        project = self._projects.get(str(project_id or ""))
        if project is None:
            raise ValueError("Unknown project_id")
        scoped_fingerprint = self.project_fingerprint(project.project_id)
        self._validate_fingerprint(
            scoped_fingerprint,
            context_fingerprint_hint=context_fingerprint_hint,
            registry_fingerprint_hint=registry_fingerprint_hint,
        )
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
            context_fingerprint=scoped_fingerprint,
        )


def _common_request_fields(context: Any) -> dict[str, Any]:
    return {
        "read_scope": context.read_scope.value,
        "agent_id": context.agent_id,
        "session_id": context.session_id,
        "source_types": list(context.source_types),
        "language": context.language,
        "min_score": context.min_score,
        "include_metadata": context.include_metadata,
    }


def _normalize_common(context: Any) -> None:
    for name in ("agent_id", "session_id"):
        object.__setattr__(context, name, _required(getattr(context, name), name))
    object.__setattr__(context, "read_scope", ReadScope(context.read_scope))
    object.__setattr__(context, "source_types", tuple(sorted(str(item) for item in context.source_types)))
    object.__setattr__(context, "language", str(context.language).strip().lower() or "en")
    object.__setattr__(context, "min_score", float(context.min_score))
    if not -1.0 <= context.min_score <= 1.0:
        raise ValueError("min_score must be between -1 and 1")


def _identity_cache_fields(context: Any) -> tuple[Any, ...]:
    identifies_agent = context.read_scope in (ReadScope.AGENT, ReadScope.SESSION)
    identifies_session = context.read_scope is ReadScope.SESSION
    return (
        context.read_scope.value,
        context.agent_id if identifies_agent else "",
        context.session_id if identifies_session else "",
        tuple(context.source_types),
        context.min_score,
        context.include_metadata,
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
    context_fingerprint: str = ""
    ad_hoc: bool = False

    def __post_init__(self) -> None:
        for name in ("project_id", "project_name", "root_id"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        _normalize_common(self)
        try:
            active_root = _canonical_root(self.project_root, "project_root")
        except ValueError as exc:
            raise ValueError("Active project_root must be an existing registered root") from exc
        registered_roots = tuple(str(_canonical_root(root, "allowed_root")) for root in self.allowed_roots)
        if str(active_root) not in registered_roots:
            raise ValueError("Active project_root must be a registered root")
        object.__setattr__(self, "project_root", str(active_root))
        object.__setattr__(self, "allowed_roots", registered_roots)
        object.__setattr__(self, "write_visibility", WriteVisibility(self.write_visibility))
        object.__setattr__(self, "validation_status", ValidationStatus(self.validation_status))
        for name in ("registry_fingerprint", "context_fingerprint"):
            value = getattr(self, name)
            if value:
                object.__setattr__(self, name, _required(value, name))

    def validate_path(self, value: str | Path, *, must_exist: bool = False) -> Path:
        raw = str(value)
        if ".." in raw.replace("\\", "/").split("/"):
            raise ValueError("Path traversal is forbidden")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = Path(self.project_root) / candidate
        original = candidate
        candidate = _canonical_path(candidate, "path", must_exist=must_exist)
        if not _is_within(candidate, Path(self.project_root)):
            raise ValueError("Path is outside the active registered root")
        if candidate != original:
            raise ValueError("Symbolic links are not allowed in paths")
        return candidate

    @property
    def cache_key(self) -> tuple[Any, ...]:
        return (
            self.project_id,
            self.project_name,
            self.root_id,
            self.project_root,
            tuple(self.allowed_roots),
            *_identity_cache_fields(self),
            self.write_visibility.value,
            self.agent_id,
            self.session_id,
            self.language,
            self.validation_status.value,
            self.context_fingerprint,
        )

    def search_cache_key(self, query: str, *, limit: int, score_threshold: float | None = None,
                         search_mode: str = "search", parameters: Iterable[Any] = ()) -> tuple[Any, ...]:
        return (
            *self.cache_key,
            str(query).strip(),
            int(limit),
            self.min_score if score_threshold is None else float(score_threshold),
            str(search_mode),
            tuple(parameters),
        )

    def to_request_dict(self) -> dict[str, Any]:
        if self.ad_hoc:
            return {
                "working_directory": self.project_root,
                "write_visibility": self.write_visibility.value,
                "validation_status": self.validation_status.value,
                **_common_request_fields(self),
            }
        return {
            "project_id": self.project_id,
            "root_id": self.root_id,
            "write_visibility": self.write_visibility.value,
            **_common_request_fields(self),
            "validation_status": self.validation_status.value,
            "registry_fingerprint": self.registry_fingerprint,
            "context_fingerprint": self.context_fingerprint,
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
    context_fingerprint: str = ""

    def __post_init__(self) -> None:
        for name in ("project_id", "project_name"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        _normalize_common(self)
        for name in ("registry_fingerprint", "context_fingerprint"):
            value = getattr(self, name)
            if value:
                object.__setattr__(self, name, _required(value, name))

    def to_request_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            **_common_request_fields(self),
            "registry_fingerprint": self.registry_fingerprint,
            "context_fingerprint": self.context_fingerprint,
        }

    def search_cache_key(self, query: str, *, limit: int, score_threshold: float | None = None,
                         search_mode: str = "search", parameters: Iterable[Any] = ()) -> tuple[Any, ...]:
        return (
            self.project_id,
            *_identity_cache_fields(self),
            self.min_score if score_threshold is None else float(score_threshold),
            self.context_fingerprint,
            str(query).strip(),
            int(limit),
            str(search_mode),
            tuple(parameters),
        )


@dataclass(frozen=True, slots=True)
class GlobalWriteContext:
    write_visibility: WriteVisibility
    read_scope: ReadScope
    agent_id: str
    session_id: str
    validation_status: ValidationStatus = ValidationStatus.UNVERIFIED
    source_types: tuple[str, ...] = field(default_factory=tuple)
    language: str = "en"
    min_score: float = 0.0
    include_metadata: bool = True
    project_id: str = field(default="__global_knowledge__", init=False)
    project_name: str = field(default="Global knowledge", init=False)
    root_id: str = field(default="global", init=False)
    project_root: str = field(default="", init=False)

    def __post_init__(self) -> None:
        _normalize_common(self)
        object.__setattr__(self, "write_visibility", WriteVisibility(self.write_visibility))
        object.__setattr__(self, "validation_status", ValidationStatus(self.validation_status))

    def to_request_dict(self) -> dict[str, Any]:
        return {"global_write": True, "write_visibility": self.write_visibility.value,
                "validation_status": self.validation_status.value, **_common_request_fields(self)}


@dataclass(frozen=True, slots=True)
class WorkingDirectoryContext:
    working_directory: str
    read_scope: ReadScope
    agent_id: str
    session_id: str
    source_types: tuple[str, ...] = field(default_factory=tuple)
    language: str = "en"
    min_score: float = 0.0
    include_metadata: bool = True
    write_visibility: WriteVisibility = WriteVisibility.SESSION_PRIVATE
    validation_status: ValidationStatus = ValidationStatus.UNVERIFIED

    def __post_init__(self) -> None:
        object.__setattr__(self, "working_directory", str(workspace_definition(self.working_directory).roots["workspace"]))
        _normalize_common(self)
        object.__setattr__(self, "write_visibility", WriteVisibility(self.write_visibility))
        object.__setattr__(self, "validation_status", ValidationStatus(self.validation_status))

    def to_request_dict(self) -> dict[str, Any]:
        return {
            "working_directory": self.working_directory,
            "write_visibility": self.write_visibility.value,
            "validation_status": self.validation_status.value,
            **_common_request_fields(self),
        }

    def search_cache_key(self, query: str, *, limit: int, score_threshold: float | None = None,
                         search_mode: str = "search", parameters: Iterable[Any] = ()) -> tuple[Any, ...]:
        return (
            "working-directory",
            self.working_directory,
            *_identity_cache_fields(self),
            str(query).strip(),
            int(limit),
            self.min_score if score_threshold is None else float(score_threshold),
            str(search_mode),
            tuple(parameters),
        )


@dataclass(frozen=True, slots=True)
class GlobalKnowledgeContext:
    read_scope: ReadScope
    agent_id: str
    session_id: str
    source_types: tuple[str, ...] = field(default_factory=tuple)
    language: str = "en"
    min_score: float = 0.0
    include_metadata: bool = True

    def __post_init__(self) -> None:
        _normalize_common(self)

    def to_request_dict(self) -> dict[str, Any]:
        return {"global_search": True, **_common_request_fields(self)}

    def search_cache_key(self, query: str, *, limit: int, score_threshold: float | None = None,
                         search_mode: str = "search", parameters: Iterable[Any] = ()) -> tuple[Any, ...]:
        return (
            "global",
            *_identity_cache_fields(self),
            str(query).strip(),
            int(limit),
            self.min_score if score_threshold is None else float(score_threshold),
            str(search_mode),
            tuple(parameters),
        )


def build_default_project_context(
    settings: Any,
    *,
    agent_id: str,
    session_id: str,
    project_id: str | None = None,
    root_id: str | None = None,
    working_directory: str | None = None,
    write_visibility: str = "session-private",
    read_scope: str = "session",
    source_types: Iterable[str] = (),
    language: str = "en",
    validation_status: str = "unverified",
    min_score: float = 0.0,
    include_metadata: bool = True,
) -> ProjectContext:
    if not project_id and not working_directory:
        raise ValueError("Explicit project_id/root_id or client working_directory is required")
    registry = get_project_registry(settings)
    if project_id and not root_id:
        project = next((item for item in registry.projects if item.project_id == project_id), None)
        if project is not None and len(project.roots) == 1:
            root_id = next(iter(project.roots))
    return registry.resolve(
        project_id=project_id,
        root_id=root_id,
        working_directory=working_directory,
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
_registry_instances: dict[str, tuple[tuple[int, int, int], ProjectRegistry]] = {}


def get_project_registry(settings: Any) -> ProjectRegistry:
    registry_path = Path(settings.projects_file).expanduser().absolute()
    key = str(registry_path)
    try:
        stat = registry_path.stat()
        current_version = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    except OSError:
        current_version = (-1, -1, -1)
    with _registry_lock:
        cached = _registry_instances.get(key)
        if cached is None or cached[0] != current_version:
            registry = ProjectRegistry.from_settings(settings)
            _registry_instances[key] = (current_version, registry)
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
