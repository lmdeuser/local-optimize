#!/usr/bin/env python3
"""Fresh database entrypoint for the shared versioned migration runner."""

from __future__ import annotations

import argparse
import logging

from migration_runner import MigrationRunner, verify_runtime_schema

log = logging.getLogger("local_optimize.create_schema")


def init_db_schema(*, verify_only: bool = False) -> bool:
    try:
        runner = MigrationRunner.from_default_manifest()
        if verify_only:
            conn = runner._connect()
            try:
                return verify_runtime_schema(conn)
            finally:
                conn.close()
        runner.apply_all()
        return True
    except Exception:
        log.exception("Database migration failed")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Initialize or migrate the aiagent PostgreSQL schema."
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify required migrations and invariants without applying changes.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    return 0 if init_db_schema(verify_only=args.verify) else 1


if __name__ == "__main__":
    raise SystemExit(main())
