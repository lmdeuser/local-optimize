"""Bounded provenance checks: indexed text is evidence, never an instruction."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


def read_snapshot(path: Path, max_bytes: int = 10_000_000) -> tuple[str, dict]:
    """Read one validated source and record the version actually read."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Source must be a regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            data = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > max_bytes:
        raise ValueError("Source exceeds the indexing size limit")
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise RuntimeError("Source changed during indexing")
    return data.decode("utf-8"), {
        "mtime_ns": after.st_mtime_ns,
        "size": after.st_size,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _relative_parts(raw_path: str, roots: tuple[Path, ...]) -> tuple[str, ...]:
    path = Path(raw_path)
    if path.is_absolute():
        for root in roots:
            try:
                if path.is_relative_to(root):
                    return path.relative_to(root).parts
            except (OSError, ValueError):
                continue
    return path.parts


def source_kind(hit: dict, roots: tuple[Path, ...] = ()) -> str:
    """Keep generated notes distinct from primary files and test fixtures."""
    source = str(hit.get("source_uri") or "")
    kind = hit.get("source_type", "")
    if kind in {"llm_output", "agent_note", "conversation", "dialogue"} or source == "agent_generated":
        return "agent_note"
    rel_target = str(hit.get("file_path") or "")
    parts = Path(rel_target).parts if rel_target and not Path(rel_target).is_absolute() else _relative_parts(rel_target or source, roots)
    name = Path(rel_target or source).name
    if (
        "tests" in parts
        or (parts and parts[0] in {"probes", "evaluation", "artifacts"})
        or name == "conftest.py"
        or name.startswith("test_")
        or name.endswith("_test.py")
    ):
        return "test_fixture"
    return kind if kind in {"code", "document", "skill"} else "stored_note"


def source_state(hit: dict, roots: tuple[Path, ...]) -> str:
    """Compare source stat with its indexed snapshot without reading arbitrary paths."""
    metadata = hit.get("metadata") or {}
    snapshot = metadata.get("source_snapshot")
    if not isinstance(snapshot, dict):
        return "untracked"
    raw = str(hit.get("source_uri") or "")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        return "unavailable"
    # Never follow a symlink supplied through stored metadata, even inside a root.
    try:
        if path.resolve(strict=False) != path:
            return "unavailable"
        if not any(path.is_relative_to(root) for root in roots):
            return "outside_project"
        st = path.stat()
        if not path.is_file():
            return "missing"
    except FileNotFoundError:
        return "missing"
    except (OSError, RuntimeError):
        return "unavailable"
    if st.st_mtime_ns != snapshot.get("mtime_ns") or st.st_size != snapshot.get("size"):
        return "changed"
    return "current"


def prepare_evidence(hits: list[dict], roots: tuple[Path, ...]) -> tuple[list[dict], int]:
    """Reject stale file snapshots even when the search response came from cache."""
    accepted = []
    rejected = 0
    states: dict[tuple[str, str], str] = {}
    for hit in hits:
        item = dict(hit)
        # Old indexes may predate the watcher's exclusions. Evaluation questions
        # are not evidence answering themselves, regardless of model score.
        uri_parts = _relative_parts(str(item.get("source_uri") or ""), roots)
        file_parts = _relative_parts(str(item.get("file_path") or ""), roots)
        if (uri_parts and uri_parts[0] in {"evaluation", "artifacts", "probes"}) or (
            file_parts and file_parts[0] in {"evaluation", "artifacts", "probes"}
        ):
            rejected += 1
            continue
        snapshot = (item.get("metadata") or {}).get("source_snapshot")
        key = (str(item.get("source_uri") or ""), str(snapshot))
        if key not in states:
            states[key] = source_state(item, roots)
        item["source_state"] = states[key]
        item["source_kind"] = source_kind(item, roots=roots)
        if item["source_state"] in {"changed", "missing", "outside_project", "unavailable"}:
            rejected += 1
        else:
            accepted.append(item)
    return accepted, rejected
