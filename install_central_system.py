#!/usr/bin/env python3
"""Transactional installer for local_optimize-owned resources."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from atomic_files import atomic_write_bytes, fsync_directory
from config import settings
from installer_context import InstallationContext, ManagedTarget, deduplicate_targets

BASE_DIR = settings.project_root.resolve()
UNIT_NAMES = ("gpu-rag-server", "watchdog-indexer")
logger = logging.getLogger("local_optimize.installer")
_STDLIB_URLOPEN = urllib.request.urlopen
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass
class PhaseResult:
    name: str
    status: str = "success"
    details: str = ""
    changed: bool = False
    rollback_action: Callable[[], None] | None = None
    committed_boundary: bool = False

    @property
    def is_critical_failure(self) -> bool:
        return self.status == "failed"


@dataclass
class InstallReport:
    results: list[PhaseResult]
    primary_error: str = ""
    rollback_errors: list[str] | None = None
    committed_changes: list[str] | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.primary_error or self.rollback_errors else 0


Phase = tuple[str, Callable[[InstallationContext], PhaseResult]]
Runner = Callable[..., subprocess.CompletedProcess]


def run_phase_sequence(context: InstallationContext, phases: Sequence[Phase]) -> InstallReport:
    results: list[PhaseResult] = []
    primary_error = ""
    for name, phase in phases:
        try:
            result = phase(context)
        except Exception as exc:
            result = PhaseResult(name=name, status="failed", details=str(exc))
        results.append(result)
        if result.changed:
            if result.committed_boundary:
                context.record_committed_change(
                    f"{name}: {result.details or 'committed change remains applied'}"
                )
            elif result.rollback_action is None:
                result = PhaseResult(name, "failed", "Mutating phase omitted rollback action")
                results[-1] = result
            else:
                context.register_rollback(name, result.rollback_action)
        if result.is_critical_failure:
            primary_error = f"{name}: {result.details}"
            if not context.dry_run:
                context.rollback_all()
            break
    return InstallReport(
        results,
        primary_error,
        list(context.rollback_errors),
        list(context.committed_changes),
    )


def _client_targets(clients: Iterable[str]) -> list[ManagedTarget]:
    home = Path.home()
    mapping = {
        "cursor": [home / ".cursor" / "mcp.json"],
        "antigravity": [
            home / ".antigravity" / "mcp_config.json",
            home / ".gemini" / "antigravity" / "mcp_config.json",
        ],
        "gemini": [
            home / ".gemini" / "mcp.json",
            home / ".gemini" / "antigravity" / "mcp_config.json",
        ],
        "opencode": [home / ".config" / "opencode" / "mcp.json"],
        "lmstudio": [home / ".lmstudio" / "mcp.json"],
    }
    return [ManagedTarget(f"{client}-mcp", path) for client in clients for path in mapping[client]]


def build_managed_targets(clients: Iterable[str]) -> tuple[ManagedTarget, ...]:
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    project_configs = [
        ManagedTarget("project-config", BASE_DIR / "config.py"),
        ManagedTarget("mcp-generator", BASE_DIR / "mcp_config_generator.py"),
        ManagedTarget("test-config", BASE_DIR / "pytest.ini"),
        ManagedTarget("secrets", Path.home() / ".config" / "local-optimize" / "secrets.env"),
    ]
    units = [ManagedTarget(f"{name}-unit", systemd_dir / f"{name}.service") for name in UNIT_NAMES]
    return deduplicate_targets([*project_configs, *units, *_client_targets(clients)])


def phase_preflight(context: InstallationContext) -> PhaseResult:
    if shutil.disk_usage(BASE_DIR).free < 100 * 1024 * 1024:
        return PhaseResult("preflight", "failed", "Insufficient disk space")
    if settings.db_name != "aiagent":
        return PhaseResult("preflight", "failed", "Configured database is not the agent database")
    return PhaseResult("preflight")


def phase_backup(context: InstallationContext) -> PhaseResult:
    if context.dry_run:
        return PhaseResult("backup", "skipped", "Dry-run: no backup written")
    context.create_backup()
    return PhaseResult("backup", changed=True, rollback_action=lambda: None)


class _SecretMigrationFailed(RuntimeError):
    pass


@contextmanager
def _restore_local_file_on_error(target: Path):
    """Atomically restore one file if work inside the context does not complete."""
    existed = target.is_file()
    original = target.read_bytes() if existed else b""
    original_mode = target.stat().st_mode & 0o777 if existed else 0o600
    try:
        yield
    except BaseException as original_error:
        try:
            if existed:
                atomic_write_bytes(target, original, original_mode)
            elif target.exists():
                target.unlink()
                fsync_directory(target.parent)
        except Exception as restore_error:
            original_error.add_note(
                f"Local secret restore also failed: {restore_error.__class__.__name__}"
            )
        raise


def phase_secret_migration(context: InstallationContext) -> PhaseResult:
    if context.dry_run:
        return PhaseResult("secret_migration", "skipped", "Dry-run")
    context.require_backup()
    target = Path.home() / ".config" / "local-optimize" / "secrets.env"
    from migrate_secrets import migrate_secrets

    try:
        with _restore_local_file_on_error(target):
            if not migrate_secrets():
                raise _SecretMigrationFailed("Secret migration failed")
    except _SecretMigrationFailed as exc:
        notes = getattr(exc, "__notes__", ())
        details = "Secret migration failed"
        if notes:
            details += f"; {notes[-1]}"
        return PhaseResult("secret_migration", "failed", details)
    return PhaseResult(
        "secret_migration", changed=True, rollback_action=lambda: context.restore_target(target)
    )


def phase_db_connectivity(context: InstallationContext) -> PhaseResult:
    if context.dry_run:
        return PhaseResult("db_connectivity", "skipped", "Dry-run")
    try:
        import psycopg2

        with psycopg2.connect(**settings.get_db_params()) as connection:
            connection.set_session(readonly=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database(), 1")
                database, value = cursor.fetchone()
                if database != "aiagent" or value != 1:
                    raise RuntimeError("Unexpected database identity")
    except Exception as exc:
        return PhaseResult("db_connectivity", "failed", f"Database connectivity failed: {exc}")
    return PhaseResult("db_connectivity")


def phase_mcp_config(context: InstallationContext, clients: Sequence[str]) -> PhaseResult:
    from mcp_config_generator import (
        build_antigravity_mcp_config,
        build_cursor_mcp_config,
        build_gemini_mcp_config,
        build_lmstudio_mcp_config,
        build_opencode_mcp_config,
    )

    builders = {
        "cursor": build_cursor_mcp_config,
        "antigravity": build_antigravity_mcp_config,
        "gemini": build_gemini_mcp_config,
        "opencode": build_opencode_mcp_config,
        "lmstudio": build_lmstudio_mcp_config,
    }
    if context.dry_run:
        return PhaseResult("mcp_config", "skipped", "Dry-run")
    context.require_backup()
    changed_paths = [target.path for target in _client_targets(clients)]
    try:
        for client in clients:
            builders[client](backup=False)
    except Exception as original_error:
        rollback_errors: list[str] = []
        for path in reversed(changed_paths):
            try:
                context.restore_target(path)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error.__class__.__name__)
        if rollback_errors:
            raise RuntimeError(
                "MCP configuration failed; installation-backup restore also failed: "
                + ", ".join(rollback_errors)
            ) from original_error
        raise

    def rollback() -> None:
        for path in reversed(changed_paths):
            context.restore_target(path)

    return PhaseResult("mcp_config", changed=bool(clients), rollback_action=rollback)


def _unit_content(name: str) -> bytes:
    script = "gpu_rag_server.py" if name == "gpu-rag-server" else "watchdog_indexer.py"
    after = " gpu-rag-server.service" if name == "watchdog-indexer" else ""
    secrets = Path.home() / ".config" / "local-optimize" / "secrets.env"
    return f"""[Unit]
Description={name} for local_optimize
After=network.target postgresql.service{after}

[Service]
Type=simple
WorkingDirectory={BASE_DIR}
ExecStartPre=/usr/bin/test -f {secrets}
ExecStart={sys.executable} {BASE_DIR / script}
Restart=always
RestartSec=5
EnvironmentFile={secrets}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
""".encode("utf-8")


def _run_checked(runner: Runner, args: list[str]) -> subprocess.CompletedProcess:
    result = runner(args, capture_output=True, text=True, timeout=30, check=False, shell=False)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({args[0]} {args[2] if len(args) > 2 else ''})")
    return result


def _service_state(runner: Runner, name: str, operation: str) -> str:
    result = runner(
        ["systemctl", "--user", operation, name],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        shell=False,
    )
    return (result.stdout or "").strip() or "unknown"


def phase_systemd(
    context: InstallationContext,
    skip_systemd: bool,
    runner: Runner = subprocess.run,
) -> PhaseResult:
    if skip_systemd:
        return PhaseResult("systemd", "skipped", "Skipped by option")
    if context.dry_run:
        return PhaseResult("systemd", "skipped", "Dry-run")
    context.require_backup()
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    unit_paths = [service_dir / f"{name}.service" for name in UNIT_NAMES]
    prior_states = {
        name: {
            "enabled": _service_state(runner, name, "is-enabled"),
            "active": _service_state(runner, name, "is-active"),
        }
        for name in UNIT_NAMES
    }
    temporary_paths: list[Path] = []

    def restore() -> None:
        for path in unit_paths:
            context.restore_target(path)
        _run_checked(runner, ["systemctl", "--user", "daemon-reload"])
        for name, state in prior_states.items():
            enabled_action = "enable" if state["enabled"] == "enabled" else "disable"
            _run_checked(runner, ["systemctl", "--user", enabled_action, name])
            if state["active"] == "active":
                _run_checked(runner, ["systemctl", "--user", "start", name])
            else:
                _run_checked(runner, ["systemctl", "--user", "stop", name])

    try:
        for name, target in zip(UNIT_NAMES, unit_paths):
            temporary = service_dir / f".{uuid.uuid4().hex}-{target.name}"
            temporary_paths.append(temporary)
            with temporary.open("wb") as stream:
                os.chmod(temporary, 0o644)
                stream.write(_unit_content(name))
                stream.flush()
                os.fsync(stream.fileno())
        _run_checked(runner, ["systemd-analyze", "--user", "verify", *map(str, temporary_paths)])
        for temporary, target in zip(temporary_paths, unit_paths):
            os.replace(temporary, target)
            os.chmod(target, 0o644)
        fsync_directory(service_dir)
        _run_checked(runner, ["systemctl", "--user", "daemon-reload"])
        _run_checked(runner, ["systemctl", "--user", "enable", *UNIT_NAMES])
        _run_checked(runner, ["systemctl", "--user", "restart", *UNIT_NAMES])
        for name in UNIT_NAMES:
            if _service_state(runner, name, "is-enabled") != "enabled":
                raise RuntimeError(f"Service is not enabled: {name}")
            if _service_state(runner, name, "is-active") != "active":
                raise RuntimeError(f"Service is not active: {name}")
    except Exception as exc:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)
        try:
            restore()
        except Exception as rollback_exc:
            return PhaseResult(
                "systemd", "failed", f"{exc}; rollback failure: {rollback_exc}"
            )
        return PhaseResult("systemd", "failed", str(exc))
    return PhaseResult("systemd", changed=True, rollback_action=restore)


def _fetch_health(path: str) -> tuple[int, dict[str, object]]:
    headers = {}
    token = settings.bearer_token.get_secret_value()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{settings.rag_server_url}{path}", method="GET", headers=headers
    )
    try:
        response_cm = (
            _DIRECT_OPENER.open(request, timeout=2)
            if urllib.request.urlopen is _STDLIB_URLOPEN
            else urllib.request.urlopen(request, timeout=2)
        )
        with response_cm as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def phase_verification(context: InstallationContext) -> PhaseResult:
    if context.dry_run:
        return PhaseResult("verification", "skipped", "Dry-run")
    for _ in range(30):
        try:
            code, payload = _fetch_health("/liveness")
            if code == 200 and payload == {"status": "success"}:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        return PhaseResult("verification", "failed", "Liveness contract failed")

    for _ in range(30):
        try:
            code, payload = _fetch_health("/readiness")
            if code == 200 and payload.get("status") == "success" and payload.get("ready") is True:
                return PhaseResult("verification")
        except Exception:
            pass
        time.sleep(1)
    return PhaseResult("verification", "failed", "Readiness contract failed")


def phase_migrations(context: InstallationContext, skip_db: bool) -> PhaseResult:
    if skip_db:
        return PhaseResult("migrations", "skipped", "Skipped by option")
    if context.dry_run:
        try:
            from migration_runner import MigrationRunner

            MigrationRunner.from_default_manifest().validate_plan()
            return PhaseResult("migrations", "skipped", "Dry-run plan validated")
        except ImportError:
            return PhaseResult("migrations", "skipped", "Dry-run; migration runner pending")
    context.require_backup()
    from migration_runner import MigrationError, MigrationRunner

    runner = MigrationRunner.from_default_manifest()
    try:
        committed = runner.apply_all()
    except MigrationError as exc:
        committed = exc.committed_versions
        details = str(exc)
        if committed:
            details += "; committed PostgreSQL migrations remain applied: " + ", ".join(committed)
        return PhaseResult(
            "migrations",
            "failed",
            details,
            changed=bool(committed),
            committed_boundary=bool(committed),
        )
    if not committed:
        return PhaseResult("migrations", details="Schema already matches the manifest")
    return PhaseResult(
        "migrations",
        details="Committed PostgreSQL migrations remain applied after later filesystem rollback: "
        + ", ".join(committed),
        changed=True,
        committed_boundary=True,
    )


def build_phases(
    clients: Sequence[str], skip_systemd: bool, skip_db: bool
) -> list[Phase]:
    return [
        ("preflight", phase_preflight),
        ("backup", phase_backup),
        ("secret_migration", phase_secret_migration),
        ("db_connectivity", phase_db_connectivity),
        ("migrations", lambda ctx: phase_migrations(ctx, skip_db)),
        ("mcp_config", lambda ctx: phase_mcp_config(ctx, clients)),
        ("systemd", lambda ctx: phase_systemd(ctx, skip_systemd)),
        ("verification", phase_verification),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="local_optimize infrastructure installer")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--client",
        action="append",
        choices=["cursor", "antigravity", "gemini", "opencode", "lmstudio"],
    )
    parser.add_argument("--all-clients", action="store_true")
    parser.add_argument("--skip-systemd", action="store_true")
    parser.add_argument("--skip-db-migration", action="store_true")
    args = parser.parse_args(argv)
    clients = (
        ["cursor", "antigravity", "gemini", "opencode"]
        if args.all_clients
        else (args.client or [])
    )
    targets = build_managed_targets(clients)
    context = InstallationContext(
        dry_run=args.dry_run,
        backup_root=Path.home() / ".local" / "share" / "local-optimize" / "backups",
        targets=targets,
    )
    report = run_phase_sequence(
        context, build_phases(clients, args.skip_systemd, args.skip_db_migration)
    )
    if report.primary_error:
        logger.error("Installation failed: %s", report.primary_error)
    for error in report.rollback_errors or []:
        logger.error("Rollback failed: %s", error)
    for committed in report.committed_changes or []:
        logger.warning("Committed boundary: %s", committed)
    return report.exit_code


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
