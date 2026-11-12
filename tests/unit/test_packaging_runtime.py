import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SPEC = importlib.util.spec_from_file_location(
    "stage_runtime", Path(__file__).parents[2] / "packaging" / "stage_runtime.py"
)
stage_runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage_runtime)


def test_relocate_venv_rewrites_shebang_and_removes_build_command(tmp_path):
    venv = tmp_path / "build" / "venv"
    script = venv / "bin" / "pip"
    script.parent.mkdir(parents=True)
    script.write_text(f"#!{venv}/bin/python\nprint('ok')\n")
    activate = venv / "bin" / "activate.csh"
    activate.write_text(f"setenv VIRTUAL_ENV '{venv}'\n")
    upstream = venv / "lib" / "module.py"
    upstream.parent.mkdir()
    upstream.write_text("#!/usr/bin/env/python3\n")
    cache = venv / "lib" / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-314.pyc").write_bytes(b"bytecode")
    (venv / ".gitignore").write_text("build-only\n")
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\n"
        f"command = /usr/bin/python3 -m venv {venv}\n"
        "version = 3.14.4\n"
    )

    stage_runtime.relocate_venv(venv, Path("/usr/lib/local-optimize/venv"))

    assert script.read_text().startswith("#!/usr/lib/local-optimize/venv/bin/python\n")
    assert activate.read_text() == "setenv VIRTUAL_ENV '/usr/lib/local-optimize/venv'\n"
    assert upstream.read_text() == "#!/usr/bin/env python3\n"
    assert not cache.exists()
    assert not (venv / ".gitignore").exists()
    config = (venv / "pyvenv.cfg").read_text()
    assert "command = " not in config
    assert str(venv) not in config


def test_ollama_staging_copies_only_manifest_dependencies(tmp_path):
    config = b'{"format":"test"}'
    layer = b"model weights"
    config_digest = hashlib.sha256(config).hexdigest()
    layer_digest = hashlib.sha256(layer).hexdigest()
    manifest = json.dumps({
        "config": {"digest": f"sha256:{config_digest}", "size": len(config)},
        "layers": [{"digest": f"sha256:{layer_digest}", "size": len(layer)}],
    }).encode()
    source = tmp_path / "source"
    relative_manifest = Path("manifests/registry.ollama.ai/library/test/1b")
    (source / relative_manifest).parent.mkdir(parents=True)
    (source / relative_manifest).write_bytes(manifest)
    for digest, payload in ((config_digest, config), (layer_digest, layer)):
        blob = source / "blobs" / f"sha256-{digest}"
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(payload)
    unrelated = source / "blobs" / f"sha256-{'0' * 64}"
    unrelated.write_bytes(b"unrelated")
    destination = tmp_path / "destination"
    policy = {"ollama": {
        "repository": "library/test",
        "tag": "1b",
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }}

    stage_runtime.stage_ollama_model(destination, policy, source)

    assert (destination / relative_manifest).read_bytes() == manifest
    assert (destination / "blobs" / f"sha256-{layer_digest}").read_bytes() == layer
    assert not (destination / unrelated.name).exists()


def test_huggingface_staging_excludes_caches_and_onnx(tmp_path):
    source_root = tmp_path / "source"
    model = source_root / "models" / "encoder"
    (model / "onnx").mkdir(parents=True)
    (model / ".cache").mkdir()
    (model / "config.json").write_text("{}")
    (model / "model.safetensors").write_bytes(b"weights")
    (model / "onnx" / "model.onnx").write_bytes(b"unused")
    (model / ".cache" / "metadata.json").write_text("{}")
    destination = tmp_path / "destination"
    policy = {"models": {"encoder": {
        "repository": "example/encoder",
        "revision": "pinned",
        "required_files": ["config.json", "model.safetensors"],
    }}}

    stage_runtime.stage_huggingface_models(source_root, destination, policy, tmp_path / "python")

    assert (destination / "encoder" / "model.safetensors").is_file()
    assert not (destination / "encoder" / "onnx").exists()
    assert not (destination / "encoder" / ".cache").exists()


def test_blob_path_rejects_non_sha256_digest():
    with pytest.raises(ValueError, match="Invalid Ollama blob digest"):
        stage_runtime.blob_path("md5:abcd")



def test_launcher_preserves_caller_working_directory(tmp_path):
    app_root = tmp_path / "app"
    python = app_root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        "#!/bin/sh\n"
        "printf '%s' \"{\\\"caller\\\":\\\"$LOCAL_OPTIMIZE_CALLER_DIR\\\",\\\"script\\\":\\\"$1\\\"}\" "
        "> \"$LOCAL_OPTIMIZE_ROOT/result.json\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    caller = tmp_path / "caller project"
    caller.mkdir()
    environment = dict(os.environ, LOCAL_OPTIMIZE_ROOT=str(app_root))
    launcher = Path(__file__).parents[2] / "packaging" / "local-optimize"

    finished = subprocess.run(
        [str(launcher), "index"],
        cwd=caller,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert finished.returncode == 0, finished.stderr
    result = json.loads((app_root / "result.json").read_text(encoding="utf-8"))
    assert result["caller"] == str(caller)
    assert result["script"].endswith("index_project_codebase.py")


def test_index_all_uses_launcher_caller_directory(tmp_path, monkeypatch):
    import index_project_codebase

    caller = tmp_path / "caller project"
    caller.mkdir()
    monkeypatch.setenv("LOCAL_OPTIMIZE_CALLER_DIR", str(caller))
    captured = {}

    def fake_context(_settings, **_kwargs):
        captured.update(_kwargs)
        return type("Context", (), {"project_root": caller})()

    class FakeEngine:
        def close(self):
            return None

    monkeypatch.setattr(index_project_codebase, "build_default_project_context", fake_context)
    monkeypatch.setattr(index_project_codebase, "RagEngine", FakeEngine)

    assert index_project_codebase.index_all() == 0
    assert captured["working_directory"] == str(caller)
