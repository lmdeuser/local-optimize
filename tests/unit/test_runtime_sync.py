import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import runtime_sync

pytestmark = pytest.mark.unit


def _write_policy(root: Path) -> None:
    (root / "packaging").mkdir(parents=True, exist_ok=True)
    (root / "packaging" / "runtime-policy.json").write_text(
        json.dumps(
            {
                "python": "3.14",
                "install_root": "/usr/lib/local-optimize",
                "system_runtime_root": "/var/lib/local-optimize",
                "models": {
                    "multilingual-e5-small": {
                        "role": "embedding",
                        "repository": "intfloat/multilingual-e5-small",
                        "revision": "rev-e5-1",
                        "required_files": ["config.json", "model.safetensors"],
                    },
                    "Qwen3-Reranker-0.6B": {
                        "role": "reranker",
                        "repository": "Qwen/Qwen3-Reranker-0.6B",
                        "revision": "rev-qwen-1",
                        "required_files": ["config.json", "model.safetensors"],
                    },
                },
                "ollama": {
                    "model": "qwen2.5-coder:7b",
                    "repository": "library/qwen2.5-coder",
                    "tag": "7b",
                    "manifest_sha256": "a" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "local_model.json").write_text(
        json.dumps({"model": "qwen2.5-coder:7b", "num_ctx": 8192}),
        encoding="utf-8",
    )


def _populate_model(model_dir: Path, revision: str, weights: bytes = b"weights-v1") -> str:
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.safetensors").write_bytes(weights)
    weights_sha = hashlib.sha256(weights).hexdigest()
    (model_dir / ".model-state.json").write_text(
        json.dumps({"revision": revision, "weights_sha256": weights_sha}),
        encoding="utf-8",
    )
    return weights_sha


def test_resolve_model_dir_and_traversal_protection(tmp_path, monkeypatch):
    system_root = tmp_path / "system"
    monkeypatch.setenv("LOCAL_OPTIMIZE_SYSTEM_DIR", str(system_root))
    monkeypatch.delenv("LOCAL_OPTIMIZE_MODELS_DIR", raising=False)

    checkout = tmp_path / "checkout"
    (checkout / "models" / "Qwen3-Reranker-0.6B").mkdir(parents=True)
    assert runtime_sync.resolve_model_dir("Qwen3-Reranker-0.6B", checkout) == (
        checkout / "models" / "Qwen3-Reranker-0.6B"
    )

    (system_root / "models" / "Qwen3-Reranker-0.6B").mkdir(parents=True)
    empty_checkout = tmp_path / "empty_checkout"
    empty_checkout.mkdir()
    assert runtime_sync.resolve_model_dir("Qwen3-Reranker-0.6B", empty_checkout) == (
        system_root / "models" / "Qwen3-Reranker-0.6B"
    )

    resolved = runtime_sync.resolve_evidence_model_path(empty_checkout, "models/Qwen3-Reranker-0.6B")
    assert resolved == (system_root / "models" / "Qwen3-Reranker-0.6B").resolve()

    with pytest.raises(ValueError, match="traversal"):
        runtime_sync.resolve_evidence_model_path(checkout, "models/../outside")

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="allowed local models directory"):
        runtime_sync.resolve_evidence_model_path(checkout, str(outside))


def test_evidence_ranker_policy_hash_reflects_active_model_revision(tmp_path, monkeypatch):
    import evidence_ranker

    models_dir = tmp_path / "models"
    qwen_dir = models_dir / "Qwen3-Reranker-0.6B"
    _populate_model(qwen_dir, "rev-initial")
    (tmp_path / "retrieval_policy.json").write_text(
        json.dumps({"version": "v1", "model_path": "models/Qwen3-Reranker-0.6B"}),
        encoding="utf-8",
    )

    monkeypatch.setenv("LOCAL_OPTIMIZE_MODELS_DIR", str(models_dir))
    monkeypatch.setattr(evidence_ranker, "ROOT", tmp_path)
    monkeypatch.setattr(evidence_ranker, "POLICY", {"version": "v1", "model_path": "models/Qwen3-Reranker-0.6B"})
    first_hash = hashlib.sha256(
        json.dumps(evidence_ranker._active_policy_payload(), sort_keys=True).encode()
    ).hexdigest()

    _populate_model(qwen_dir, "rev-updated")
    second_hash = hashlib.sha256(
        json.dumps(evidence_ranker._active_policy_payload(), sort_keys=True).encode()
    ).hexdigest()
    assert first_hash != second_hash

    importlib.reload(evidence_ranker)


def test_sync_venv_skips_when_lock_matches_and_installs_on_change(tmp_path, monkeypatch):
    root = tmp_path / "app"
    (root / "runtime").mkdir(parents=True)
    lock_file = root / "runtime" / "requirements.lock"
    lock_file.write_text("pip==26.3\ntorch==2.14.1+xpu\n", encoding="utf-8")
    venv_dir = tmp_path / "venv"
    pip_cache = tmp_path / "pip-cache"
    commands = []

    def fake_run(cmd, check=False, **_kwargs):
        commands.append(cmd)
        if "-m" in cmd and "venv" in cmd:
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(runtime_sync.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_sync.sys, "version_info", (3, 14, 4, "final", 0))

    first = runtime_sync.sync_venv(root, venv_dir=venv_dir, pip_cache_dir=pip_cache)
    assert first["status"] == "updated"
    assert (venv_dir / ".lock-sha256").read_text(encoding="utf-8").strip() == runtime_sync.sha256_file(lock_file)
    assert any("--no-deps" in cmd and runtime_sync.PYTORCH_XPU_INDEX_URL in cmd for cmd in commands)
    assert any(cmd[-2:] == ["pip", "check"] for cmd in commands)

    commands.clear()
    second = runtime_sync.sync_venv(root, venv_dir=venv_dir, pip_cache_dir=pip_cache)
    assert second["status"] == "up_to_date"
    assert len(commands) == 1  # Only the python version probe ran; pip did not run.

    lock_file.write_text("pip==26.3\ntorch==2.14.2+xpu\n", encoding="utf-8")
    commands.clear()
    third = runtime_sync.sync_venv(root, venv_dir=venv_dir, pip_cache_dir=pip_cache)
    assert third["status"] == "updated"
    assert any("--no-deps" in cmd for cmd in commands)


def test_sync_hf_models_checks_upstream_protects_e5_weights_and_handles_offline(tmp_path, monkeypatch):
    root = tmp_path / "app"
    _write_policy(root)
    models_dir = tmp_path / "var_models"
    e5_sha = _populate_model(models_dir / "multilingual-e5-small", "rev-e5-1", b"e5-v1")
    qwen_sha = _populate_model(models_dir / "Qwen3-Reranker-0.6B", "rev-qwen-1", b"qwen-v1")

    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")

    downloads = []

    def fake_download(python_bin, repo_id, revision, target_dir):
        assert python_bin == venv_python
        downloads.append((repo_id, revision))
        (target_dir / "config.json").write_text("{}", encoding="utf-8")
        (target_dir / "model.safetensors").write_bytes(b"new-weights-" + revision.encode())

    monkeypatch.setattr(runtime_sync, "_download_hf_snapshot", fake_download)

    # 1. Upstream unchanged -> no downloads.
    unchanged = runtime_sync.sync_huggingface_models(
        root,
        models_dir=models_dir,
        venv_python=venv_python,
        upstream_fetcher=lambda repo: (
            {"sha": "rev-e5-1", "weights_sha256": e5_sha}
            if "e5" in repo
            else {"sha": "rev-qwen-1", "weights_sha256": qwen_sha}
        ),
    )
    assert unchanged["multilingual-e5-small"]["status"] == "up_to_date"
    assert unchanged["Qwen3-Reranker-0.6B"]["status"] == "up_to_date"
    assert downloads == []

    # 2. Upstream updated: Qwen updates automatically; E5 weights change is held safely.
    new_weights_sha = hashlib.sha256(b"e5-v2").hexdigest()
    updated = runtime_sync.sync_huggingface_models(
        root,
        models_dir=models_dir,
        venv_python=venv_python,
        upstream_fetcher=lambda repo: (
            {"sha": "rev-e5-2", "weights_sha256": new_weights_sha}
            if "e5" in repo
            else {"sha": "rev-qwen-2", "weights_sha256": hashlib.sha256(b"qwen-v2").hexdigest()}
        ),
    )
    assert updated["multilingual-e5-small"]["status"] == "held_embedding_weights"
    assert updated["Qwen3-Reranker-0.6B"]["status"] == "updated"
    assert downloads == [("Qwen/Qwen3-Reranker-0.6B", "rev-qwen-2")]
    assert json.loads((models_dir / "Qwen3-Reranker-0.6B" / ".model-state.json").read_text())["revision"] == "rev-qwen-2"

    # 3. E5 metadata-only commit (same model.safetensors SHA) updates safely without flag.
    downloads.clear()
    meta_only = runtime_sync.sync_huggingface_models(
        root,
        models_dir=models_dir,
        venv_python=venv_python,
        upstream_fetcher=lambda repo: (
            {"sha": "rev-e5-meta", "weights_sha256": e5_sha}
            if "e5" in repo
            else {"sha": "rev-qwen-2", "weights_sha256": hashlib.sha256(b"new-weights-rev-qwen-2").hexdigest()}
        ),
    )
    assert meta_only["multilingual-e5-small"]["status"] == "updated"
    assert downloads == [("intfloat/multilingual-e5-small", "rev-e5-meta")]

    # 4. Offline fallback keeps existing valid models; fails if model is missing locally.
    def fail_upstream(_repo):
        raise OSError("network unreachable")

    offline = runtime_sync.sync_huggingface_models(
        root,
        models_dir=models_dir,
        venv_python=venv_python,
        upstream_fetcher=fail_upstream,
    )
    assert offline["multilingual-e5-small"]["status"] == "offline_kept"
    assert offline["Qwen3-Reranker-0.6B"]["status"] == "offline_kept"


def test_sync_ollama_model_checks_digest_pulls_on_change_and_enforces_loopback(tmp_path, monkeypatch):
    root = tmp_path / "app"
    _write_policy(root)

    with pytest.raises(RuntimeError, match="loopback"):
        runtime_sync.local_ollama_url("/api/tags", host="http://example.com:11434")

    old_digest = "a" * 64
    new_digest = "b" * 64
    state = {"digest": old_digest, "resident": True, "pulls": 0, "unloads": 0}

    def fake_json(path, payload=None, timeout=10.0):
        if path == "/api/tags":
            return {"models": [{"name": "qwen2.5-coder:7b", "digest": state["digest"]}]} if state["digest"] else {"models": []}
        if path == "/api/ps":
            return {"models": [{"name": "qwen2.5-coder:7b"}]} if state["resident"] else {"models": []}
        if path == "/api/generate" and payload and payload.get("keep_alive") == 0:
            state["unloads"] += 1
            state["resident"] = False
            return {"done": True}
        raise AssertionError(f"Unexpected Ollama call: {path} {payload}")

    def fake_pull(model, timeout=600.0):
        assert model == "qwen2.5-coder:7b"
        state["pulls"] += 1
        state["digest"] = new_digest

    monkeypatch.setattr(runtime_sync, "_ollama_json_request", fake_json)
    monkeypatch.setattr(runtime_sync, "_ollama_pull_stream", fake_pull)

    up_to_date = runtime_sync.sync_ollama_model(
        root,
        upstream_digest_fetcher=lambda _repo, _tag: old_digest,
    )
    assert up_to_date["status"] == "up_to_date"
    assert state["pulls"] == 0

    updated = runtime_sync.sync_ollama_model(
        root,
        upstream_digest_fetcher=lambda _repo, _tag: new_digest,
    )
    assert updated == {"status": "updated", "model": "qwen2.5-coder:7b", "digest": new_digest}
    assert state["pulls"] == 1
    assert state["unloads"] == 1

    offline = runtime_sync.sync_ollama_model(
        root,
        upstream_digest_fetcher=Mock(side_effect=OSError("offline")),
    )
    assert offline == {"status": "offline_kept", "model": "qwen2.5-coder:7b", "digest": new_digest}


def test_launcher_dispatches_sync_without_venv_and_uses_system_venv_fallback(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "runtime_sync.py").write_text(
        "import json, sys\n"
        f"open({str(app_root / 'sync_call.json')!r}, 'w').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    system_dir = tmp_path / "system"
    system_python = system_dir / "venv" / "bin" / "python"
    system_python.parent.mkdir(parents=True)
    system_python.write_text(
        "#!/bin/sh\n"
        f"printf '%s' \"$1\" > {str(app_root / 'server_script.txt')!r}\n",
        encoding="utf-8",
    )
    system_python.chmod(0o755)

    launcher = Path(__file__).parents[2] / "packaging" / "local-optimize"
    env = dict(
        os.environ,
        LOCAL_OPTIMIZE_ROOT=str(app_root),
        LOCAL_OPTIMIZE_SYSTEM_DIR=str(system_dir),
        LOCAL_OPTIMIZE_SYSTEM_PYTHON=sys.executable,
    )
    env.pop("LOCAL_OPTIMIZE_VENV", None)

    sync_res = subprocess.run([str(launcher), "models", "status"], env=env, capture_output=True, text=True, check=False)
    assert sync_res.returncode == 0, sync_res.stderr
    assert json.loads((app_root / "sync_call.json").read_text(encoding="utf-8")) == [
        "--root",
        str(app_root),
        "models",
        "status",
    ]

    srv_res = subprocess.run([str(launcher), "server"], env=env, capture_output=True, text=True, check=False)
    assert srv_res.returncode == 0, srv_res.stderr
    assert (app_root / "server_script.txt").read_text(encoding="utf-8").endswith("gpu_rag_server.py")
