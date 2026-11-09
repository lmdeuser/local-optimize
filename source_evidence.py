"""Bounded provenance checks: indexed text is evidence, never an instruction."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def read_snapshot(path: Path, max_bytes: int = 10_000_000) -> tuple[str, dict]:
    """Read one validated source and record the version actually read."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        data = stream.read(max_bytes + 1)
        after = os.fstat(stream.fileno())
    if len(data) > max_bytes:
        raise ValueError("Source exceeds the indexing size limit")
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise RuntimeError("Source changed during indexing")
    return data.decode("utf-8"), {
        "mtime_ns": after.st_mtime_ns,
        "size": after.st_size,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def source_kind(hit: dict) -> str:
    """Keep generated notes distinct from primary files and test fixtures."""
    source = str(hit.get("source_uri") or "")
    kind = hit.get("source_type", "")
    if kind in {"llm_output", "agent_note", "conversation", "dialogue"} or source == "agent_generated":
        return "agent_note"
    parts = Path(str(hit.get("file_path") or source)).parts
    if "tests" in parts or "probes" in parts or "evaluation" in parts:
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
        stat = path.stat()
        if not path.is_file():
            return "missing"
    except FileNotFoundError:
        return "missing"
    except (OSError, RuntimeError):
        return "unavailable"
    if stat.st_mtime_ns != snapshot.get("mtime_ns") or stat.st_size != snapshot.get("size"):
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
        source_path = Path(str(item.get("source_uri") or item.get("file_path") or ""))
        source_parts = next((source_path.relative_to(root).parts for root in roots
                             if source_path.is_relative_to(root)), source_path.parts)
        if any(part in {"evaluation", "artifacts", "probes"} for part in source_parts):
            rejected += 1
            continue
        snapshot = (item.get("metadata") or {}).get("source_snapshot")
        key = (str(item.get("source_uri") or ""), str(snapshot))
        if key not in states:
            states[key] = source_state(item, roots)
        item["source_state"] = states[key]
        item["source_kind"] = source_kind(item)
        if item["source_state"] in {"changed", "missing", "outside_project", "unavailable"}:
            rejected += 1
        else:
            accepted.append(item)
    return accepted, rejected
