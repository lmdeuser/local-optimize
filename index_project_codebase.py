#!/usr/bin/env python3
"""Explicit project-boundary entrypoint for manual indexing."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from config import settings
from project_context import build_default_project_context
from rag_engine import RagEngine


def index_all(project_id: str = "", root_id: str = "", working_directory: str = "") -> int:
    context = build_default_project_context(
        settings,
        agent_id="project-indexer",
        session_id="manual-index",
        project_id=project_id or None,
        root_id=root_id or None,
        working_directory=(working_directory or ("" if project_id else str(Path.cwd()))) or None,
        write_visibility=os.getenv("INDEX_VISIBILITY", "shared"),
        read_scope="shared",
        validation_status="unverified",
    )
    root = Path(context.project_root)
    ignored = {
        "__pycache__", ".git", "venv", ".venv", "rag-venv", "node_modules", ".mimocode",
        "artifacts", "evaluation", "backup", "backups", "models", "refactored-agent-skills",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", "brain", "logs",
        ".agents", ".claude", ".cursor", ".vscode",
    }
    engine = RagEngine()
    indexed = 0
    try:
        for current_root, directories, files in os.walk(root):
            directories[:] = [
                name for name in sorted(directories)
                if name not in ignored and not name.startswith(".")
            ]
            for filename in sorted(files):
                path = Path(current_root) / filename
                rel = path.relative_to(root)
                if path.suffix == ".py":
                    result = engine.index_code(context, str(path))
                elif rel.parts[0] == "skills" and path.suffix == ".md":
                    result = engine.index_skill(context, str(path))
                elif path.suffix in {".md", ".sql", ".sh", ".json", ".yaml", ".yml"}:
                    result = engine.index_document(context, str(path))
                else:
                    continue
                if result.get("status") in {"success", "fallback_success"}:
                    indexed += 1
        return indexed
    finally:
        engine.close()


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
    print(f"Indexed {index_all(args.project_id, args.root_id, args.working_directory)} project files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
