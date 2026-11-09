import hashlib
import json

import pytest

from migration_runner import (
    LegacyUnverifiedError,
    MigrationError,
    MigrationRunner,
    verify_runtime_schema,
)

pytestmark = pytest.mark.unit


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.one = None
        self.many = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        text = str(query)
        self.connection.calls.append((text, params))
        if "BROKEN_FIXTURE" in text:
            raise RuntimeError("forward failure")
        if "current_database()" in text:
            self.one = (self.connection.database,)
        elif "pg_try_advisory_lock" in text:
            result = self.connection.lock_results.pop(0) if self.connection.lock_results else True
            self.one = (result,)
            if result:
                self.connection.lock_held = True
        elif "pg_advisory_unlock" in text:
            self.one = (self.connection.lock_held,)
            self.connection.lock_held = False
        elif "to_regclass" in text:
            self.one = ("schema_migrations" if self.connection.registry else None,)
        elif "SELECT column_name FROM information_schema.columns" in text:
            self.many = [(name,) for name in self.connection.registry_columns]
        elif "ALTER TABLE public.schema_migrations" in text:
            self.connection.registry_columns.update(
                {"filename", "checksum", "execution_status", "reconciled_at", "reconciliation_method"}
            )
        elif "SELECT version, filename, checksum" in text:
            self.many = list(self.connection.runtime_rows)
        elif "SELECT version, checksum" in text:
            self.many = list(self.connection.applied)
        elif "SELECT checksum FROM public.schema_migrations" in text:
            self.one = (self.connection.legacy_checksum,)
        elif "SELECT EXISTS" in text:
            self.one = (self.connection.invariants_exist,)
        elif "CREATE TABLE" in text and "schema_migrations" in text:
            self.connection.registry = True

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class FakeConnection:
    def __init__(
        self,
        *,
        registry=False,
        applied=(),
        runtime_rows=(),
        database="aiagent",
        registry_columns=None,
        lock_results=(),
    ):
        self.registry = registry
        self.registry_columns = set(
            registry_columns
            if registry_columns is not None
            else {
                "version",
                "applied_at",
                "filename",
                "checksum",
                "execution_status",
                "reconciled_at",
                "reconciliation_method",
            }
        )
        self.applied = list(applied)
        self.runtime_rows = list(runtime_rows)
        self.database = database
        self.legacy_checksum = ""
        self.invariants_exist = True
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.lock_results = list(lock_results)
        self.lock_held = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _manifest(tmp_path, migrations):
    folder = tmp_path / "migrations"
    folder.mkdir()
    raw_items = []
    for version, filename, sql, invariants in migrations:
        path = folder / filename
        path.write_text(sql, encoding="utf-8")
        raw_items.append(
            {
                "version": version,
                "filename": filename,
                "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
                "required": True,
                "transactional": True,
                "invariants": invariants,
            }
        )
    path = folder / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "database": "aiagent",
                "schema": "public",
                "migrations": raw_items,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_missing_required_migration_fails_before_connect(tmp_path):
    manifest = _manifest(tmp_path, [("baseline", "0001.sql", "SELECT 1;", [])])
    (manifest.parent / "0001.sql").unlink()
    connected = []
    runner = MigrationRunner(manifest, lambda: connected.append(True))
    with pytest.raises(MigrationError, match=r"baseline \(0001.sql\) is missing"):
        runner.apply_all()
    assert connected == []


def test_transaction_failure_uses_connection_rollback_without_compensating_sql(tmp_path):
    manifest = _manifest(
        tmp_path,
        [
            ("baseline", "0001.sql", "CREATE TABLE public.schema_migrations(id int);", []),
            ("v2", "0002.sql", "BROKEN_FIXTURE;", []),
        ],
    )
    connection = FakeConnection()
    runner = MigrationRunner(manifest, lambda: connection)

    with pytest.raises(MigrationError, match=r"v2 \(0002.sql\) failed") as raised:
        runner.apply_all()

    assert raised.value.committed_versions == ("baseline",)
    assert connection.commits == 1
    assert connection.rollbacks >= 2
    statements = [query for query, _params in connection.calls]
    assert not any("DROP " in query.upper() for query in statements)
    assert connection.closed


def test_empty_checksum_is_reported_as_legacy_unverified_and_not_reapplied(tmp_path):
    manifest = _manifest(tmp_path, [("baseline", "0001.sql", "SELECT 'forward';", [])])
    connection = FakeConnection(
        registry=True,
        applied=[("baseline", "", "applied")],
    )
    runner = MigrationRunner(manifest, lambda: connection)

    with pytest.raises(LegacyUnverifiedError, match="legacy-unverified"):
        runner.apply_all()

    assert not any("SELECT 'forward'" in query for query, _params in connection.calls)


def test_legacy_registry_is_bootstrapped_before_new_columns_are_selected(tmp_path):
    manifest = _manifest(tmp_path, [("baseline", "0001.sql", "SELECT 'forward';", [])])
    connection = FakeConnection(
        registry=True,
        registry_columns={"version", "applied_at"},
        applied=[("baseline", None, "legacy-unverified")],
    )
    runner = MigrationRunner(manifest, lambda: connection)

    with pytest.raises(LegacyUnverifiedError, match="legacy-unverified"):
        runner.apply_all()

    statements = [query for query, _params in connection.calls]
    lock_index = next(i for i, query in enumerate(statements) if "pg_try_advisory_lock" in query)
    columns_index = next(i for i, query in enumerate(statements) if "SELECT column_name" in query)
    alter_index = next(i for i, query in enumerate(statements) if "ALTER TABLE" in query)
    applied_index = next(i for i, query in enumerate(statements) if "SELECT version, checksum" in query)
    assert lock_index < columns_index < alter_index < applied_index
    assert any(
        "SET checksum = NULL, execution_status = 'legacy-unverified'" in query
        for query in statements
    )
    assert not any("SELECT 'forward'" in query for query in statements)
    assert connection.commits == 1
    assert not connection.lock_held
    assert any("pg_advisory_unlock" in query for query in statements)


def test_advisory_lock_timeout_happens_before_registry_bootstrap(tmp_path):
    manifest = _manifest(tmp_path, [("baseline", "0001.sql", "SELECT 1;", [])])
    connection = FakeConnection(
        registry=True,
        registry_columns={"version", "applied_at"},
        lock_results=[False],
    )
    runner = MigrationRunner(manifest, lambda: connection, lock_timeout_seconds=0)

    with pytest.raises(MigrationError, match="Timed out waiting"):
        runner.apply_all()

    statements = [query for query, _params in connection.calls]
    assert not any("information_schema.columns" in query for query in statements)
    assert connection.rollbacks >= 2
    assert connection.closed


def test_advisory_lock_is_released_after_migration_rollback(tmp_path):
    manifest = _manifest(tmp_path, [("baseline", "0001.sql", "BROKEN_FIXTURE;", [])])
    connection = FakeConnection()
    runner = MigrationRunner(manifest, lambda: connection)

    with pytest.raises(MigrationError, match="failed"):
        runner.apply_all()

    statements = [query for query, _params in connection.calls]
    failure_index = next(i for i, query in enumerate(statements) if "BROKEN_FIXTURE" in query)
    unlock_index = next(i for i, query in enumerate(statements) if "pg_advisory_unlock" in query)
    assert failure_index < unlock_index
    assert connection.rollbacks >= 3
    assert not connection.lock_held


def test_legacy_reconciliation_checks_schema_before_updating_checksum(tmp_path):
    invariants = [{"type": "table", "name": "project_knowledge"}]
    manifest = _manifest(tmp_path, [("baseline", "0001.sql", "SELECT 1;", invariants)])
    connection = FakeConnection(registry=True)
    runner = MigrationRunner(manifest, lambda: connection)

    runner.reconcile_legacy("baseline")

    assert connection.commits == 1
    update_index = next(
        index for index, (query, _params) in enumerate(connection.calls) if "UPDATE public.schema_migrations" in query
    )
    invariant_index = next(
        index for index, (query, _params) in enumerate(connection.calls) if "information_schema.tables" in query
    )
    assert invariant_index < update_index


def test_legacy_reconciliation_rolls_back_on_schema_mismatch(tmp_path):
    invariants = [{"type": "table", "name": "project_knowledge"}]
    manifest = _manifest(tmp_path, [("baseline", "0001.sql", "SELECT 1;", invariants)])
    connection = FakeConnection(registry=True)
    connection.invariants_exist = False
    runner = MigrationRunner(manifest, lambda: connection)

    with pytest.raises(LegacyUnverifiedError, match="fingerprint mismatch"):
        runner.reconcile_legacy("baseline")

    assert connection.commits == 0
    assert connection.rollbacks >= 1


def _runtime_fixture(tmp_path):
    manifest = _manifest(
        tmp_path,
        [
            ("baseline", "0001.sql", "SELECT 1;", [{"type": "table", "name": "one"}]),
            ("v2", "0002.sql", "SELECT 2;", [{"type": "table", "name": "two"}]),
        ],
    )
    migrations = MigrationRunner(manifest).migrations
    rows = [
        (item.version, item.filename, item.checksum, "applied") for item in migrations
    ]
    return manifest, rows


def test_runtime_readiness_requires_every_manifest_registry_row(tmp_path):
    manifest, rows = _runtime_fixture(tmp_path)
    complete = FakeConnection(registry=True, runtime_rows=rows)
    missing_baseline = FakeConnection(registry=True, runtime_rows=rows[1:])

    assert verify_runtime_schema(complete, manifest)
    assert not verify_runtime_schema(missing_baseline, manifest)


@pytest.mark.parametrize("field,value", [(2, "wrong-checksum"), (3, "legacy-unverified")])
def test_runtime_readiness_rejects_mismatch_or_non_applied_status(tmp_path, field, value):
    manifest, rows = _runtime_fixture(tmp_path)
    changed = list(rows[0])
    changed[field] = value
    connection = FakeConnection(registry=True, runtime_rows=[tuple(changed), rows[1]])

    assert not verify_runtime_schema(connection, manifest)


def test_runtime_readiness_requires_all_schema_invariants(tmp_path):
    manifest, rows = _runtime_fixture(tmp_path)
    connection = FakeConnection(registry=True, runtime_rows=rows)
    connection.invariants_exist = False

    assert not verify_runtime_schema(connection, manifest)


def test_runtime_readiness_revalidates_migration_file_checksum(tmp_path):
    manifest, rows = _runtime_fixture(tmp_path)
    (manifest.parent / "0001.sql").write_text("SELECT 'changed';", encoding="utf-8")
    connection = FakeConnection(registry=True, runtime_rows=rows)

    assert not verify_runtime_schema(connection, manifest)
    assert connection.calls == []
