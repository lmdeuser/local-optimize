#!/usr/bin/env python3
"""Explicit project-boundary entrypoint for manual indexing."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from config import settings
from indexer_queue import IGNORED_DIRS
from project_context import build_default_project_context, get_project_registry
from rag_engine import CODE_EXTENSIONS, DOCUMENT_EXTENSIONS, RagEngine


def _index_root(project_id: str = "", root_id: str = "", working_directory: str = "") -> tuple[int, int]:
    caller_directory = working_directory or (
        "" if project_id else os.environ.get("LOCAL_OPTIMIZE_CALLER_DIR", str(Path.cwd()))
    )
    context = build_default_project_context(
        settings,
        agent_id="project-indexer",
        session_id="manual-index",
        project_id=project_id or None,
        root_id=root_id or None,
        working_directory=caller_directory or None,
        write_visibility=os.getenv("INDEX_VISIBILITY", "shared"),
        read_scope="shared",
        validation_status="unverified",
    )
    root = Path(context.project_root)
    registry = get_project_registry(settings)

    def _is_owned(candidate: Path) -> bool:
        return registry.owns_context_path(context, candidate)

    engine = RagEngine()
    indexed = 0
    failed = 0
    try:
        for current_root, directories, files in os.walk(root):
            directories[:] = [
                name for name in sorted(directories)
                if name not in IGNORED_DIRS
                and not name.startswith(".")
                and not (Path(current_root) / name).is_symlink()
                and _is_owned(Path(current_root) / name)
            ]
            for filename in sorted(files):
                path = Path(current_root) / filename
                if path.is_symlink() or not _is_owned(path):
                    continue
                rel = path.relative_to(root)
                suffix = path.suffix.lower()
                if suffix in CODE_EXTENSIONS:
                    result = engine.index_code(context, str(path))
                elif rel.parts[0] == "skills" and suffix == ".md":
                    result = engine.index_skill(context, str(path))
                elif suffix in DOCUMENT_EXTENSIONS:
                    result = engine.index_document(context, str(path))
                else:
                    continue
                if result.get("status") in {"success", "fallback_success"}:
                    indexed += 1
                else:
                    failed += 1
                    print(f"Failed to index {path}: {result}", file=sys.stderr)
        return indexed, failed
    finally:
        engine.close()


def index_all(project_id: str = "", root_id: str = "", working_directory: str = "") -> int:
    indexed, _failed = _index_root(project_id, root_id, working_directory)
    return indexed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--root-id", default="")
    parser.add_argument("--working-directory", default="")
    args = parser.parse_args()
    if bool(args.project_id) != bool(args.root_id):
        parser.error("--project-id and --root-id must be provided together")
    if args.project_id and args.working_directory:
        parser.error("Explicit project/root cannot be combined with --working-directory")
    try:
        indexed, failed = _index_root(args.project_id, args.root_id, args.working_directory)
    except Exception as exc:
        print(f"Project indexing failed: {exc}", file=sys.stderr)
        return 1
    print(f"Indexed {indexed} project files")
    if failed:
        print(f"Failed to index {failed} project files", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
