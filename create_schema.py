#!/usr/bin/env python3
"""Fresh database entrypoint for the shared versioned migration runner."""

from __future__ import annotations

import logging

from migration_runner import MigrationRunner

log = logging.getLogger("local_optimize.create_schema")


def init_db_schema() -> bool:
    try:
        MigrationRunner.from_default_manifest().apply_all()
        return True
    except Exception:
        log.exception("Database migration failed")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(0 if init_db_schema() else 1)
