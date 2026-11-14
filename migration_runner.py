"""Strict versioned SQL migration runner for the aiagent.public schema."""

from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from config import settings

EXPECTED_DATABASE = "aiagent"
EXPECTED_SCHEMA = "public"
MANIFEST_PATH = Path(__file__).resolve().parent / "migrations" / "manifest.json"
MIGRATION_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"local_optimize:aiagent:public:migrations", usedforsecurity=False).digest()[:8],
    byteorder="big",
    signed=True,
)
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_INTERVAL_SECONDS = 0.1

REGISTRY_METADATA_COLUMNS = {
    "filename": "TEXT",
    "checksum": "TEXT",
    "execution_status": "TEXT",
    "reconciled_at": "TIMESTAMPTZ",
    "reconciliation_method": "TEXT",
}


class MigrationError(RuntimeError):
    def __init__(self, message: str, *, committed_versions: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.committed_versions = tuple(committed_versions)


class LegacyUnverifiedError(MigrationError):
    pass


@dataclass(frozen=True)
class Invariant:
    type: str
    name: str
    table: str = ""


@dataclass(frozen=True)
class Migration:
    version: str
    filename: str
    checksum: str
    required: bool
    transactional: bool
    invariants: tuple[Invariant, ...]


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes(), usedforsecurity=False).hexdigest()


def load_manifest(path: Path = MANIFEST_PATH) -> tuple[Migration, ...]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MigrationError(f"Invalid migration manifest: {path.name}") from exc
    if data.get("format_version") != 1:
        raise MigrationError("Unsupported migration manifest format")
    if data.get("database") != EXPECTED_DATABASE or data.get("schema") != EXPECTED_SCHEMA:
        raise MigrationError("Migration manifest targets an unexpected database/schema")
    raw_items = data.get("migrations")
    if not isinstance(raw_items, list) or not raw_items:
        raise MigrationError("Migration manifest must contain migrations")
    versions: set[str] = set()
    filenames: set[str] = set()
    migrations: list[Migration] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise MigrationError("Migration metadata must be an object")
        version = raw.get("version")
        filename = raw.get("filename")
        checksum = raw.get("checksum")
        if not all(isinstance(item, str) and item for item in (version, filename, checksum)):
            raise MigrationError("Migration version, filename and checksum are required")
        if Path(filename).name != filename or Path(filename).suffix != ".sql":
            raise MigrationError(f"Unsafe migration filename: {filename}")
        if version in versions or filename in filenames:
            raise MigrationError(f"Duplicate migration metadata: {version}/{filename}")
        versions.add(version)
        filenames.add(filename)
        transactional = raw.get("transactional", True)
        if transactional is not True:
            raise MigrationError(
                f"Non-transactional migration {version} requires a separately reviewed compensating policy"
            )
        raw_invariants = raw.get("invariants", [])
        invariants = tuple(
            Invariant(item["type"], item["name"], item.get("table", ""))
            for item in raw_invariants
            if isinstance(item, dict) and "type" in item and "name" in item
        )
        if len(invariants) != len(raw_invariants):
            raise MigrationError(f"Malformed invariants for migration {version}")
        migrations.append(
            Migration(
                version,
                filename,
                checksum,
                raw.get("required", True) is not False,
                True,
                invariants,
            )
        )
    if migrations[0].version != "baseline":
        raise MigrationError("Baseline must be the first migration")
    return tuple(migrations)


class MigrationRunner:
    def __init__(
        self,
        manifest_path: Path = MANIFEST_PATH,
        connection_factory: Callable[[], Any] | None = None,
        expected_database: str = EXPECTED_DATABASE,
        lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.manifest_path = Path(manifest_path)
        self.migrations_dir = self.manifest_path.parent
        self.migrations = load_manifest(self.manifest_path)
        self._connection_factory = connection_factory
        self.expected_database = expected_database
        if not math.isfinite(lock_timeout_seconds) or lock_timeout_seconds < 0:
            raise ValueError("lock_timeout_seconds must be finite and not negative")
        self.lock_timeout_seconds = lock_timeout_seconds

    @classmethod
    def from_default_manifest(cls) -> "MigrationRunner":
        return cls()

    def _connect(self):
        if self._connection_factory is not None:
            return self._connection_factory()
        import psycopg2

        return psycopg2.connect(**settings.get_db_params())

    def validate_plan(self) -> tuple[Migration, ...]:
        for migration in self.migrations:
            path = self.migrations_dir / migration.filename
            if not path.is_file():
                if migration.required:
                    raise MigrationError(
                        f"Required migration {migration.version} ({migration.filename}) is missing"
                    )
                continue
            actual = _checksum(path)
            if actual != migration.checksum:
                raise MigrationError(
                    f"Migration {migration.version} ({migration.filename}) checksum mismatch"
                )
        return self.migrations

    def _verify_database(self, cursor) -> None:
        cursor.execute("SELECT current_database()")
        row = cursor.fetchone()
        if not row or row[0] != self.expected_database:
            raise MigrationError("Connected database is not the configured agent database")
        cursor.execute("SET search_path TO public, pg_catalog")

    @staticmethod
    def _registry_exists(cursor) -> bool:
        cursor.execute("SELECT to_regclass('public.schema_migrations')")
        row = cursor.fetchone()
        return bool(row and row[0])

    @staticmethod
    def _registry_columns(cursor) -> set[str]:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'schema_migrations'"
        )
        return {row[0] for row in cursor.fetchall()}

    def _bootstrap_registry(self, cursor) -> bool:
        """Upgrade a legacy registry before any query references new metadata."""
        if not self._registry_exists(cursor):
            return False
        existing = self._registry_columns(cursor)
        missing = [name for name in REGISTRY_METADATA_COLUMNS if name not in existing]
        if not missing:
            return False
        legacy_rows_need_marking = any(
            name not in existing for name in ("filename", "checksum", "execution_status")
        )
        clauses = ", ".join(
            f"ADD COLUMN IF NOT EXISTS {name} {REGISTRY_METADATA_COLUMNS[name]}"
            for name in missing
        )
        cursor.execute(f"ALTER TABLE public.schema_migrations {clauses}")
        if legacy_rows_need_marking:
            cursor.execute(
                "UPDATE public.schema_migrations "
                "SET checksum = NULL, execution_status = 'legacy-unverified'"
            )
        return True

    def _acquire_lock(self, connection) -> None:
        deadline = time.monotonic() + self.lock_timeout_seconds
        while True:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
                row = cursor.fetchone()
            if row and row[0] is True:
                connection.rollback()
                return
            connection.rollback()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MigrationError(
                    f"Timed out waiting for migration advisory lock after "
                    f"{self.lock_timeout_seconds:g} seconds"
                )
            time.sleep(min(LOCK_POLL_INTERVAL_SECONDS, remaining))

    @staticmethod
    def _release_lock(connection) -> None:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
            row = cursor.fetchone()
        connection.rollback()
        if not row or row[0] is not True:
            raise MigrationError("Migration advisory lock was not held at release")

    @contextmanager
    def _locked_plan(self, connection):
        self._acquire_lock(connection)
        try:
            yield
        finally:
            active_error = sys.exc_info()[0] is not None
            try:
                self._release_lock(connection)
            except Exception:
                if not active_error:
                    raise

    def _load_applied(self, cursor) -> dict[str, tuple[str, str]]:
        if not self._registry_exists(cursor):
            return {}
        cursor.execute(
            "SELECT version, checksum, execution_status "
            "FROM public.schema_migrations ORDER BY applied_at, version"
        )
        return {row[0]: (row[1] or "", row[2] or "applied") for row in cursor.fetchall()}

    def statuses(self) -> dict[str, str]:
        self.validate_plan()
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                self._verify_database(cursor)
            connection.rollback()
            with self._locked_plan(connection):
                with connection.cursor() as cursor:
                    registry_changed = self._bootstrap_registry(cursor)
                    applied = self._load_applied(cursor)
                if registry_changed:
                    connection.commit()
                else:
                    connection.rollback()
                result = {}
                for migration in self.migrations:
                    row = applied.get(migration.version)
                    if row is None:
                        result[migration.version] = "pending"
                    elif not row[0]:
                        result[migration.version] = "legacy-unverified"
                    elif row[0] == migration.checksum and row[1] == "applied":
                        result[migration.version] = "verified"
                    else:
                        result[migration.version] = "mismatch"
                return result
        finally:
            connection.close()

    def apply_all(self) -> tuple[str, ...]:
        self.validate_plan()
        connection = self._connect()
        committed_versions: list[str] = []
        try:
            with connection.cursor() as cursor:
                self._verify_database(cursor)
            connection.rollback()
            with self._locked_plan(connection):
                with connection.cursor() as cursor:
                    registry_changed = self._bootstrap_registry(cursor)
                    applied = self._load_applied(cursor)
                if registry_changed:
                    connection.commit()
                else:
                    connection.rollback()
                for migration in self.migrations:
                    row = applied.get(migration.version)
                    if row is not None:
                        checksum, status = row
                        if not checksum:
                            raise LegacyUnverifiedError(
                                f"Migration {migration.version} ({migration.filename}) is legacy-unverified"
                            )
                        if checksum != migration.checksum or status != "applied":
                            raise MigrationError(
                                f"Migration {migration.version} ({migration.filename}) does not match the manifest"
                            )
                        continue
                    path = self.migrations_dir / migration.filename
                    try:
                        with connection.cursor() as cursor:
                            self._verify_database(cursor)
                            cursor.execute(path.read_text(encoding="utf-8"))
                            cursor.execute(
                                "INSERT INTO public.schema_migrations "
                                "(version, filename, checksum, execution_status) "
                                "VALUES (%s, %s, %s, 'applied')",
                                (migration.version, migration.filename, migration.checksum),
                            )
                        connection.commit()
                        applied[migration.version] = (migration.checksum, "applied")
                        committed_versions.append(migration.version)
                    except Exception as exc:
                        connection.rollback()
                        raise MigrationError(
                            f"Migration {migration.version} ({migration.filename}) failed",
                            committed_versions=committed_versions,
                        ) from exc
                return tuple(committed_versions)
        finally:
            connection.close()

    @staticmethod
    def _invariant_exists(cursor, invariant: Invariant) -> bool:
        if invariant.type == "table":
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = %s)",
                (invariant.name,),
            )
        elif invariant.type == "column":
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s)",
                (invariant.table, invariant.name),
            )
        elif invariant.type == "constraint":
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.table_constraints "
                "WHERE table_schema = 'public' AND table_name = %s AND constraint_name = %s)",
                (invariant.table, invariant.name),
            )
        elif invariant.type == "index":
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = %s AND indexname = %s)",
                (invariant.table, invariant.name),
            )
        else:
            raise MigrationError(f"Unknown schema invariant type: {invariant.type}")
        row = cursor.fetchone()
        return bool(row and row[0])

    def reconcile_legacy(self, version: str) -> None:
        self.validate_plan()
        migration = next((item for item in self.migrations if item.version == version), None)
        if migration is None:
            raise MigrationError(f"Unknown migration version: {version}")
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                self._verify_database(cursor)
            connection.rollback()
            with self._locked_plan(connection):
                with connection.cursor() as cursor:
                    self._bootstrap_registry(cursor)
                    cursor.execute(
                        "SELECT checksum FROM public.schema_migrations WHERE version = %s FOR UPDATE",
                        (version,),
                    )
                    row = cursor.fetchone()
                    if row is None or row[0]:
                        raise MigrationError(f"Migration {version} is not legacy-unverified")
                    missing = [
                        f"{item.type}:{item.table}:{item.name}"
                        for item in migration.invariants
                        if not self._invariant_exists(cursor, item)
                    ]
                    if missing:
                        raise LegacyUnverifiedError(
                            f"Migration {version} schema fingerprint mismatch: {', '.join(missing)}"
                        )
                    cursor.execute(
                        "UPDATE public.schema_migrations SET checksum = %s, filename = %s, "
                        "execution_status = 'applied', reconciled_at = now(), "
                        "reconciliation_method = 'schema-fingerprint' WHERE version = %s",
                        (migration.checksum, migration.filename, version),
                    )
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def verify_runtime_schema(
    connection,
    manifest_path: Path = MANIFEST_PATH,
    expected_database: str = EXPECTED_DATABASE,
) -> bool:
    """Read-only full-manifest and runtime-schema compatibility check."""
    try:
        runner = MigrationRunner(
            manifest_path=manifest_path, expected_database=expected_database
        )
        migrations = runner.validate_plan()
        with connection.cursor() as cursor:
            runner._verify_database(cursor)
            if not runner._registry_exists(cursor):
                return False
            cursor.execute(
                "SELECT version, filename, checksum, execution_status "
                "FROM public.schema_migrations"
            )
            applied = {
                row[0]: (row[1] or "", row[2] or "", row[3] or "")
                for row in cursor.fetchall()
            }
            for migration in migrations:
                if not migration.required:
                    continue
                if applied.get(migration.version) != (
                    migration.filename,
                    migration.checksum,
                    "applied",
                ):
                    return False
                if not all(
                    runner._invariant_exists(cursor, invariant)
                    for invariant in migration.invariants
                ):
                    return False
            return True
    except Exception:
        return False
    finally:
        try:
            connection.rollback()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Apply or verify strict versioned SQL migrations for aiagent.public."
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify manifest checksums and schema invariants without modifying the database.",
    )
    args = parser.parse_args(argv)

    runner = MigrationRunner.from_default_manifest()
    if args.verify:
        statuses = runner.statuses()
        conn = runner._connect()
        try:
            ok = verify_runtime_schema(conn)
        finally:
            conn.close()
        for version, status in statuses.items():
            print(f"{version}: {status}")
        return 0 if ok else 1

    committed = runner.apply_all()
    if committed:
        print("Applied migrations:", ", ".join(committed))
    else:
        print("Database schema is up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
