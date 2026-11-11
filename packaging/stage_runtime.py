#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

REGISTRY = "https://registry.ollama.ai/v2"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
        relative = item.relative_to(path).as_posix().encode()
        digest.update(relative + b"\0")
        if item.is_symlink():
            digest.update(b"symlink\0" + os.readlink(item).encode() + b"\0")
        elif item.is_file():
            digest.update(b"file\0")
            digest.update(sha256_file(item).encode() + b"\0")
        elif item.is_dir():
            digest.update(b"directory\0")
    return digest.hexdigest()


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, **kwargs)


def build_venv(source_root: Path, destination: Path, python: str, install_root: Path) -> None:
    run([python, "-m", "venv", str(destination)])
    venv_python = destination / "bin" / "python"
    run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"])
    run([
        str(venv_python), "-m", "pip", "install", "--no-deps", "-r",
        str(source_root / "deploy" / "intel-xpu-requirements.txt"),
    ])
    run([str(venv_python), "-m", "pip", "install", "-r", str(source_root / "requirements.txt")])
    run([str(venv_python), "-m", "pip", "check"])
    lock = run([str(venv_python), "-m", "pip", "freeze", "--all"], capture_output=True, text=True)
    (destination.parent / "requirements.lock").write_text(lock.stdout, encoding="utf-8")
    relocate_venv(destination, install_root / "venv")


def relocate_venv(venv: Path, installed_venv: Path) -> None:
    for cache in sorted(venv.rglob("__pycache__"), reverse=True):
        shutil.rmtree(cache)
    for bytecode in venv.rglob("*.py[co]"):
        bytecode.unlink()
    old_prefix = str(venv).encode()
    new_prefix = str(installed_venv).encode()
    for item in venv.rglob("*"):
        if not item.is_file() or item.is_symlink() or item.stat().st_size > 16 * 1024 * 1024:
            continue
        raw = item.read_bytes()
        if old_prefix not in raw:
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"Build path remains in binary file {item}") from exc
        item.write_bytes(raw.replace(old_prefix, new_prefix))
    for item in venv.rglob("*.py"):
        with item.open("rb") as stream:
            prefix = stream.read(32)
        if prefix.startswith(b"#!/usr/bin/env/python3"):
            raw = item.read_bytes()
            item.write_bytes(raw.replace(b"#!/usr/bin/env/python3", b"#!/usr/bin/env python3", 1))
    (venv / ".gitignore").unlink(missing_ok=True)
    config = venv / "pyvenv.cfg"
    lines = config.read_text(encoding="utf-8").splitlines()
    config.write_text(
        "\n".join(line for line in lines if not line.startswith("command = ")) + "\n",
        encoding="utf-8",
    )


def copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".cache", "onnx", "*.lock", "__pycache__"),
    )


def stage_huggingface_models(
    source_root: Path,
    destination: Path,
    policy: dict,
    venv_python: Path,
) -> None:
    for name, model_policy in policy["models"].items():
        source = source_root / "models" / name
        target = destination / name
        if source.is_dir():
            copy_tree(source, target)
        else:
            code = (
                "from huggingface_hub import snapshot_download; "
                "snapshot_download(repo_id=" + repr(model_policy["repository"]) + ", "
                "revision=" + repr(model_policy["revision"]) + ", "
                "local_dir=" + repr(str(target)) + ")"
            )
            run([str(venv_python), "-c", code])
            for cache in sorted(target.rglob(".cache"), reverse=True):
                shutil.rmtree(cache)
        missing = [relative for relative in model_policy["required_files"] if not (target / relative).is_file()]
        if missing:
            raise RuntimeError(f"Model {name} is missing required files: {', '.join(missing)}")


def ollama_manifest_path(repository: str, tag: str) -> Path:
    parts = repository.split("/")
    if len(parts) != 2 or parts[0] != "library" or not parts[1]:
        raise ValueError("Only registry.ollama.ai/library models are supported")
    return Path("manifests") / "registry.ollama.ai" / "library" / parts[1] / tag


def blob_path(digest: str) -> Path:
    algorithm, value = digest.split(":", 1)
    if algorithm != "sha256" or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"Invalid Ollama blob digest: {digest}")
    return Path("blobs") / f"{algorithm}-{value}"


def fetch_to_file(url: str, destination: Path, expected_sha256: str) -> None:
    temporary = destination.with_name(destination.name + ".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as stream:
            for chunk in iter(lambda: response.read(8 * 1024 * 1024), b""):
                stream.write(chunk)
                digest.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if digest.hexdigest() != expected_sha256:
            raise RuntimeError(f"SHA-256 mismatch for {url}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    if sha256_file(source) != expected_sha256:
        raise RuntimeError(f"SHA-256 mismatch for {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def stage_ollama_model(destination: Path, policy: dict, source: Path | None) -> None:
    model_policy = policy["ollama"]
    repository = model_policy["repository"]
    tag = model_policy["tag"]
    relative_manifest = ollama_manifest_path(repository, tag)
    expected_manifest = model_policy["manifest_sha256"]
    target_manifest = destination / relative_manifest
    if source is not None and (source / relative_manifest).is_file():
        raw = (source / relative_manifest).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_manifest:
            raise RuntimeError("Ollama manifest does not match the pinned digest")
        target_manifest.parent.mkdir(parents=True, exist_ok=True)
        target_manifest.write_bytes(raw)
    else:
        fetch_to_file(f"{REGISTRY}/{repository}/manifests/{tag}", target_manifest, expected_manifest)
        raw = target_manifest.read_bytes()
    manifest = json.loads(raw)
    entries = [manifest["config"], *manifest["layers"]]
    for entry in entries:
        relative_blob = blob_path(entry["digest"])
        expected = entry["digest"].split(":", 1)[1]
        target = destination / relative_blob
        if target.is_file() and sha256_file(target) == expected:
            continue
        if source is not None and (source / relative_blob).is_file():
            copy_verified(source / relative_blob, target, expected)
        else:
            fetch_to_file(f"{REGISTRY}/{repository}/blobs/{entry['digest']}", target, expected)
        if target.stat().st_size != entry["size"]:
            raise RuntimeError(f"Size mismatch for {entry['digest']}")


def write_manifest(destination: Path, policy: dict) -> None:
    payload = {
        "format": 1,
        "policy": policy,
        "payload_sha256": {
            "venv": tree_sha256(destination / "venv"),
            "models": tree_sha256(destination / "models"),
            "ollama-models": tree_sha256(destination / "ollama-models"),
            "requirements.lock": sha256_file(destination / "requirements.lock"),
        },
    }
    (destination / "manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--ollama-source", type=Path)
    parser.add_argument("--skip-venv", action="store_true")
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    output_root = args.output_root.resolve()
    policy = json.loads((source_root / "packaging" / "runtime-policy.json").read_text())
    install_root = Path(policy["install_root"])
    output_root.mkdir(parents=True, exist_ok=True)
    if not args.skip_venv:
        build_venv(source_root, output_root / "venv", args.python, install_root)
    stage_huggingface_models(source_root, output_root / "models", policy, output_root / "venv" / "bin" / "python")
    stage_ollama_model(output_root / "ollama-models", policy, args.ollama_source)
    write_manifest(output_root, policy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
