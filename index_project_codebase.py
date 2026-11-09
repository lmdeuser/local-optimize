#!/usr/bin/env python3
"""Explicit project-boundary entrypoint for indexing this codebase."""

from __future__ import annotations

import os
from pathlib import Path

from config import settings
from project_context import build_default_project_context
from rag_engine import RagEngine


def index_all() -> int:
    root = settings.project_root.resolve()
    context = build_default_project_context(
        settings,
        agent_id="project-indexer",
        session_id="manual-index",
        write_visibility=os.getenv("INDEX_VISIBILITY", "shared"),
        read_scope="shared",
        validation_status="unverified",
    )
    ignored = {
        "__pycache__",
        ".git",
        "venv",
        ".venv",
        "rag-venv",
        "node_modules",
        ".mimocode",
        "artifacts",
        "evaluation",
        "backup",
        "backups",
        "models",
        "refactored-agent-skills",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        "brain",
        "logs",
        ".agents",
        ".claude",
        ".cursor",
        ".vscode",
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


if __name__ == "__main__":
    print(f"Indexed {index_all()} project files")

