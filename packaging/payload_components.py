#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile

GROUPS = ("venv", "models", "ollama-models")
DEFAULT_CHUNK_SIZE = 512 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        destination.mkdir(exist_ok=True)
    else:
        os.link(source, destination, follow_symlinks=False)


def archive_directory(directory: Path, destination: Path) -> None:
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(directory, arcname=directory.name)


def pack_payload(source_root: Path, main_root: Path, package: str, version: str, output_dir: Path, chunk_size: int) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    components = []
    split_entries = []
    for group in GROUPS:
        payload = source_root / "runtime" / group
        index = 0
        current_size = 0
        component_root = None
        component_name = None

        def finish_component() -> None:
            nonlocal component_root, component_name
            if component_root is not None:
                archive = output_dir / f"{package}_{version}.orig-{component_name}.tar.gz"
                archive_directory(component_root, archive)
                components.append(component_name)
                shutil.rmtree(component_root.parent)
                component_root = None
                component_name = None

        def start_component() -> None:
            nonlocal component_root, component_name, current_size, index
            component_name = f"runtime-{group}-{index:02d}"
            component_root = output_dir / "component-work" / component_name / f"{package}-{version}"
            component_root.mkdir(parents=True)
            index += 1
            current_size = 0

        for source in sorted(payload.rglob("*"), key=lambda path: path.as_posix()):
            relative = source.relative_to(payload)
            if component_root is None:
                start_component()
            if source.is_dir() and not source.is_symlink():
                (component_root / "runtime" / group / relative).mkdir(parents=True, exist_ok=True)
                continue
            if source.is_symlink():
                link_or_copy(source, component_root / "runtime" / group / relative)
                continue
            size = source.stat().st_size
            if size <= chunk_size:
                if current_size and current_size + size > chunk_size:
                    finish_component()
                    start_component()
                link_or_copy(source, component_root / "runtime" / group / relative)
                current_size += size
                continue
            entry = {"target": f"{group}/{relative.as_posix()}", "parts": [], "sha256": sha256_file(source), "mode": source.stat().st_mode & 0o777}
            with source.open("rb") as stream:
                part_index = 0
                while True:
                    data = stream.read(chunk_size)
                    if not data:
                        break
                    if current_size and current_size + len(data) > chunk_size:
                        finish_component()
                        start_component()
                    part_relative = Path(group) / f"{relative.as_posix()}.part{part_index:05d}"
                    destination = component_root / "runtime-parts" / part_relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                    entry["parts"].append(part_relative.as_posix())
                    current_size += len(data)
                    part_index += 1
            split_entries.append(entry)
        finish_component()
    manifest_path = main_root / "runtime" / "split-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"format": 1, "files": split_entries}, indent=2) + "\n")
    main_archive = output_dir / f"{package}_{version}.orig.tar.gz"
    archive_directory(main_root, main_archive)
    return components


def assemble_payload(source_root: Path) -> None:
    for group in GROUPS:
        for component in sorted(source_root.glob(f"runtime-{group}-*")):
            for prefix in ("runtime", "runtime-parts"):
                source = component / prefix / group
                if source.exists():
                    destination = source_root / prefix / group
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
            shutil.rmtree(component)
    manifest_path = source_root / "runtime" / "split-manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        for entry in manifest["files"]:
            target = source_root / "runtime" / entry["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".assemble")
            digest = hashlib.sha256()
            with temporary.open("wb") as stream:
                for part in entry["parts"]:
                    data = (source_root / "runtime-parts" / part).read_bytes()
                    stream.write(data)
                    digest.update(data)
                stream.flush()
                os.fsync(stream.fileno())
            if digest.hexdigest() != entry["sha256"]:
                raise RuntimeError(f"Payload checksum mismatch: {entry['target']}")
            os.chmod(temporary, entry["mode"])
            os.replace(temporary, target)
        shutil.rmtree(source_root / "runtime-parts")
    runtime_manifest = source_root / "runtime" / "manifest.json"
    if runtime_manifest.is_file():
        from stage_runtime import tree_sha256
        expected = json.loads(runtime_manifest.read_text())["payload_sha256"]
        for group in GROUPS:
            actual = tree_sha256(source_root / "runtime" / group)
            if actual != expected[group]:
                raise RuntimeError(f"Runtime manifest mismatch for {group}")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    pack = subparsers.add_parser("pack")
    pack.add_argument("--source-root", type=Path, required=True)
    pack.add_argument("--main-root", type=Path, required=True)
    pack.add_argument("--package", required=True)
    pack.add_argument("--version", required=True)
    pack.add_argument("--output-dir", type=Path, required=True)
    pack.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "pack":
        pack_payload(args.source_root, args.main_root, args.package, args.version, args.output_dir, args.chunk_size)
    else:
        assemble_payload(args.source_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
