import json
import subprocess

import pytest

import install_central_system as installer
from installer_context import InstallationContext, ManagedTarget

pytestmark = pytest.mark.unit


def _context(tmp_path, *, dry_run=False, targets=()):
    return InstallationContext(
        dry_run=dry_run,
        backup_root=tmp_path / "backups",
        targets=tuple(targets),
    )


def test_critical_failure_rolls_back_completed_phases_in_reverse_order(tmp_path):
    events = []

    def success(name):
        def phase(_context):
            events.append(f"apply:{name}")
            return installer.PhaseResult(
                name,
                changed=True,
                rollback_action=lambda: events.append(f"rollback:{name}"),
            )

        return phase

    def failure(_context):
        events.append("apply:four")
        return installer.PhaseResult("four", "failed", "primary")

    report = installer.run_phase_sequence(
        _context(tmp_path),
        [(name, success(name)) for name in ("one", "two", "three")]
        + [("four", failure)],
    )

    assert report.exit_code == 1
    assert report.primary_error == "four: primary"
    assert events[-3:] == ["rollback:three", "rollback:two", "rollback:one"]


def test_rollback_failure_does_not_hide_primary_error(tmp_path):
    def bad_rollback():
        raise RuntimeError("rollback fixture failure")

    report = installer.run_phase_sequence(
        _context(tmp_path),
        [
            (
                "changed",
                lambda _ctx: installer.PhaseResult(
                    "changed", changed=True, rollback_action=bad_rollback
                ),
            ),
            ("failed", lambda _ctx: installer.PhaseResult("failed", "failed", "primary")),
        ],
    )

    assert report.primary_error == "failed: primary"
    assert report.rollback_errors == ["changed: rollback fixture failure"]


def test_success_does_not_run_rollback(tmp_path):
    called = []
    report = installer.run_phase_sequence(
        _context(tmp_path),
        [
            (
                "ok",
                lambda _ctx: installer.PhaseResult(
                    "ok", changed=True, rollback_action=lambda: called.append(True)
                ),
            )
        ],
    )
    assert report.exit_code == 0
    assert called == []


def test_committed_database_boundary_survives_later_filesystem_rollback(tmp_path):
    events = []
    report = installer.run_phase_sequence(
        _context(tmp_path),
        [
            (
                "files",
                lambda _ctx: installer.PhaseResult(
                    "files", changed=True, rollback_action=lambda: events.append("files-restored")
                ),
            ),
            (
                "migrations",
                lambda _ctx: installer.PhaseResult(
                    "migrations",
                    details="committed v8",
                    changed=True,
                    committed_boundary=True,
                ),
            ),
            ("verification", lambda _ctx: installer.PhaseResult("verification", "failed", "fixture")),
        ],
    )

    assert events == ["files-restored"]
    assert report.committed_changes == ["migrations: committed v8"]
    assert report.primary_error == "verification: fixture"


def test_installer_orders_migrations_before_mcp_and_systemd():
    names = [name for name, _phase in installer.build_phases([], False, False)]
    assert names.index("migrations") < names.index("mcp_config") < names.index("systemd")


def test_backup_records_existing_and_absent_targets_and_restores(tmp_path):
    existing = tmp_path / "existing.conf"
    absent = tmp_path / "created.conf"
    existing.write_text("old", encoding="utf-8")
    existing.chmod(0o640)
    context = _context(
        tmp_path,
        targets=(ManagedTarget("existing", existing), ManagedTarget("absent", absent)),
    )
    context.create_backup()

    manifest = json.loads((context.backup_dir / "manifest.json").read_text())
    assert [item["name"] for item in manifest["files"]] == ["existing"]
    assert manifest["absent_targets"] == [{"name": "absent", "target": str(absent)}]

    existing.write_text("new", encoding="utf-8")
    absent.write_text("created", encoding="utf-8")
    context.restore_target(existing)
    context.restore_target(absent)
    assert existing.read_text(encoding="utf-8") == "old"
    assert not absent.exists()


def test_dry_run_backup_has_no_filesystem_effect(tmp_path):
    context = _context(
        tmp_path,
        dry_run=True,
        targets=(ManagedTarget("absent", tmp_path / "absent"),),
    )
    result = installer.phase_backup(context)
    assert result.status == "skipped"
    assert not (tmp_path / "backups").exists()


def _secret_context(tmp_path, monkeypatch, *, existing=True):
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: tmp_path))
    target = tmp_path / ".config" / "local-optimize" / "secrets.env"
    if existing:
        target.parent.mkdir(parents=True)
        target.write_text("DB_USER=before\n", encoding="utf-8")
        target.chmod(0o600)
    context = _context(tmp_path, targets=(ManagedTarget("secrets", target),))
    context.create_backup()
    return context, target


def test_secret_phase_restores_local_file_when_migrator_returns_false(
    tmp_path, monkeypatch
):
    context, target = _secret_context(tmp_path, monkeypatch)

    def partial_failure():
        target.write_text("DB_USER=partial\n", encoding="utf-8")
        return False

    monkeypatch.setattr("migrate_secrets.migrate_secrets", partial_failure)
    result = installer.phase_secret_migration(context)

    assert result.status == "failed"
    assert target.read_text(encoding="utf-8") == "DB_USER=before\n"
    assert target.stat().st_mode & 0o777 == 0o600


def test_secret_phase_restores_file_and_preserves_original_exception(
    tmp_path, monkeypatch
):
    context, target = _secret_context(tmp_path, monkeypatch)
    original = RuntimeError("original migration error")

    def partial_exception():
        target.write_text("DB_USER=partial\n", encoding="utf-8")
        raise original

    monkeypatch.setattr("migrate_secrets.migrate_secrets", partial_exception)
    with pytest.raises(RuntimeError) as raised:
        installer.phase_secret_migration(context)

    assert raised.value is original
    assert target.read_text(encoding="utf-8") == "DB_USER=before\n"


class FakeSystemd:
    def __init__(self, fail_operation=None):
        self.calls = []
        self.fail_operation = fail_operation
        self.enabled = False
        self.active = False

    def __call__(self, args, **kwargs):
        assert kwargs["shell"] is False
        self.calls.append(list(args))
        operation = args[2] if args[0] == "systemctl" else "verify"
        if operation == self.fail_operation:
            return subprocess.CompletedProcess(args, 1, "", "fixture failure")
        if operation == "enable":
            self.enabled = True
        elif operation == "disable":
            self.enabled = False
        elif operation in ("restart", "start"):
            self.active = True
        elif operation == "stop":
            self.active = False
        stdout = ""
        if operation == "is-enabled":
            stdout = "enabled\n" if self.enabled else "disabled\n"
        elif operation == "is-active":
            stdout = "active\n" if self.active else "inactive\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")


def _systemd_context(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: tmp_path))
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    targets = []
    for name in installer.UNIT_NAMES:
        path = unit_dir / f"{name}.service"
        path.write_text(f"old:{name}", encoding="utf-8")
        targets.append(ManagedTarget(name, path))
    context = _context(tmp_path, targets=targets)
    context.create_backup()
    return context, unit_dir


@pytest.mark.systemd
def test_systemd_verifies_enables_and_checks_both_services(tmp_path, monkeypatch):
    context, _unit_dir = _systemd_context(tmp_path, monkeypatch)
    fake = FakeSystemd()

    result = installer.phase_systemd(context, False, runner=fake)

    assert result.status == "success"
    verify = next(call for call in fake.calls if call[0] == "systemd-analyze")
    assert verify[:3] == ["systemd-analyze", "--user", "verify"]
    assert len(verify[3:]) == 2
    for name in installer.UNIT_NAMES:
        assert ["systemctl", "--user", "is-enabled", name] in fake.calls
        assert ["systemctl", "--user", "is-active", name] in fake.calls


@pytest.mark.systemd
def test_systemd_enable_failure_restores_previous_units(tmp_path, monkeypatch):
    context, unit_dir = _systemd_context(tmp_path, monkeypatch)
    fake = FakeSystemd(fail_operation="enable")

    result = installer.phase_systemd(context, False, runner=fake)

    assert result.status == "failed"
    for name in installer.UNIT_NAMES:
        assert (unit_dir / f"{name}.service").read_text() == f"old:{name}"
    assert fake.calls.count(["systemctl", "--user", "daemon-reload"]) >= 1


def _successful_http_response():
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"status":"success"}'

    return Response()


def test_fetch_health_sends_configured_bearer_token(monkeypatch):
    from config import SecretString

    monkeypatch.setattr(installer.settings, "_bearer_token", SecretString("fixture-token"))
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return _successful_http_response()

    monkeypatch.setattr(installer.urllib.request, "urlopen", fake_urlopen)

    code, payload = installer._fetch_health("/liveness")

    assert code == 200
    assert payload == {"status": "success"}
    assert requests[0][0].get_header("Authorization") == "Bearer fixture-token"
    assert requests[0][1] == 2


def test_fetch_health_omits_authorization_without_a_token(monkeypatch):
    from config import SecretString

    monkeypatch.setattr(installer.settings, "_bearer_token", SecretString(""))
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return _successful_http_response()

    monkeypatch.setattr(installer.urllib.request, "urlopen", fake_urlopen)

    installer._fetch_health("/liveness")

    assert requests[0].get_header("Authorization") is None


def test_verification_accepts_canonical_health_statuses(tmp_path, monkeypatch):
    calls = []

    def fetch(path):
        calls.append(path)
        if path == "/liveness":
            return 200, {"status": "success"}
        return 200, {"status": "success", "ready": True}

    monkeypatch.setattr(installer, "_fetch_health", fetch)
    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: None)

    result = installer.phase_verification(_context(tmp_path))

    assert result.status == "success"
    assert calls == ["/liveness", "/readiness"]


def test_verification_stops_after_bounded_liveness_failures(tmp_path, monkeypatch):
    calls = []

    def fetch(path):
        calls.append(path)
        return 503, {"status": "unavailable"}

    monkeypatch.setattr(installer, "_fetch_health", fetch)
    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: None)

    result = installer.phase_verification(_context(tmp_path))

    assert result.status == "failed"
    assert result.details == "Liveness contract failed"
    assert calls == ["/liveness"] * 30
