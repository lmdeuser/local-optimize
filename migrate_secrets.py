#!/usr/bin/env python3
"""Strict migration of local_optimize-owned database settings only."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Mapping, Sequence

from config import OWNED_SECRET_KEYS, SECRETS_FILE, _parse_env_value, load_secrets_file, save_secrets_file

log = logging.getLogger("local_optimize.secret_migrator")

ALLOWED_SECRET_KEYS = OWNED_SECRET_KEYS
DEFAULTS = {
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "5432",
    "DB_NAME": "aiagent",
    "DB_USER": "aiagent",
}


def _read_env_file(file_path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not file_path.is_file():
        return result
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        if key not in ALLOWED_SECRET_KEYS:
            continue
        normalized = _parse_env_value(value)
        if normalized:
            result[key] = normalized
    return result


def sanitize_existing_secrets(*, secrets_file: Path = SECRETS_FILE) -> bool:
    """Explicitly remove non-owned keys using an atomic write and backup."""
    existing = load_secrets_file(secrets_file)
    allowed = {key: value for key, value in existing.items() if key in ALLOWED_SECRET_KEYS}
    if allowed == existing:
        return True
    return save_secrets_file(allowed, backup=True, file_path=secrets_file)


def migrate_secrets(
    *,
    secrets_file: Path = SECRETS_FILE,
    legacy_paths: Sequence[Path] | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Merge only allowlisted DB settings; no source names or values are logged."""
    if not sanitize_existing_secrets(secrets_file=secrets_file):
        log.error("Existing secrets file could not be safely sanitized")
        return False

    sources = (
        (
            Path.home() / ".antigravity_rag.env",
            Path(__file__).resolve().parent / ".env",
        )
        if legacy_paths is None
        else legacy_paths
    )
    merged: dict[str, str] = {}
    for source in sources:
        merged.update(_read_env_file(Path(source)))
    merged.update(
        {
            key: value
            for key, value in load_secrets_file(secrets_file).items()
            if key in ALLOWED_SECRET_KEYS and value
        }
    )
    environment = os.environ if environ is None else environ
    for key in ALLOWED_SECRET_KEYS:
        value = environment.get(key)
        if value:
            merged[key] = value
    for key, value in DEFAULTS.items():
        merged.setdefault(key, value)

    if not save_secrets_file(merged, backup=True, file_path=secrets_file):
        log.error("Database settings migration failed")
        return False
    if not secrets_file.is_file() or secrets_file.stat().st_mode & 0o777 != 0o600:
        log.error("Database settings file verification failed")
        return False
    if secrets_file.parent.stat().st_mode & 0o777 != 0o700:
        log.error("Database settings directory verification failed")
        return False
    log.info("Database settings migrated using the strict project allowlist")
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate local_optimize database settings")
    parser.add_argument("--sanitize-existing", action="store_true")
    args = parser.parse_args(argv)
    ok = sanitize_existing_secrets() if args.sanitize_existing else migrate_secrets()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
