import hashlib
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / "migrations" / "0009_graph_evidence_acl.sql"
MANIFEST_PATH = ROOT / "migrations" / "manifest.json"


def _migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def _v9_manifest_entry() -> dict:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return next(item for item in manifest["migrations"] if item["version"] == "v9")


@pytest.mark.parametrize("table,canonical_id", [
    ("entity_evidence", "entity_id"),
    ("relation_evidence", "relation_id"),
])
def test_evidence_tables_have_scoped_source_and_lifecycle_columns(table, canonical_id):
    sql = _migration_sql()
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS public\.{table} \((.*?)\n\);",
        sql,
        re.DOTALL,
    )
    assert match, f"missing {table} definition"
    definition = match.group(1)

    required_columns = {
        canonical_id,
        "project_id",
        "root_id",
        "document_id",
        "provenance_id",
        "chunk_id",
        "visibility",
        "agent_id",
        "session_id",
        "validation_status",
        "is_active",
        "source_ref",
        "metadata",
        "created_at",
        "updated_at",
    }
    for column in required_columns:
        assert re.search(rf"^    {column} ", definition, re.MULTILINE), column

    assert f"CONSTRAINT {table}_owner_check CHECK" in definition
    assert "visibility IN ('public', 'shared') AND agent_id IS NULL AND session_id IS NULL" in definition
    assert "agent_id IS NOT NULL AND btrim(agent_id) <> ''" in definition
    assert "session_id IS NOT NULL AND btrim(session_id) <> ''" in definition
    for source_column in ("document_id", "provenance_id", "chunk_id", "source_ref"):
        assert re.search(rf"^    {source_column} (bigint|text),$", definition, re.MULTILINE)


def test_source_foreign_keys_enforce_project_and_root_scope_without_mutating_canonical_graph():
    sql = _migration_sql()

    for table in ("entity_evidence", "relation_evidence"):
        assert f"CONSTRAINT {table}_document_scope_fk" in sql
        assert "REFERENCES public.kb_documents (id, project_id, root_id)" in sql
        assert f"CONSTRAINT {table}_chunk_scope_fk" in sql
        assert "REFERENCES public.project_knowledge (id, project_id, root_id)" in sql

    assert "ALTER TABLE public.kb_entities" not in sql
    assert "ALTER TABLE public.kb_relations" not in sql
    assert "DROP TABLE" not in sql
    assert "DELETE FROM" not in sql


def test_uniqueness_keeps_canonical_fact_reusable_across_evidence_scopes():
    sql = _migration_sql()

    for table, canonical_id in (
        ("entity_evidence", "entity_id"),
        ("relation_evidence", "relation_id"),
    ):
        match = re.search(
            rf"CREATE UNIQUE INDEX IF NOT EXISTS {table}_identity_unique\s+"
            rf"ON public\.{table} \((.*?)\n    \);",
            sql,
            re.DOTALL,
        )
        assert match, table
        columns = match.group(1)
        for scoped_identity in (
            canonical_id,
            "project_id",
            "root_id",
            "visibility",
            "COALESCE(agent_id, '')",
            "COALESCE(session_id, '')",
        ):
            assert scoped_identity in columns


def test_manifest_checksum_and_schema_fingerprint_cover_v9_contract():
    entry = _v9_manifest_entry()
    assert entry["filename"] == MIGRATION_PATH.name
    assert entry["required"] is True
    assert entry["transactional"] is True
    assert entry["checksum"] == hashlib.sha256(MIGRATION_PATH.read_bytes()).hexdigest()

    invariants = {
        (item["type"], item.get("table"), item["name"])
        for item in entry["invariants"]
    }
    for table, canonical_id in (
        ("entity_evidence", "entity_id"),
        ("relation_evidence", "relation_id"),
    ):
        assert ("table", None, table) in invariants
        for column in (
            canonical_id,
            "project_id",
            "root_id",
            "document_id",
            "provenance_id",
            "chunk_id",
            "visibility",
            "agent_id",
            "session_id",
            "validation_status",
            "is_active",
            "source_ref",
            "metadata",
            "created_at",
            "updated_at",
        ):
            assert ("column", table, column) in invariants
        for constraint in (
            f"{table}_owner_check",
            f"{table}_validation_status_check",
        ):
            assert ("constraint", table, constraint) in invariants
        for index in (
            f"{table}_identity_unique",
            f"{table}_scope_idx",
            f"{table}_source_idx",
        ):
            assert ("index", table, index) in invariants

    sql = _migration_sql()
    named_constraints = set(re.findall(r"CONSTRAINT ([a-z0-9_]+)", sql))
    named_indexes = set(re.findall(r"INDEX IF NOT EXISTS ([a-z0-9_]+)", sql))
    manifest_constraints = {
        item["name"] for item in entry["invariants"] if item["type"] == "constraint"
    }
    manifest_indexes = {
        item["name"] for item in entry["invariants"] if item["type"] == "index"
    }
    assert named_constraints <= manifest_constraints
    assert named_indexes <= manifest_indexes
