#!/usr/bin/env python3
"""Fail-closed MCP configuration generator owned by local_optimize."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from config import settings

PROJECT_ROOT = settings.project_root.resolve()
PYTHON_EXEC = sys.executable
MCP_SERVER_SCRIPT = str(PROJECT_ROOT / "mcp_server.py")
LMSTUDIO_MCP_SCRIPT = str(PROJECT_ROOT / "lmstudio_rag_mcp.py")
CANONICAL_SERVER_NAME = "local-rag"
LEGACY_SERVER_NAME = "local_rag"


def load_existing_json(file_path: Path) -> dict[str, Any]:
    if not file_path.exists():
        return {}
    try:
        with file_path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except Exception as exc:
        raise ValueError(f"Corrupted JSON in {file_path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Root JSON element in {file_path} is not an object")
    return value


def _backup_path(file_path: Path) -> Path:
    return file_path.with_name(
        f"{file_path.name}.backup-{time.time_ns()}-{uuid.uuid4().hex}"
    )


def _write_backup(file_path: Path) -> Path:
    backup_path = _backup_path(file_path)
    fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(file_path.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    return backup_path


def atomic_write_json(file_path: Path, data: dict[str, Any], backup: bool = True) -> Path | None:
    """Atomically replace *file_path* and retain a collision-safe backup."""

    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    original_existed = file_path.exists()
    backup_path = _write_backup(file_path) if backup and original_existed else None
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=file_path.parent, delete=False, encoding="utf-8"
        ) as stream:
            tmp_path = Path(stream.name)
            os.chmod(tmp_path, 0o600)
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        with tmp_path.open("r", encoding="utf-8") as check:
            loaded = json.load(check)
        if loaded != data:
            raise ValueError("Temporary MCP configuration failed semantic verification")

        os.replace(tmp_path, file_path)
        tmp_path = None
        os.chmod(file_path, 0o600)
        directory_fd = os.open(file_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return backup_path
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        # os.replace is the commit point. If a post-replace check ever fails,
        # restore from a retained backup or remove a newly-created target.
        if backup_path is not None and backup_path.exists():
            restore_tmp = file_path.with_name(f".{file_path.name}.restore-{uuid.uuid4().hex}")
            try:
                restore_tmp.write_bytes(backup_path.read_bytes())
                os.chmod(restore_tmp, 0o600)
                os.replace(restore_tmp, file_path)
            finally:
                restore_tmp.unlink(missing_ok=True)
        elif not original_existed:
            file_path.unlink(missing_ok=True)
        raise


def _owned_entry(*, transport: bool = False, opencode: bool = False) -> dict[str, Any]:
    raise RuntimeError(
        "Refusing to expose RAG MCP to a cloud-capable agent client. "
        "Use a separately isolated local-only LM Studio connection; cloud handoff "
        "must not inherit RAG tools or conversation history."
    )


def _lmstudio_entry() -> dict[str, Any]:
    return {"command": PYTHON_EXEC, "args": [LMSTUDIO_MCP_SCRIPT]}


def _is_owned_legacy_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    args = entry.get("args")
    if not isinstance(args, list) or len(args) != 1 or not isinstance(entry.get("command"), str):
        return False
    try:
        target = Path(str(args[0])).resolve()
    except OSError:
        return False
    owned_scripts = {
        Path(MCP_SERVER_SCRIPT).resolve(),
        Path(LMSTUDIO_MCP_SCRIPT).resolve(),
    }
    return target in owned_scripts


def merge_owned_server(existing: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(existing)
    servers = result.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers must be an object")

    canonical = servers.get(CANONICAL_SERVER_NAME)
    if canonical is not None and not _is_owned_legacy_entry(canonical):
        raise ValueError(f"MCP entry '{CANONICAL_SERVER_NAME}' is not owned by local_optimize")

    legacy = servers.get(LEGACY_SERVER_NAME)
    if legacy is not None:
        if not _is_owned_legacy_entry(legacy):
            raise ValueError(f"Legacy MCP entry '{LEGACY_SERVER_NAME}' is not owned by local_optimize")
        del servers[LEGACY_SERVER_NAME]

    servers[CANONICAL_SERVER_NAME] = copy.deepcopy(entry)
    result["mcpServers"] = servers
    return result


def _build(path: Path, entry: dict[str, Any], dry_run: bool, backup: bool) -> dict[str, Any]:
    return _build_many([path], entry, dry_run, backup)


def _restore_snapshot(file_path: Path, original: bytes | None) -> None:
    if original is None:
        file_path.unlink(missing_ok=True)
        return
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=file_path.parent, delete=False) as stream:
            tmp_path = Path(stream.name)
            os.chmod(tmp_path, 0o600)
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, file_path)
        tmp_path = None
        os.chmod(file_path, 0o600)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _build_many(
    paths: list[Path], entry: dict[str, Any], dry_run: bool, backup: bool
) -> dict[str, Any]:
    targets = [Path(path) for path in paths]
    merged_configs = [merge_owned_server(load_existing_json(path), entry) for path in targets]
    if dry_run:
        return merged_configs[-1] if merged_configs else {}

    snapshots = {path: path.read_bytes() if path.exists() else None for path in targets}
    committed: list[Path] = []
    try:
        for path, merged in zip(targets, merged_configs, strict=True):
            atomic_write_json(path, merged, backup=backup)
            committed.append(path)
    except Exception as original_error:
        rollback_errors: list[str] = []
        for path in reversed(committed):
            try:
                _restore_snapshot(path, snapshots[path])
            except Exception as rollback_error:
                rollback_errors.append(rollback_error.__class__.__name__)
        if rollback_errors:
            raise RuntimeError(
                "MCP configuration failed and partial rollback also failed: "
                + ", ".join(rollback_errors)
            ) from original_error
        raise
    return merged_configs[-1] if merged_configs else {}


def build_cursor_mcp_config(
    target_path: Path | None = None, dry_run: bool = False, backup: bool = True
) -> dict[str, Any]:
    paths = [target_path] if target_path else [
        Path.home() / ".cursor" / "mcp.json",
        Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "mcp.json",
        Path.home() / ".config" / "cursor" / "mcp.json",
    ]
    target_paths = [p for p in paths if p.parent.exists()] or [paths[0]]
    return _build_many([Path(p) for p in target_paths], _owned_entry(), dry_run, backup)


def build_antigravity_mcp_config(
    target_path: Path | None = None, dry_run: bool = False, backup: bool = True
) -> dict[str, Any]:
    paths = [target_path] if target_path else [
        Path.home() / ".antigravity" / "mcp_config.json",
        Path.home() / ".gemini" / "antigravity" / "mcp_config.json",
        Path.home() / ".gemini" / "antigravity-cli" / "mcp_config.json",
        Path.home() / ".config" / "Antigravity" / "mcp_config.json",
    ]
    target_paths = [p for p in paths if p.parent.exists()] or [paths[0]]
    return _build_many(
        [Path(path) for path in target_paths], _owned_entry(transport=True), dry_run, backup
    )


def build_gemini_mcp_config(
    target_path: Path | None = None, dry_run: bool = False, backup: bool = True
) -> dict[str, Any]:
    paths = [target_path] if target_path else [
        Path.home() / ".gemini" / "mcp.json",
        Path.home() / ".gemini" / "antigravity" / "mcp_config.json",
    ]
    target_paths = [p for p in paths if p.parent.exists()] or [paths[0]]
    return _build_many([Path(path) for path in target_paths], _owned_entry(), dry_run, backup)


def build_opencode_mcp_config(
    target_path: Path | None = None, dry_run: bool = False, backup: bool = True
) -> dict[str, Any]:
    """Generate mcp.json for OpenCode.

    Note: As of OpenCode 2026-07, the client reads MCP servers from opencode.json
    under the "mcp" key, not from a separate mcp.json. This function writes to the
    legacy location for backward compatibility; users must manually merge into
    opencode.json if they want OpenCode to see local-rag.
    """
    paths = [target_path] if target_path else [
        Path.home() / ".config" / "opencode" / "mcp.json",
        Path.home() / ".opencode" / "mcp.json",
    ]
    target_paths = [p for p in paths if p.parent.exists()] or [paths[0]]
    result = _build_many(
        [Path(path) for path in target_paths], _owned_entry(opencode=True), dry_run, backup
    )
    if not dry_run and target_paths:
        import sys
        print(
            "Warning: OpenCode reads MCP servers from opencode.json (not mcp.json).\n"
            "Merge local-rag manually into ~/.config/opencode/opencode.json under 'mcp'.",
            file=sys.stderr,
        )
    return result


def build_mimocode_mcp_config(
    target_path: Path | None = None, dry_run: bool = False, backup: bool = True
) -> dict[str, Any]:
    paths = [target_path] if target_path else [
        Path.home() / ".mimocode" / "mcp.json",
        Path.home() / ".config" / "mimocode" / "mcp.json",
    ]
    target_paths = [p for p in paths if p.parent.exists()] or [paths[0]]
    return _build_many([Path(path) for path in target_paths], _owned_entry(), dry_run, backup)


def build_cagent_mcp_config(
    target_path: Path | None = None, dry_run: bool = False, backup: bool = True
) -> dict[str, Any]:
    paths = [target_path] if target_path else [
        Path.home() / ".config" / "cagent" / "mcp.json",
        Path.home() / ".cagent" / "mcp.json",
    ]
    target_paths = [p for p in paths if p.parent.exists()] or [paths[0]]
    return _build_many([Path(path) for path in target_paths], _owned_entry(), dry_run, backup)


def build_kimi_mcp_config(
    target_path: Path | None = None, dry_run: bool = False, backup: bool = True
) -> dict[str, Any]:
    paths = [target_path] if target_path else [
        Path.home() / ".kimi-code" / "mcp.json",
    ]
    target_paths = [p for p in paths if p.parent.exists()] or [paths[0]]
    return _build_many([Path(path) for path in target_paths], _owned_entry(), dry_run, backup)


def build_vscode_mcp_config(
    target_path: Path | None = None, dry_run: bool = False, backup: bool = True
) -> dict[str, Any]:
    paths = [target_path] if target_path else [
        Path.home() / ".config" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        Path.home() / ".config" / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings" / "cline_mcp_settings.json",
    ]
    target_paths = [p for p in paths if p.parent.exists()]
    if not target_paths:
        return {}
    return _build_many([Path(path) for path in target_paths], _owned_entry(), dry_run, backup)


def build_claude_mcp_config(
    target_path: Path | None = None, dry_run: bool = False, backup: bool = True
) -> dict[str, Any]:
    paths = [target_path] if target_path else [
        Path.home() / ".claude" / "mcp.json",
        Path.home() / ".config" / "Claude" / "claude_desktop_config.json",
    ]
    target_paths = [p for p in paths if p.parent.exists()] or [paths[0]]
    return _build_many([Path(path) for path in target_paths], _owned_entry(), dry_run, backup)


def build_lmstudio_mcp_config(
    target_path: Path | None = None, dry_run: bool = False, backup: bool = True
) -> dict[str, Any]:
    path = Path(target_path) if target_path else Path.home() / ".lmstudio" / "mcp.json"
    return _build(path, _lmstudio_entry(), dry_run, backup)


def _cloud_client_mcp_paths() -> list[Path]:
    home = Path.home()
    return [
        home / ".cursor" / "mcp.json",
        home / ".antigravity" / "mcp_config.json",
        home / ".gemini" / "mcp.json",
        home / ".gemini" / "antigravity" / "mcp_config.json",
        home / ".config" / "opencode" / "mcp.json",
        home / ".opencode" / "mcp.json",
        home / ".mimocode" / "mcp.json",
        home / ".config" / "mimocode" / "mcp.json",
        home / ".config" / "cagent" / "mcp.json",
        home / ".cagent" / "mcp.json",
        home / ".kimi-code" / "mcp.json",
        home / ".config" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        home / ".config" / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings" / "cline_mcp_settings.json",
        home / ".claude" / "mcp.json",
        home / ".config" / "Claude" / "claude_desktop_config.json",
    ]


def prune_cloud_client_configs(
    paths: list[Path] | None = None, *, dry_run: bool = False, backup: bool = True
) -> dict[str, list[str]]:
    pruned: dict[str, list[str]] = {}
    for candidate in (paths if paths is not None else _cloud_client_mcp_paths()):
        file_path = Path(candidate)
        if not file_path.is_file():
            continue
        data = load_existing_json(file_path)
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        removed = [
            key
            for key in (CANONICAL_SERVER_NAME, LEGACY_SERVER_NAME)
            if key in servers and _is_owned_legacy_entry(servers[key])
        ]
        if not removed:
            continue
        if not dry_run:
            updated = copy.deepcopy(data)
            for key in removed:
                del updated["mcpServers"][key]
            atomic_write_json(file_path, updated, backup=backup)
        pruned[str(file_path)] = removed
    return pruned


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate local_optimize MCP entries")
    parser.add_argument(
        "--client",
        choices=["cursor", "antigravity", "gemini", "opencode", "mimocode", "cagent", "kimi", "vscode", "claude", "lmstudio", "all"],
        default=None,
    )
    parser.add_argument("--lmstudio", action="store_true", help="Write local-only LM Studio MCP config")
    parser.add_argument("--prune-cloud-clients", action="store_true", help="Remove owned local-rag entries from cloud client configs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    builders = {
        "cursor": build_cursor_mcp_config,
        "antigravity": build_antigravity_mcp_config,
        "gemini": build_gemini_mcp_config,
        "opencode": build_opencode_mcp_config,
        "mimocode": build_mimocode_mcp_config,
        "cagent": build_cagent_mcp_config,
        "kimi": build_kimi_mcp_config,
        "vscode": build_vscode_mcp_config,
        "claude": build_claude_mcp_config,
        "lmstudio": build_lmstudio_mcp_config,
    }
    if args.prune_cloud_clients:
        try:
            prune_cloud_client_configs(dry_run=args.dry_run, backup=not args.no_backup)
        except Exception as exc:
            print(f"[Error] cloud MCP cleanup failed: {exc}", file=sys.stderr)
            return 1

    if args.lmstudio and args.client is None:
        selected = {"lmstudio": build_lmstudio_mcp_config}
    elif args.prune_cloud_clients and args.client is None:
        selected = {}
    else:
        target_client = args.client or "all"
        selected = builders if target_client == "all" else {target_client: builders[target_client]}

    for name, builder in selected.items():
        try:
            builder(dry_run=args.dry_run, backup=not args.no_backup)
        except Exception as exc:
            print(f"[Error] {name} MCP configuration failed: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
