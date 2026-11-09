"""Installation journal, backup manifest, and idempotent file restoration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from atomic_files import atomic_write_bytes


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ManagedTarget:
    name: str
    path: Path


@dataclass(frozen=True)
class BackupRecord:
    name: str
    target: Path
    existed: bool
    backup_path: Path | None = None
    mode: int | None = None
    size: int | None = None
    sha256: str | None = None


@dataclass
class InstallationContext:
    dry_run: bool
    backup_root: Path
    targets: tuple[ManagedTarget, ...] = ()
    backup_dir: Path | None = None
    records: dict[Path, BackupRecord] = field(default_factory=dict)
    applied_rollbacks: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    rollback_errors: list[str] = field(default_factory=list)
    committed_changes: list[str] = field(default_factory=list)

    def create_backup(self) -> None:
        if self.dry_run:
            return
        if self.backup_dir is not None:
            raise RuntimeError("Backup already created")
        self.backup_dir = self.backup_root / f"{time.time_ns()}-{uuid.uuid4().hex}"
        self.backup_dir.mkdir(parents=True, mode=0o700)
        os.chmod(self.backup_dir, 0o700)

        manifest_records: list[dict[str, object]] = []
        absent_targets: list[dict[str, str]] = []
        seen: set[Path] = set()
        for index, managed in enumerate(self.targets):
            target = managed.path.expanduser().resolve(strict=False)
            if target in seen:
                continue
            seen.add(target)
            if target.exists():
                if not target.is_file():
                    raise RuntimeError(f"Managed target is not a regular file: {managed.name}")
                snapshot = self.backup_dir / "files" / f"{index:04d}.snapshot"
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, snapshot)
                os.chmod(snapshot, 0o600)
                mode = stat.S_IMODE(target.stat().st_mode)
                digest = file_sha256(snapshot)
                record = BackupRecord(
                    managed.name, target, True, snapshot, mode, target.stat().st_size, digest
                )
                manifest_records.append(
                    {
                        "name": managed.name,
                        "target": str(target),
                        "backup": str(snapshot.relative_to(self.backup_dir)),
                        "mode": mode,
                        "size": target.stat().st_size,
                        "sha256": digest,
                    }
                )
            else:
                record = BackupRecord(managed.name, target, False)
                absent_targets.append({"name": managed.name, "target": str(target)})
            self.records[target] = record

        manifest = {
            "version": 1,
            "files": manifest_records,
            "absent_targets": absent_targets,
        }
        payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        manifest_path = self.backup_dir / "manifest.json"
        atomic_write_bytes(manifest_path, payload, 0o600)
        atomic_write_bytes(
            self.backup_dir / "manifest.sha256",
            hashlib.sha256(payload).hexdigest().encode("ascii") + b"\n",
            0o600,
        )
        self.verify_backup()

    def verify_backup(self) -> None:
        if self.dry_run:
            return
        if self.backup_dir is None:
            raise RuntimeError("Mandatory backup has not been created")
        manifest_path = self.backup_dir / "manifest.json"
        checksum_path = self.backup_dir / "manifest.sha256"
        payload = manifest_path.read_bytes()
        expected = checksum_path.read_text(encoding="ascii").strip()
        if hashlib.sha256(payload).hexdigest() != expected:
            raise RuntimeError("Backup manifest integrity check failed")
        parsed = json.loads(payload)
        if parsed.get("version") != 1:
            raise RuntimeError("Unsupported backup manifest version")
        for record in self.records.values():
            if record.existed:
                if record.backup_path is None or not record.backup_path.is_file():
                    raise RuntimeError(f"Missing snapshot for managed target: {record.name}")
                if file_sha256(record.backup_path) != record.sha256:
                    raise RuntimeError(f"Snapshot checksum mismatch: {record.name}")

    def require_backup(self) -> None:
        if not self.dry_run:
            self.verify_backup()

    def restore_target(self, path: Path) -> None:
        target = path.expanduser().resolve(strict=False)
        record = self.records.get(target)
        if record is None:
            raise RuntimeError(f"No backup record for managed target: {target.name}")
        if record.existed:
            assert record.backup_path is not None and record.mode is not None
            atomic_write_bytes(target, record.backup_path.read_bytes(), record.mode)
        else:
            target.unlink(missing_ok=True)

    def register_rollback(self, name: str, action: Callable[[], None]) -> None:
        self.applied_rollbacks.append((name, action))

    def record_committed_change(self, description: str) -> None:
        self.committed_changes.append(description)

    def rollback_all(self) -> None:
        for name, action in reversed(self.applied_rollbacks):
            try:
                action()
            except Exception as exc:
                self.rollback_errors.append(f"{name}: {exc}")


def deduplicate_targets(targets: Iterable[ManagedTarget]) -> tuple[ManagedTarget, ...]:
    result: list[ManagedTarget] = []
    seen: set[Path] = set()
    for item in targets:
        path = item.path.expanduser().resolve(strict=False)
        if path not in seen:
            seen.add(path)
            result.append(ManagedTarget(item.name, path))
    return tuple(result)
