import logging
from pathlib import Path

import pytest

import config
from migrate_secrets import migrate_secrets

pytestmark = pytest.mark.unit


def test_migration_copies_only_allowlisted_database_settings(tmp_path, caplog):
    legacy = tmp_path / "legacy.env"
    foreign_value = "fixture-foreign-secret-value"
    legacy.write_text(
        "DB_HOST=db.internal\n"
        "DB_PASSWORD=fixture-db-password\n"
        f"ARBITRARY_VENDOR_TOKEN={foreign_value}\n"
        "CONTEXT7_API_KEY=fixture-do-not-copy\n",
        encoding="utf-8",
    )
    target = tmp_path / "config" / "secrets.env"
    caplog.set_level(logging.DEBUG)

    assert migrate_secrets(
        secrets_file=target,
        legacy_paths=[legacy],
        environ={},
    )

    parsed = config.load_secrets_file(target)
    assert parsed["DB_HOST"] == "db.internal"
    assert parsed["DB_PASSWORD"] == "fixture-db-password"
    assert "ARBITRARY_VENDOR_TOKEN" not in parsed
    assert "CONTEXT7_API_KEY" not in parsed
    assert foreign_value not in caplog.text
    assert "fixture-do-not-copy" not in caplog.text
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700


def test_existing_unknown_keys_are_removed_with_backup(tmp_path):
    target = tmp_path / "secrets.env"
    target.write_text("DB_USER=agent\nFOREIGN_SECRET=fixture\n", encoding="utf-8")

    assert migrate_secrets(secrets_file=target, legacy_paths=[], environ={})

    parsed = config.load_secrets_file(target)
    assert parsed["DB_USER"] == "agent"
    assert "FOREIGN_SECRET" not in parsed
    backups = list(tmp_path.glob("secrets.env.backup-*"))
    assert backups
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in backups)


def test_atomic_write_failure_preserves_previous_secrets(tmp_path, monkeypatch):
    target = tmp_path / "secrets.env"
    target.write_text("DB_USER=before\n", encoding="utf-8")
    before = target.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("fixture replace failure")

    monkeypatch.setattr(config.os, "replace", fail_replace)
    assert not config.save_secrets_file(
        {"DB_USER": "after"}, backup=True, file_path=target
    )
    assert target.read_bytes() == before


def test_empty_legacy_path_list_does_not_use_default_sources(tmp_path, monkeypatch):
    default_source = tmp_path / ".antigravity_rag.env"
    default_source.write_text("DB_HOST=should-not-be-read\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    target = tmp_path / "explicit" / "secrets.env"

    assert migrate_secrets(secrets_file=target, legacy_paths=[], environ={})

    assert config.load_secrets_file(target)["DB_HOST"] == "127.0.0.1"



@pytest.mark.parametrize(
    "value",
    [
        "plain",
        " leading and trailing ",
        'double"quote',
        "single'quote",
        "back\\slash",
        "dollar$sign",
        "back`tick",
        "hash#value",
        "unicode-пароль",
        "both'\"$",
    ],
)
def test_secret_values_round_trip_without_distortion(tmp_path, value):
    target = tmp_path / "secrets.env"

    assert config.save_secrets_file({"DB_PASSWORD": value}, file_path=target)

    assert config.load_secrets_file(target)["DB_PASSWORD"] == value


def test_unquoted_edge_quotes_are_not_stripped(tmp_path):
    target = tmp_path / "secrets.env"
    target.write_text('DB_PASSWORD=password"\n', encoding="utf-8")

    assert config.load_secrets_file(target)["DB_PASSWORD"] == 'password"'
