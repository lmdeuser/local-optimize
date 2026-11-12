import zipfile

import pytest

import build_review_archive as archive_builder

pytestmark = pytest.mark.unit


def test_canonical_archive_path_is_portable_and_next_to_the_project():
    assert archive_builder.OUTPUT_PATH == (
        archive_builder.PROJECT_ROOT.parent / "local_optimize_reviewed.zip"
    )


def test_archive_includes_public_documentation_and_required_entrypoints():
    included = {
        path.relative_to(archive_builder.PROJECT_ROOT).as_posix()
        for path in archive_builder.build_file_list()
    }

    assert {
        "README.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "docs/installation.md",
        "docs/examples/secrets.env.example",
        "lmstudio_rag_mcp.py",
        "manage_secrets.py",
        "index_project_codebase.py",
        "reembed_corpus.py",
        "ruff.toml",
    } <= included


def test_archive_includes_intel_runtime_dependency_file(tmp_path, monkeypatch):
    runtime = tmp_path / "deploy" / "intel-xpu-requirements.txt"
    runtime.parent.mkdir()
    runtime.write_text("torch==2.14.1+xpu\n")
    unrelated = runtime.parent / "private.env"
    unrelated.write_text("fixture\n")
    monkeypatch.setattr(archive_builder, "PROJECT_ROOT", tmp_path)
    assert archive_builder.build_file_list() == [runtime]


def test_archive_allowlist_keeps_harmless_fixtures_and_only_required_shell(
    tmp_path, monkeypatch
):
    root = tmp_path / "local_optimize"
    root.mkdir()
    (root / "migration_runner.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "postgres_backup.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / "apply_updates.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    fixture = tests / "test_context7_wrapper_fixture.py"
    fixture.write_text("CONTEXT7_API_KEY = 'harmless-fixture'\n", encoding="utf-8")
    investigation = root / "investigation"
    investigation.mkdir()
    (investigation / "notes.md").write_text("exclude", encoding="utf-8")
    monkeypatch.setattr(archive_builder, "PROJECT_ROOT", root)

    included = {
        path.relative_to(root).as_posix() for path in archive_builder.build_file_list()
    }

    assert "tests/test_context7_wrapper_fixture.py" in included
    assert "postgres_backup.sh" in included
    assert "apply_updates.sh" not in included
    assert "investigation/notes.md" not in included


def test_archive_is_verified_then_atomically_replaced(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "migration_runner.py").write_text("print('reviewed')\n", encoding="utf-8")
    output = tmp_path / "reviewed.zip"
    output.write_bytes(b"old archive")
    monkeypatch.setattr(archive_builder, "PROJECT_ROOT", root)

    result = archive_builder.build_archive(output)

    assert result == output
    with zipfile.ZipFile(output) as built:
        assert built.namelist() == ["migration_runner.py"]
        assert built.read("migration_runner.py") == b"print('reviewed')\n"
    assert not list(tmp_path.glob(".reviewed.zip.*.tmp"))


def test_archive_failure_preserves_previous_output(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "migration_runner.py").write_text("print('reviewed')\n", encoding="utf-8")
    output = tmp_path / "reviewed.zip"
    output.write_bytes(b"previous archive")
    monkeypatch.setattr(archive_builder, "PROJECT_ROOT", root)

    def fail_replace(_source, _target):
        raise OSError("fixture replace failure")

    monkeypatch.setattr(archive_builder.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        archive_builder.build_archive(output)

    assert output.read_bytes() == b"previous archive"
    assert not list(tmp_path.glob(".reviewed.zip.*.tmp"))


def test_archive_includes_agents_and_runtime_metadata_but_not_venv_or_models(
    tmp_path, monkeypatch
):
    root = tmp_path / "local_optimize"
    root.mkdir()
    (root / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    runtime = root / "runtime"
    runtime.mkdir()
    for name in ("manifest.json", "requirements.lock", "split-manifest.json"):
        (runtime / name).write_text("{}\n", encoding="utf-8")
    venv_python = runtime / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    models = root / "models"
    models.mkdir()
    (models / "weights.bin").write_bytes(b"weights")
    monkeypatch.setattr(archive_builder, "PROJECT_ROOT", root)

    included = {
        path.relative_to(root).as_posix() for path in archive_builder.build_file_list()
    }

    assert {
        "AGENTS.md",
        "runtime/manifest.json",
        "runtime/requirements.lock",
        "runtime/split-manifest.json",
    } <= included
    assert "runtime/venv/bin/python" not in included
    assert "models/weights.bin" not in included
