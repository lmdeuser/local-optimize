#!/usr/bin/env python3
"""Build and verify the minimal reviewed project archive."""

from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from pathlib import Path

from atomic_files import fsync_directory

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = PROJECT_ROOT.parent / "local_optimize_reviewed.zip"

EXCLUDED_PARTS = {
    ".git",
    ".mimocode",
    ".cursor",
    ".vscode",
    ".idea",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "backup",
    "backups",
    "build",
    "dist",
    "investigation",
    "refactored-agent-skills",
    "scripts",
}
EXCLUDED_NAMES = {
    "server.log",
    "secrets.env",
    ".env",
    "install_central_system_status.md",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".zip", ".log", ".coverage"}
ALLOWED_DIRECTORIES = {"debian", "docs", "evaluation", "hooks", "migrations", "obs", "packaging", "skills", "tests"}
ALLOWED_SUPPORT_FILES = {"deploy/intel-xpu-requirements.txt"}
ALLOWED_ROOT_FILES = {
    ".gitignore",
    "ARCHITECTURE.md",
    "DATABASE_SCHEMA.md",
    "LICENSE",
    "MCP_TOOLS.md",
    "MIGRATIONS.md",
    "README.md",
    "SYSTEM.md",
    "TESTING.md",
    "THIRD_PARTY_NOTICES.md",
    "__init__.py",
    "atomic_files.py",
    "build_review_archive.py",
    "config.py",
    "create_schema.py",
    "evidence_ranker.py",
    "evaluate_retrieval.py",
    "fast_router.py",
    "gpu_rag_server.py",
    "index_project_codebase.py",
    "indexer_queue.py",
    "install_central_system.py",
    "installer_context.py",
    "lmstudio_rag_mcp.py",
    "local_inference.py",
    "local_model.json",
    "manage_secrets.py",
    "mcp_config_generator.py",
    "mcp_server.py",
    "migrate_secrets.py",
    "migration_runner.py",
    "postgres_backup.sh",
    "project_context.py",
    "pytest.ini",
    "query_analysis.py",
    "query_cache.py",
    "rag_benchmark.py",
    "rag_client.py",
    "rag_engine.py",
    "reembed_corpus.py",
    "requirements-dev.txt",
    "requirements.txt",
    "response_format.py",
    "retrieval_policy.json",
    "ruff.toml",
    "skills_manager.py",
    "source_evidence.py",
    "watchdog_indexer.py",
}


def _included(path: Path) -> bool:
    relative = path.relative_to(PROJECT_ROOT)
    if path.is_symlink() or not path.is_file():
        return False
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if path.name in EXCLUDED_NAMES or path.suffix in EXCLUDED_SUFFIXES:
        return False
    if relative.parts[0] in ALLOWED_DIRECTORIES:
        return True
    if relative.as_posix() in ALLOWED_SUPPORT_FILES:
        return True
    return len(relative.parts) == 1 and path.name in ALLOWED_ROOT_FILES


def build_file_list() -> list[Path]:
    files = sorted(path for path in PROJECT_ROOT.rglob("*") if _included(path))
    if not files:
        raise RuntimeError("Archive allowlist produced no files")
    for path in files:
        relative = path.relative_to(PROJECT_ROOT)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"Unsafe archive path: {relative}")
    return files


def _hashes(files: list[Path]) -> dict[str, str]:
    return {
        str(path.relative_to(PROJECT_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }


def build_archive(output_path: Path = OUTPUT_PATH) -> Path:
    files = build_file_list()
    expected = _hashes(files)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
        with zipfile.ZipFile(
            temporary_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for path in files:
                archive.write(path, path.relative_to(PROJECT_ROOT).as_posix())

        with temporary_path.open("rb") as stream:
            os.fsync(stream.fileno())
        with zipfile.ZipFile(temporary_path, "r") as archive:
            members = archive.namelist()
            if len(members) != len(set(members)):
                raise RuntimeError("Archive contains duplicate members")
            if any(Path(name).is_absolute() or ".." in Path(name).parts for name in members):
                raise RuntimeError("Archive contains unsafe member path")
            if archive.testzip() is not None:
                raise RuntimeError("Archive CRC verification failed")
            with tempfile.TemporaryDirectory(prefix="local-optimize-archive-") as temporary:
                root = Path(temporary)
                archive.extractall(root)
                actual = {
                    name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for name in members
                }
        if actual != expected:
            raise RuntimeError("Extracted archive does not match source manifest")
        os.replace(temporary_path, output_path)
        temporary_path = None
        fsync_directory(output_path.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return output_path


if __name__ == "__main__":
    print(build_archive())
