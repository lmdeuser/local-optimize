import hashlib
import importlib.util
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_build_assets_rewrites_dsc_for_version_bump_and_excludes_audit_dirs(tmp_path, monkeypatch):
    script_path = Path(__file__).parents[2] / "packaging" / "build_source_assets.py"
    spec = importlib.util.spec_from_file_location("build_source_assets", script_path)
    build_source_assets = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(build_source_assets)

    work = tmp_path / "repo"
    (work / "debian").mkdir(parents=True)
    (work / "obs").mkdir(parents=True)
    (work / ".claude").mkdir(parents=True)
    (work / ".agents").mkdir(parents=True)
    (work / "artifacts").mkdir(parents=True)

    (work / "debian" / "changelog").write_text(
        "local-optimize (1.0.2-0ubuntu1) resolute; urgency=medium\n\n  * Bump.\n",
        encoding="utf-8",
    )
    (work / "debian" / "control").write_text("Source: local-optimize\n", encoding="utf-8")
    (work / "obs" / "base.dsc").write_text(
        "Format: 3.0 (quilt)\n"
        "Source: local-optimize\n"
        "Version: 1.0.1-0ubuntu8\n"
        "Checksums-Sha1:\n"
        " 0000000000000000000000000000000000000000 0 local-optimize_1.0.1.orig.tar.gz\n"
        " 0000000000000000000000000000000000000000 0 local-optimize_1.0.1-0ubuntu8.debian.tar.xz\n"
        "Checksums-Sha256:\n"
        " 0000000000000000000000000000000000000000000000000000000000000000 0 local-optimize_1.0.1.orig.tar.gz\n"
        " 0000000000000000000000000000000000000000000000000000000000000000 0 local-optimize_1.0.1-0ubuntu8.debian.tar.xz\n"
        "Files:\n"
        " 00000000000000000000000000000000 0 local-optimize_1.0.1.orig.tar.gz\n"
        " 00000000000000000000000000000000 0 local-optimize_1.0.1-0ubuntu8.debian.tar.xz\n",
        encoding="utf-8",
    )
    (work / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (work / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    (work / ".agents" / "note.md").write_text("# hidden\n", encoding="utf-8")
    (work / "artifacts" / "leak.txt").write_text("leak\n", encoding="utf-8")

    tracked = [
        b"app.py",
        b".claude/settings.json",
        b".agents/note.md",
        b"artifacts/leak.txt",
        b"debian/changelog",
    ]
    monkeypatch.chdir(work)
    monkeypatch.setattr(
        build_source_assets.subprocess,
        "check_output",
        lambda cmd, **_kw: b"\0".join(tracked) + b"\0",
    )

    out_dir = tmp_path / "out"
    build_source_assets.build_assets(out_dir)

    orig = out_dir / "local-optimize_1.0.2.orig.tar.gz"
    debian_tar = out_dir / "local-optimize_1.0.2-0ubuntu1.debian.tar.xz"
    dsc = out_dir / "local-optimize_1.0.2-0ubuntu1.dsc"
    assert orig.is_file()
    assert debian_tar.is_file()
    assert dsc.is_file()

    with tarfile.open(orig, "r:gz") as archive:
        members = set(archive.getnames())
    assert "local-optimize-1.0.2/app.py" in members
    assert not any(
        m.startswith(("local-optimize-1.0.2/.claude", "local-optimize-1.0.2/.agents", "local-optimize-1.0.2/artifacts", "local-optimize-1.0.2/debian"))
        for m in members
    )

    dsc_text = dsc.read_text(encoding="utf-8")
    assert "Version: 1.0.2-0ubuntu1\n" in dsc_text
    for asset in (orig, debian_tar):
        raw = asset.read_bytes()
        for algo in ("md5", "sha1", "sha256"):
            expected = f" {hashlib.new(algo, raw).hexdigest()} {len(raw)} {asset.name}"
            assert expected in dsc_text



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


def test_index_project_codebase_handles_extensions_symlinks_foreign_roots_and_exit_code(
    tmp_path, monkeypatch
):
    import sys

    import index_project_codebase
    from project_context import ProjectDefinition, ProjectRegistry

    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "notes.txt").write_text("plain text document\n", encoding="utf-8")
    (parent / "UPPER.TXT").write_text("upper text document\n", encoding="utf-8")
    (parent / "APP.PY").write_text("x = 1\n", encoding="utf-8")
    skills_dir = parent / "skills"
    skills_dir.mkdir()
    (skills_dir / "guide.MD").write_text("# skill\n", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    (parent / "link.txt").symlink_to(outside / "secret.txt")
    (parent / "linked_dir").symlink_to(outside)

    foreign = parent / "nested_foreign"
    foreign.mkdir()
    (foreign / "foreign.py").write_text("y = 2\n", encoding="utf-8")

    registry = ProjectRegistry([
        ProjectDefinition("parent", "Parent", {"primary": parent}),
        ProjectDefinition("foreign", "Foreign", {"primary": foreign}),
    ])
    context = registry.resolve(
        project_id="parent",
        root_id="primary",
        write_visibility="shared",
        read_scope="shared",
        agent_id="project-indexer",
        session_id="manual-index",
        registry_fingerprint_hint=registry.fingerprint,
    )

    calls = []

    class RecordingEngine:
        def index_code(self, _ctx, path):
            calls.append(("code", Path(path).name))
            return {"status": "success"}

        def index_skill(self, _ctx, path):
            calls.append(("skill", Path(path).name))
            return {"status": "success"}

        def index_document(self, _ctx, path):
            calls.append(("doc", Path(path).name))
            if Path(path).name == "UPPER.TXT":
                return {"status": "error"}
            return {"status": "success"}

        def close(self):
            return None

    monkeypatch.setattr(
        index_project_codebase, "build_default_project_context", lambda *_a, **_kw: context
    )
    monkeypatch.setattr(index_project_codebase, "get_project_registry", lambda _s: registry)
    monkeypatch.setattr(index_project_codebase, "RagEngine", RecordingEngine)
    monkeypatch.setattr(sys, "argv", ["index_project_codebase.py"])

    assert index_project_codebase.main() == 1
    assert ("doc", "notes.txt") in calls
    assert ("doc", "UPPER.TXT") in calls
    assert ("code", "APP.PY") in calls
    assert ("skill", "guide.MD") in calls
    assert all(name not in {"link.txt", "secret.txt", "foreign.py"} for _kind, name in calls)

    calls.clear()
    (parent / "UPPER.TXT").write_text("ok\n", encoding="utf-8")

    class AllOkEngine(RecordingEngine):
        def index_document(self, _ctx, path):
            calls.append(("doc", Path(path).name))
            return {"status": "success"}

    monkeypatch.setattr(index_project_codebase, "RagEngine", AllOkEngine)
    assert index_project_codebase.main() == 0
