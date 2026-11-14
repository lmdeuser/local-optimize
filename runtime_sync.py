#!/usr/bin/env python3
"""Bootstrap and synchronize the local Python XPU runtime and model stores."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, TypeVar
import urllib.error
import urllib.parse
import urllib.request

T = TypeVar("T")

DEFAULT_SYSTEM_ROOT = Path("/var/lib/local-optimize")
DEFAULT_PIP_CACHE_DIR = Path("/var/cache/local-optimize/pip")
PYTORCH_XPU_INDEX_URL = "https://download.pytorch.org/whl/xpu"
PYPI_INDEX_URL = "https://pypi.org/simple"
HF_API_BASE = "https://huggingface.co/api/models"
OLLAMA_REGISTRY_BASE = "https://registry.ollama.ai/v2"
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_system_runtime_root() -> Path:
    configured = os.getenv("LOCAL_OPTIMIZE_SYSTEM_DIR", str(DEFAULT_SYSTEM_ROOT)).strip()
    return Path(configured or str(DEFAULT_SYSTEM_ROOT)).expanduser()


def get_system_venv_dir() -> Path:
    return get_system_runtime_root() / "venv"


def get_system_models_dir() -> Path:
    return get_system_runtime_root() / "models"


def get_pip_cache_dir() -> Path:
    configured = os.getenv("LOCAL_OPTIMIZE_PIP_CACHE_DIR", str(DEFAULT_PIP_CACHE_DIR)).strip()
    return Path(configured or str(DEFAULT_PIP_CACHE_DIR)).expanduser()


def _is_git_checkout(project_root: Path) -> bool:
    return (project_root / ".git").exists()


def allowed_model_roots(project_root: Path) -> tuple[Path, ...]:
    roots: list[Path] = [project_root / "models", get_system_models_dir()]
    env_dir = os.getenv("LOCAL_OPTIMIZE_MODELS_DIR", "").strip()
    if env_dir:
        roots.append(Path(env_dir).expanduser())
    return tuple(roots)


def resolve_models_base_dir(project_root: Path) -> Path:
    env_dir = os.getenv("LOCAL_OPTIMIZE_MODELS_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    if _is_git_checkout(project_root):
        return project_root / "models"
    return get_system_models_dir()


_PACKAGED_APP_ROOT = Path("/usr/lib/local-optimize")


def resolve_model_dir(name: str, project_root: Path) -> Path:
    env_dir = os.getenv("LOCAL_OPTIMIZE_MODELS_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser() / name
    checkout_candidate = project_root / "models" / name
    system_candidate = get_system_models_dir() / name
    if project_root != _PACKAGED_APP_ROOT and checkout_candidate.is_dir():
        return checkout_candidate
    if system_candidate.is_dir():
        return system_candidate
    if checkout_candidate.is_dir():
        return checkout_candidate
    return checkout_candidate if _is_git_checkout(project_root) else system_candidate


def resolve_evidence_model_path(project_root: Path, configured_path: str) -> Path:
    raw = (configured_path or "").strip()
    if not raw:
        raise ValueError("Evidence model path must not be empty")
    raw_path = Path(raw)
    if ".." in raw_path.parts:
        raise ValueError("Evidence model path traversal is forbidden")
    if raw_path.is_absolute():
        candidate = raw_path
    elif raw_path.parts and raw_path.parts[0] == "models" and len(raw_path.parts) > 1:
        subpath = Path(*raw_path.parts[1:]).as_posix()
        candidate = resolve_model_dir(subpath, project_root)
    else:
        candidate = project_root / raw_path
    resolved = candidate.resolve(strict=True)
    if not any(
        resolved.is_relative_to(root.resolve(strict=False))
        for root in allowed_model_roots(project_root)
    ):
        raise ValueError("Evidence model must be inside an allowed local models directory")
    return resolved


def resolve_runtime_policy_path(project_root: Path) -> Path:
    for candidate in (
        project_root / "packaging" / "runtime-policy.json",
        project_root / "runtime" / "runtime-policy.json",
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("runtime-policy.json not found")


def resolve_requirements_lock_path(project_root: Path) -> Path | None:
    for candidate in (
        project_root / "runtime" / "requirements.lock",
        project_root / "requirements.lock",
    ):
        if candidate.is_file():
            return candidate
    return None


def resolve_venv_dir(project_root: Path) -> Path:
    env_venv = os.getenv("LOCAL_OPTIMIZE_VENV", "").strip()
    if env_venv:
        return Path(env_venv).expanduser()
    for candidate in (
        project_root / ".venv",
        project_root / "venv",
        get_system_venv_dir(),
    ):
        if (candidate / "bin" / "python").is_file():
            return candidate
    return project_root / ".venv" if _is_git_checkout(project_root) else get_system_venv_dir()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _ensure_readable_tree(root: Path) -> None:
    if os.geteuid() != 0 or not root.exists():
        return
    os.chmod(root, 0o755)
    for item in root.rglob("*"):
        if item.is_symlink():
            continue
        if item.is_dir():
            os.chmod(item, 0o755)
        elif item.is_file():
            current = item.stat().st_mode & 0o777
            desired = (current | 0o444) & ~0o022
            if current & 0o100:
                desired |= 0o111
            if desired != current:
                os.chmod(item, desired)


def _run_with_retries(
    action: Callable[[], T],
    *,
    attempts: int = 4,
    delays: tuple[int, ...] = (5, 10, 20),
    label: str,
) -> T:
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return action()
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            delay = delays[min(attempt - 1, len(delays) - 1)]
            print(
                f"[runtime_sync] {label} attempt {attempt}/{attempts} failed: {exc}; retrying in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def _expected_venv_hash(project_root: Path) -> tuple[str, Path | None]:
    lock_path = resolve_requirements_lock_path(project_root)
    if lock_path is not None:
        return sha256_file(lock_path), lock_path
    digest = hashlib.sha256()
    for rel in ("deploy/intel-xpu-requirements.txt", "requirements.txt"):
        req = project_root / rel
        if req.is_file():
            digest.update(rel.encode("utf-8") + b"\0" + req.read_bytes() + b"\0")
    return digest.hexdigest(), None


def _read_pinned_pip_spec(lock_path: Path | None) -> str | None:
    if lock_path is None:
        return None
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("pip=="):
            return stripped
    return None


def _venv_matches_lock(venv_dir: Path, expected_hash: str) -> bool:
    venv_python = venv_dir / "bin" / "python"
    marker = venv_dir / ".lock-sha256"
    if not venv_python.is_file() or not marker.is_file():
        return False
    if marker.read_text(encoding="utf-8").strip() != expected_hash:
        return False
    probe = subprocess.run(
        [str(venv_python), "-c", "import sys; assert sys.version_info[:2] == (3, 14)"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


def sync_venv(
    project_root: Path,
    *,
    venv_dir: Path | None = None,
    pip_cache_dir: Path | None = None,
    force: bool = False,
    check_only: bool = False,
    attempts: int = 4,
) -> dict[str, Any]:
    target_venv = venv_dir or resolve_venv_dir(project_root)
    cache_dir = pip_cache_dir or get_pip_cache_dir()
    expected_hash, lock_path = _expected_venv_hash(project_root)

    if not force and _venv_matches_lock(target_venv, expected_hash):
        return {
            "status": "up_to_date",
            "venv": str(target_venv),
            "lock_sha256": expected_hash,
        }

    if check_only:
        status = "outdated" if (target_venv / "bin" / "python").is_file() else "missing"
        return {
            "status": status,
            "venv": str(target_venv),
            "lock_sha256": expected_hash,
        }

    if sys.version_info[:2] != (3, 14):
        raise RuntimeError(
            f"Python 3.14 is required to provision {target_venv}, got {sys.version_info[0]}.{sys.version_info[1]}"
        )

    venv_python = target_venv / "bin" / "python"
    if venv_python.is_file():
        probe = subprocess.run(
            [str(venv_python), "-c", "import sys; assert sys.version_info[:2] == (3, 14)"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            shutil.rmtree(target_venv)

    if not venv_python.is_file():
        target_venv.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", str(target_venv)], check=True)

    cache_dir.mkdir(parents=True, exist_ok=True)
    common_pip = [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--retries",
        "10",
        "--timeout",
        "60",
        "--cache-dir",
        str(cache_dir),
    ]

    def install_step() -> None:
        pip_spec = _read_pinned_pip_spec(lock_path) or "pip"
        subprocess.run([*common_pip, "--upgrade", pip_spec], check=True)
        if lock_path is not None:
            subprocess.run(
                [
                    *common_pip,
                    "--no-deps",
                    "--index-url",
                    PYTORCH_XPU_INDEX_URL,
                    "--extra-index-url",
                    PYPI_INDEX_URL,
                    "-r",
                    str(lock_path),
                ],
                check=True,
            )
        else:
            subprocess.run(
                [*common_pip, "--no-deps", "-r", str(project_root / "deploy" / "intel-xpu-requirements.txt")],
                check=True,
            )
            subprocess.run(
                [*common_pip, "-r", str(project_root / "requirements.txt")],
                check=True,
            )
        subprocess.run([str(venv_python), "-m", "pip", "check"], check=True)

    _run_with_retries(install_step, attempts=attempts, label="pip runtime install")
    _atomic_write_text(target_venv / ".lock-sha256", expected_hash + "\n")
    _ensure_readable_tree(target_venv)
    return {
        "status": "updated",
        "venv": str(target_venv),
        "lock_sha256": expected_hash,
    }


def _validate_hf_repo_id(repo_id: str) -> str:
    cleaned = (repo_id or "").strip()
    if not _REPO_ID_RE.match(cleaned) or ".." in cleaned:
        raise ValueError(f"Invalid HuggingFace repository id: {repo_id!r}")
    return cleaned


def fetch_hf_upstream_info(repo_id: str, timeout: float = 15.0) -> dict[str, Any]:
    validated = _validate_hf_repo_id(repo_id)
    url = f"{HF_API_BASE}/{validated}?blobs=true"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "local-optimize-sync/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(2_000_000).decode("utf-8"))
    sha = str(payload.get("sha") or "").strip()
    if not sha:
        raise ValueError(f"Missing commit sha in HuggingFace metadata for {validated}")
    weights_sha256 = ""
    for sibling in payload.get("siblings") or []:
        if isinstance(sibling, dict) and sibling.get("rfilename") == "model.safetensors":
            lfs = sibling.get("lfs")
            if isinstance(lfs, dict) and isinstance(lfs.get("sha256"), str):
                weights_sha256 = lfs["sha256"].strip().lower()
                break
    return {"repository": validated, "sha": sha, "weights_sha256": weights_sha256}


def _read_model_state(model_dir: Path) -> dict[str, Any]:
    state_file = model_dir / ".model-state.json"
    if not state_file.is_file():
        return {}
    try:
        parsed = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _has_required_files(model_dir: Path, required_files: list[str]) -> bool:
    return model_dir.is_dir() and all((model_dir / rel).is_file() for rel in required_files)


def _download_hf_snapshot(venv_python: Path, repo_id: str, revision: str, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    code = (
        "from huggingface_hub import snapshot_download; "
        f"snapshot_download(repo_id={repo_id!r}, revision={revision!r}, "
        f"local_dir={str(target_dir)!r}, ignore_patterns=['onnx/*', '*.onnx'])"
    )
    subprocess.run([str(venv_python), "-c", code], check=True)


def _is_embedding_model(name: str, model_policy: dict[str, Any]) -> bool:
    role = str(model_policy.get("role") or "").strip().lower()
    return role == "embedding" or name == "multilingual-e5-small"


def sync_huggingface_models(
    project_root: Path,
    *,
    models_dir: Path | None = None,
    venv_python: Path | None = None,
    pinned: bool = False,
    check_only: bool = False,
    allow_embedding_update: bool = False,
    force: bool = False,
    attempts: int = 4,
    upstream_fetcher: Callable[[str], dict[str, Any]] = fetch_hf_upstream_info,
) -> dict[str, dict[str, Any]]:
    policy = json.loads(resolve_runtime_policy_path(project_root).read_text(encoding="utf-8"))
    base_dir = models_dir or resolve_models_base_dir(project_root)
    python_bin = venv_python or (resolve_venv_dir(project_root) / "bin" / "python")
    results: dict[str, dict[str, Any]] = {}

    for name, model_policy in policy["models"].items():
        repo_id = _validate_hf_repo_id(model_policy["repository"])
        pinned_rev = str(model_policy["revision"]).strip()
        required_files = list(model_policy["required_files"])
        target_dir = base_dir / name
        local_ready = _has_required_files(target_dir, required_files)
        state = _read_model_state(target_dir)
        local_rev = str(state.get("revision") or "").strip()
        local_weights_sha = str(state.get("weights_sha256") or "").strip().lower()

        desired_rev = pinned_rev
        desired_weights_sha = ""
        upstream_error: str | None = None

        if not pinned:
            try:
                upstream = upstream_fetcher(repo_id)
                desired_rev = str(upstream.get("sha") or "").strip() or pinned_rev
                desired_weights_sha = str(upstream.get("weights_sha256") or "").strip().lower()
            except Exception as exc:
                upstream_error = str(exc)
                if local_ready and not force:
                    print(
                        f"[runtime_sync] Warning: could not check upstream for {repo_id} ({exc}); keeping local copy.",
                        file=sys.stderr,
                    )
                    results[name] = {
                        "status": "offline_kept",
                        "repository": repo_id,
                        "revision": local_rev or pinned_rev,
                        "path": str(target_dir),
                    }
                    continue

        if local_ready and not force and local_rev == desired_rev:
            results[name] = {
                "status": "up_to_date",
                "repository": repo_id,
                "revision": local_rev,
                "path": str(target_dir),
            }
            continue

        if (
            local_ready
            and _is_embedding_model(name, model_policy)
            and desired_weights_sha
            and not allow_embedding_update
        ):
            if not local_weights_sha and (target_dir / "model.safetensors").is_file():
                local_weights_sha = sha256_file(target_dir / "model.safetensors")
            if local_weights_sha and local_weights_sha != desired_weights_sha:
                print(
                    f"[runtime_sync] Warning: upstream weights changed for {repo_id}; "
                    "skipping overwrite to protect pgvector embeddings. "
                    "Pass --allow-embedding-update and run reembed_corpus.py to migrate.",
                    file=sys.stderr,
                )
                results[name] = {
                    "status": "held_embedding_weights",
                    "repository": repo_id,
                    "revision": local_rev,
                    "upstream_revision": desired_rev,
                    "path": str(target_dir),
                }
                continue

        if check_only:
            results[name] = {
                "status": "update_available" if local_ready else "missing",
                "repository": repo_id,
                "local_revision": local_rev or None,
                "target_revision": desired_rev,
                "path": str(target_dir),
                **({"upstream_error": upstream_error} if upstream_error else {}),
            }
            continue

        if not python_bin.is_file():
            raise RuntimeError(f"Python environment not found for HuggingFace download: {python_bin}")

        _run_with_retries(
            lambda: _download_hf_snapshot(python_bin, repo_id, desired_rev, target_dir),
            attempts=attempts,
            label=f"HuggingFace download {repo_id}",
        )
        missing = [rel for rel in required_files if not (target_dir / rel).is_file()]
        if missing:
            raise RuntimeError(f"Model {name} is missing required files after download: {', '.join(missing)}")

        shutil.rmtree(target_dir / ".cache", ignore_errors=True)
        actual_weights_sha = (
            sha256_file(target_dir / "model.safetensors")
            if (target_dir / "model.safetensors").is_file()
            else desired_weights_sha
        )
        pinned_weights_sha = str(
            model_policy.get("weights_sha256") or model_policy.get("sha256") or ""
        ).strip().lower()
        expected_weights_sha = pinned_weights_sha or desired_weights_sha
        if (
            (target_dir / "model.safetensors").is_file()
            and expected_weights_sha
            and actual_weights_sha != expected_weights_sha
        ):
            raise RuntimeError(
                f"Model {name} weights SHA-256 mismatch: expected {expected_weights_sha}, got {actual_weights_sha}"
            )
        file_sha256s = model_policy.get("file_sha256s") or {}
        if isinstance(file_sha256s, dict):
            for rel_path, expected_sha in file_sha256s.items():
                artifact = target_dir / str(rel_path)
                if not artifact.is_file():
                    raise RuntimeError(f"Model {name} missing checksum-pinned file: {rel_path}")
                actual_file_sha = sha256_file(artifact).lower()
                if actual_file_sha != str(expected_sha).strip().lower():
                    raise RuntimeError(
                        f"Model {name} file {rel_path} SHA-256 mismatch: expected {expected_sha}, got {actual_file_sha}"
                    )
        state_payload = {
            "format": 1,
            "repository": repo_id,
            "revision": desired_rev,
            "weights_sha256": actual_weights_sha,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_text(
            target_dir / ".model-state.json",
            json.dumps(state_payload, indent=2, sort_keys=True) + "\n",
        )
        _ensure_readable_tree(target_dir)
        results[name] = {
            "status": "updated" if local_ready else "installed",
            "repository": repo_id,
            "revision": desired_rev,
            "path": str(target_dir),
        }

    return results


def local_ollama_url(path: str, host: str | None = None) -> str:
    configured = (host if host is not None else os.getenv("OLLAMA_HOST", "127.0.0.1:11434")).strip()
    parsed = urllib.parse.urlsplit(configured if "://" in configured else f"http://{configured}")
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("OLLAMA_HOST must be a direct HTTP loopback endpoint")
    return urllib.parse.urlunsplit(("http", parsed.netloc, path, "", ""))


def _normalize_digest(value: str | None) -> str:
    text = (value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1]
    return text if _SHA256_RE.match(text) else ""


def _ollama_json_request(path: str, payload: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    url = local_ollama_url(path)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with _DIRECT_OPENER.open(request, timeout=timeout) as response:
        parsed = json.loads(response.read(2_000_000).decode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Invalid Ollama response from {path}")
    return parsed


def _ollama_local_digest(model: str) -> str:
    tags = _ollama_json_request("/api/tags")
    for item in tags.get("models") or []:
        if isinstance(item, dict) and model in {item.get("name"), item.get("model")}:
            return _normalize_digest(str(item.get("digest") or ""))
    return ""


def fetch_ollama_upstream_digest(repository: str, tag: str, timeout: float = 15.0) -> str:
    parts = repository.split("/")
    if len(parts) != 2 or parts[0] != "library" or not parts[1]:
        raise ValueError(f"Unsupported Ollama repository: {repository!r}")
    url = f"{OLLAMA_REGISTRY_BASE}/{repository}/manifests/{tag}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.docker.distribution.manifest.v2+json",
            "User-Agent": "local-optimize-sync/1.0",
        },
        method="HEAD",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        digest = response.headers.get("ollama-content-digest") or response.headers.get("docker-content-digest")
    normalized = _normalize_digest(digest)
    if not normalized:
        raise ValueError(f"Missing Ollama manifest digest header for {repository}:{tag}")
    return normalized


def _ollama_pull_stream(model: str, timeout: float = 600.0) -> None:
    url = local_ollama_url("/api/pull")
    data = json.dumps({"model": model, "stream": True}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _DIRECT_OPENER.open(request, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict) and item.get("error"):
                raise RuntimeError(f"Ollama pull failed: {item['error']}")


def _unload_resident_ollama_model(model: str) -> None:
    try:
        ps = _ollama_json_request("/api/ps", timeout=5.0)
        resident = ps.get("models") or []
        if any(isinstance(item, dict) and model in {item.get("name"), item.get("model")} for item in resident):
            _ollama_json_request("/api/generate", {"model": model, "keep_alive": 0}, timeout=10.0)
    except Exception as exc:
        print(f"[runtime_sync] Warning: failed to unload updated model {model}: {exc}", file=sys.stderr)


def sync_ollama_model(
    project_root: Path,
    *,
    pinned: bool = False,
    check_only: bool = False,
    force: bool = False,
    attempts: int = 3,
    upstream_digest_fetcher: Callable[[str, str], str] = fetch_ollama_upstream_digest,
) -> dict[str, Any]:
    local_policy = json.loads((project_root / "local_model.json").read_text(encoding="utf-8"))
    model = str(local_policy["model"]).strip()
    runtime_policy = json.loads(resolve_runtime_policy_path(project_root).read_text(encoding="utf-8"))
    ollama_policy = runtime_policy["ollama"]
    repository = str(ollama_policy["repository"]).strip()
    tag = str(ollama_policy["tag"]).strip()
    pinned_digest = _normalize_digest(ollama_policy["manifest_sha256"])

    try:
        local_digest = _ollama_local_digest(model)
    except Exception as exc:
        if check_only:
            return {"status": "daemon_unreachable", "model": model, "error": str(exc)}
        raise RuntimeError(
            f"Ollama daemon is not reachable at {local_ollama_url('')}: {exc}. "
            "Start the ollama service and re-run sync."
        ) from exc

    desired_digest = pinned_digest
    upstream_error: str | None = None
    if not pinned:
        try:
            desired_digest = upstream_digest_fetcher(repository, tag)
        except Exception as exc:
            upstream_error = str(exc)
            if local_digest and not force:
                print(
                    f"[runtime_sync] Warning: could not check upstream for {model} ({exc}); keeping local digest.",
                    file=sys.stderr,
                )
                return {"status": "offline_kept", "model": model, "digest": local_digest}

    if local_digest and not force and local_digest == desired_digest:
        return {"status": "up_to_date", "model": model, "digest": local_digest}

    if check_only:
        return {
            "status": "update_available" if local_digest else "missing",
            "model": model,
            "local_digest": local_digest or None,
            "target_digest": desired_digest,
            **({"upstream_error": upstream_error} if upstream_error else {}),
        }

    _run_with_retries(
        lambda: _ollama_pull_stream(model),
        attempts=attempts,
        label=f"Ollama pull {model}",
    )
    new_digest = _ollama_local_digest(model)
    if not new_digest:
        raise RuntimeError(f"Ollama pull completed but model {model} is still missing from /api/tags")
    normalized_desired = _normalize_digest(desired_digest) if desired_digest else ""
    if normalized_desired and _normalize_digest(new_digest) != normalized_desired:
        raise RuntimeError(
            f"Ollama digest mismatch for {model}: expected {desired_digest}, got {new_digest}"
        )
    if local_digest and new_digest != local_digest:
        _unload_resident_ollama_model(model)
    return {
        "status": "updated" if local_digest else "installed",
        "model": model,
        "digest": new_digest,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synchronize Local Optimize venv and models.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_cmd = subparsers.add_parser("sync", help="Provision venv and synchronize all models")
    sync_cmd.add_argument("--force-venv", action="store_true")
    sync_cmd.add_argument("--force-models", action="store_true")
    sync_cmd.add_argument("--pinned", action="store_true")
    sync_cmd.add_argument("--check-only", action="store_true")
    sync_cmd.add_argument("--allow-embedding-update", action="store_true")
    sync_cmd.add_argument("--skip-venv", action="store_true")
    sync_cmd.add_argument("--skip-models", action="store_true")
    sync_cmd.add_argument("--skip-ollama", action="store_true")

    models_cmd = subparsers.add_parser("models", help="Inspect or synchronize models")
    models_sub = models_cmd.add_subparsers(dest="models_action", required=True)

    status_cmd = models_sub.add_parser("status", help="Show local vs upstream/policy model state")
    status_cmd.add_argument("--pinned", action="store_true")
    status_cmd.add_argument("--skip-ollama", action="store_true")

    msync_cmd = models_sub.add_parser("sync", help="Download or update models")
    msync_cmd.add_argument("--pinned", action="store_true")
    msync_cmd.add_argument("--force", action="store_true")
    msync_cmd.add_argument("--allow-embedding-update", action="store_true")
    msync_cmd.add_argument("--skip-ollama", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    project_root = args.root.resolve()
    report: dict[str, Any] = {}

    if args.command == "sync":
        if not args.skip_venv:
            report["venv"] = sync_venv(
                project_root,
                force=args.force_venv,
                check_only=args.check_only,
            )
        if not args.skip_models:
            report["models"] = sync_huggingface_models(
                project_root,
                pinned=args.pinned,
                check_only=args.check_only,
                allow_embedding_update=args.allow_embedding_update,
                force=args.force_models,
            )
        if not args.skip_ollama:
            report["ollama"] = sync_ollama_model(
                project_root,
                pinned=args.pinned,
                check_only=args.check_only,
                force=args.force_models,
            )
    elif args.command == "models":
        check_only = args.models_action == "status"
        report["models"] = sync_huggingface_models(
            project_root,
            pinned=args.pinned,
            check_only=check_only,
            allow_embedding_update=getattr(args, "allow_embedding_update", False),
            force=getattr(args, "force", False),
        )
        if not args.skip_ollama:
            report["ollama"] = sync_ollama_model(
                project_root,
                pinned=args.pinned,
                check_only=check_only,
                force=getattr(args, "force", False),
            )

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
