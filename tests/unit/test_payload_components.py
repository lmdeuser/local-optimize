import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit
PACKAGING = Path(__file__).parents[2] / "packaging"
sys.path.insert(0, str(PACKAGING))


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, PACKAGING / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_payload_components_round_trip_preserves_manifest(tmp_path):
    stage_runtime = load_module("stage_runtime")
    components = load_module("payload_components")
    source_root = tmp_path / "source"
    payload = source_root / "runtime"
    large = payload / "venv" / "lib" / "large.bin"
    large.parent.mkdir(parents=True)
    large.write_bytes(bytes(range(100)))
    (payload / "venv" / "bin").mkdir()
    (payload / "venv" / "bin" / "python").symlink_to("python3")
    (payload / "venv" / "empty").mkdir()
    (payload / "models" / "encoder").mkdir(parents=True)
    (payload / "models" / "encoder" / "config.json").write_text("{}")
    (payload / "ollama-models" / "blobs").mkdir(parents=True)
    (payload / "ollama-models" / "blobs" / "sha256-test").write_bytes(b"weights")
    (payload / "requirements.lock").write_text("fixture==1\n")
    policy = {"test": True}
    stage_runtime.write_manifest(payload, policy)
    main_root = tmp_path / "main" / "pkg-1.0.0"
    main_root.mkdir(parents=True)
    for name in ("manifest.json", "requirements.lock"):
        (main_root / "runtime").mkdir(exist_ok=True)
        (main_root / "runtime" / name).write_bytes((payload / name).read_bytes())
    output = tmp_path / "output"

    names = components.pack_payload(source_root, main_root, "pkg", "1.0.0", output, chunk_size=32)

    assert len(names) > 1
    assert (output / "pkg_1.0.0.orig.tar.gz").is_file()
    assemble_root = tmp_path / "assembled"
    from shutil import copytree
    copytree(main_root, assemble_root, symlinks=True)
    for name in names:
        destination = assemble_root / name
        destination.mkdir()
        subprocess.run([
            "tar", "-xzf", str(output / f"pkg_1.0.0.orig-{name}.tar.gz"),
            "-C", str(destination), "--strip-components=1",
        ], check=True)

    components.assemble_payload(assemble_root)

    assert (assemble_root / "runtime" / "venv" / "lib" / "large.bin").read_bytes() == bytes(range(100))
    assert (assemble_root / "runtime" / "venv" / "bin" / "python").is_symlink()
    assert (assemble_root / "runtime" / "venv" / "empty").is_dir()
    assert not (assemble_root / "runtime-parts").exists()
    expected = json.loads((assemble_root / "runtime" / "manifest.json").read_text())["payload_sha256"]
    for group in components.GROUPS:
        assert stage_runtime.tree_sha256(assemble_root / "runtime" / group) == expected[group]
