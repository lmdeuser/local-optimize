import copy
import json
import stat

import pytest

import mcp_config_generator as generator

pytestmark = pytest.mark.unit


def _opaque_config():
    return {
        "topLevel": {"keep": [1, {"nested": True}]},
        "mcpServers": {
            "third-party": {"command": "third-party", "args": ["serve"]},
            "remote-docs": {
                "type": "http",
                "url": "https://docs.invalid/mcp",
                "headers": {"X-Fictional-Token": "fixture-value"},
                "nested": {"keep": ["exact", 7]},
            },
        },
    }


def test_cursor_merge_changes_only_owned_entry(tmp_path):
    path = tmp_path / "mcp.json"
    original = _opaque_config()
    path.write_text(json.dumps(original), encoding="utf-8")

    result = generator._build(path, fixture_entry(), dry_run=False, backup=True)

    for key, value in original.items():
        if key != "mcpServers":
            assert result[key] == value
    for name, entry in original["mcpServers"].items():
        assert result["mcpServers"][name] == entry
    assert "local-rag" in result["mcpServers"]
    assert json.loads(path.read_text(encoding="utf-8")) == result
    backups = list(tmp_path.glob("mcp.json.backup-*"))
    assert len(backups) == 1
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_owned_legacy_entry_is_migrated(tmp_path):
    path = tmp_path / "mcp.json"
    data = _opaque_config()
    data["mcpServers"]["local_rag"] = {
        "command": generator.PYTHON_EXEC,
        "args": [generator.MCP_SERVER_SCRIPT],
    }
    path.write_text(json.dumps(data), encoding="utf-8")

    result = generator._build(path, fixture_entry(), dry_run=False, backup=False)

    assert "local_rag" not in result["mcpServers"]
    assert "local-rag" in result["mcpServers"]


def test_unowned_reserved_entry_fails_closed(tmp_path):
    path = tmp_path / "mcp.json"
    data = _opaque_config()
    data["mcpServers"]["local-rag"] = {"command": "unrelated", "args": []}
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(ValueError, match="not owned"):
        generator._build(path, fixture_entry(), dry_run=False, backup=True)

    assert path.read_bytes() == before


def test_atomic_failure_preserves_original_and_backup(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    original = _opaque_config()
    path.write_text(json.dumps(original), encoding="utf-8")
    before = path.read_bytes()

    def fail_dump(*_args, **_kwargs):
        raise OSError("fixture write failure")

    monkeypatch.setattr(generator.json, "dump", fail_dump)
    with pytest.raises(OSError, match="fixture write failure"):
        generator.atomic_write_json(path, copy.deepcopy(original), backup=True)

    assert path.read_bytes() == before
    backups = list(tmp_path.glob("mcp.json.backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before


def test_dry_run_does_not_create_target(tmp_path):
    path = tmp_path / "missing" / "mcp.json"
    result = generator._build(path, fixture_entry(), dry_run=True, backup=True)
    assert "local-rag" in result["mcpServers"]
    assert not path.exists()
    assert not path.parent.exists()


def test_second_config_failure_restores_first_config(tmp_path, monkeypatch):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(_opaque_config(), sort_keys=True), encoding="utf-8")
    second.write_text(json.dumps({"mcpServers": {}}, sort_keys=True), encoding="utf-8")
    first_before = first.read_bytes()
    second_before = second.read_bytes()
    real_atomic_write = generator.atomic_write_json

    def fail_second(path, data, backup=True):
        if Path(path) == second:
            raise OSError("fixture second-config failure")
        return real_atomic_write(path, data, backup=backup)

    from pathlib import Path

    monkeypatch.setattr(generator, "atomic_write_json", fail_second)
    with pytest.raises(OSError, match="second-config failure"):
        generator._build_many(
            [first, second], fixture_entry(), dry_run=False, backup=False
        )

    assert first.read_bytes() == first_before
    assert second.read_bytes() == second_before


def fixture_entry():
    return {"command": generator.PYTHON_EXEC, "args": [generator.MCP_SERVER_SCRIPT]}


def test_public_cloud_configuration_remains_blocked(tmp_path):
    path = tmp_path / "mcp.json"
    original = json.dumps(_opaque_config())
    path.write_text(original)
    with pytest.raises(RuntimeError, match="cloud-capable"):
        generator.build_cursor_mcp_config(target_path=path)
    assert path.read_text() == original


def test_lmstudio_config_writes_readonly_wrapper_and_preserves_other_servers(tmp_path):
    path = tmp_path / "lmstudio" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_opaque_config()))

    result = generator.build_lmstudio_mcp_config(target_path=path)

    saved = json.loads(path.read_text())
    assert result["mcpServers"][generator.CANONICAL_SERVER_NAME]["args"] == [
        generator.LMSTUDIO_MCP_SCRIPT
    ]
    assert saved["mcpServers"]["third-party"] == _opaque_config()["mcpServers"]["third-party"]
    assert saved["topLevel"] == _opaque_config()["topLevel"]


def test_prune_cloud_client_configs_removes_only_owned_entries(tmp_path):
    owned_path = tmp_path / "cursor" / "mcp.json"
    owned_path.parent.mkdir(parents=True)
    owned_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    generator.CANONICAL_SERVER_NAME: {
                        "command": generator.PYTHON_EXEC,
                        "args": [generator.MCP_SERVER_SCRIPT],
                    },
                    generator.LEGACY_SERVER_NAME: {
                        "command": generator.PYTHON_EXEC,
                        "args": [generator.LMSTUDIO_MCP_SCRIPT],
                    },
                    "third-party": {"command": "node", "args": ["other.js"]},
                }
            }
        )
    )
    unowned_path = tmp_path / "gemini" / "mcp.json"
    unowned_path.parent.mkdir(parents=True)
    unowned_raw = json.dumps(
        {
            "mcpServers": {
                generator.CANONICAL_SERVER_NAME: {
                    "command": "node",
                    "args": ["/opt/foreign/server.js"],
                }
            }
        }
    )
    unowned_path.write_text(unowned_raw)

    pruned = generator.prune_cloud_client_configs(paths=[owned_path, unowned_path])

    assert pruned == {
        str(owned_path): [generator.CANONICAL_SERVER_NAME, generator.LEGACY_SERVER_NAME]
    }
    owned_saved = json.loads(owned_path.read_text())
    assert set(owned_saved["mcpServers"]) == {"third-party"}
    assert unowned_path.read_text() == unowned_raw
