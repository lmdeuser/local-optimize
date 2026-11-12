import os
import secrets

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from config import settings
from migration_runner import MigrationError, MigrationRunner, load_manifest, verify_runtime_schema

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def _admin_connection():
    params = settings.get_db_params()
    params["dbname"] = os.getenv("TEST_POSTGRES_ADMIN_DB", "postgres")
    try:
        connection = psycopg2.connect(**params)
    except Exception as exc:
        pytest.skip(
            "Fresh database test requires an accessible administrative PostgreSQL database "
            f"({exc.__class__.__name__})"
        )
    connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user"
        )
        row = cursor.fetchone()
    if not row or not row[0]:
        connection.close()
        pytest.skip("Fresh database test requires PostgreSQL CREATEDB privilege")
    return connection


def _create_test_database(admin, database_name):
    assert database_name.startswith("local_optimize_test_")
    template = os.getenv("TEST_POSTGRES_TEMPLATE", "")
    if template and template != "local_optimize_test_template":
        raise ValueError("Only the dedicated empty vector test template is allowed")
    query = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
    if template:
        query += sql.SQL(" TEMPLATE {}").format(sql.Identifier(template))
    with admin.cursor() as cursor:
        cursor.execute(query)


def test_migrations_on_truly_fresh_database(tmp_path):
    admin = _admin_connection()
    database_name = f"local_optimize_test_{secrets.token_hex(8)}"
    database_connection = None
    created = False
    try:
        _create_test_database(admin, database_name)
        created = True

        params = settings.get_db_params()
        params["dbname"] = database_name

        def connect_test_database():
            return psycopg2.connect(**params)

        runner = MigrationRunner(
            connection_factory=connect_test_database,
            expected_database=database_name,
        )
        try:
            runner.apply_all()
        except MigrationError as exc:
            cause = exc.__cause__
            primary = getattr(getattr(cause, "diag", None), "message_primary", "")
            if isinstance(cause, psycopg2.errors.InsufficientPrivilege) and "extension" in primary:
                pytest.skip(
                    "Fresh database was created, but the PostgreSQL role cannot install "
                    "the required vector extension"
                )
            raise
        runner.apply_all()  # idempotence

        database_connection = connect_test_database()
        assert verify_runtime_schema(
            database_connection, expected_database=database_name
        )
        with database_connection.cursor() as cursor:
            cursor.execute("SET search_path TO public, pg_catalog")
            cursor.execute("SELECT current_database(), current_schema()")
            assert cursor.fetchone() == (database_name, "public")

            required_tables = {
                "schema_migrations",
                "project_knowledge",
                "kb_documents",
                "knowledge_provenance",
                "kb_entities",
                "kb_relations",
                "kb_feedback_logs",
            }
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
            assert required_tables <= {row[0] for row in cursor.fetchall()}

            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'project_knowledge'"
            )
            assert {
                "project_id",
                "project_name",
                "dedup_key",
                "visibility",
                "validation_status",
            } <= {row[0] for row in cursor.fetchall()}

            cursor.execute(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_schema = 'public' AND table_name = 'kb_relations'"
            )
            assert "kb_relations_from_project_fk" in {row[0] for row in cursor.fetchall()}

            cursor.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = 'project_knowledge'"
            )
            assert "project_knowledge_dedup_key_unique" in {row[0] for row in cursor.fetchall()}

            expected = {item.version: item.checksum for item in load_manifest()}
            cursor.execute(
                "SELECT version, checksum FROM public.schema_migrations "
                "WHERE execution_status = 'applied'"
            )
            assert dict(cursor.fetchall()) == expected

            import numpy as np
            from project_context import configured_context
            from rag_engine import RagEngine

            root_a = tmp_path / "root-a"
            root_b = tmp_path / "root-b"
            root_a.mkdir()
            root_b.mkdir()
            source_a = root_a / "same.md"
            source_b = root_b / "same.md"
            source_a.write_text("original source content for root isolation", encoding="utf-8")
            source_b.write_text("different source content for the second root", encoding="utf-8")
            context_a = configured_context(
                project_id="project-a", project_name="Project A", root_id="root-a",
                project_root=root_a, allowed_roots=[root_a], write_visibility="shared",
                read_scope="shared", agent_id="agent-a", session_id="session-a",
            )
            context_b = configured_context(
                project_id="project-a", project_name="Project A", root_id="root-b",
                project_root=root_b, allowed_roots=[root_b], write_visibility="shared",
                read_scope="shared", agent_id="agent-a", session_id="session-a",
            )
            engine = object.__new__(RagEngine)
            engine._get_conn = lambda: database_connection
            engine._put_conn = lambda *_args, **_kwargs: None
            engine.encode = lambda *_args, **_kwargs: np.zeros(384, dtype=np.float32)
            engine._invalidate_search_cache = lambda *_args, **_kwargs: None

            saved_a = engine.save(
                context_a, source_a.read_text(), source_uri=str(source_a),
                source_type="document", file_path="same.md",
            )
            saved_b = engine.save(
                context_b, source_b.read_text(), source_uri=str(source_b),
                source_type="document", file_path="same.md",
            )
            assert saved_a["chunk_id"] != saved_b["chunk_id"]
            assert engine._live_chunk_ids(context_a, "same.md", "document") == {saved_a["chunk_id"]}
            assert engine._live_chunk_ids(context_b, "same.md", "document") == {saved_b["chunk_id"]}

            assert engine.delete_document(context_a, str(source_a))["deleted_count"] == 1
            with database_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT validation_status FROM public.project_knowledge WHERE id = %s",
                    (saved_b["chunk_id"],),
                )
                assert cursor.fetchone()[0] == "unverified"

            engine.save(
                context_a, source_a.read_text(), source_uri=str(source_a),
                source_type="document", file_path="same.md",
            )
            with database_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT validation_status FROM public.project_knowledge WHERE id = %s",
                    (saved_a["chunk_id"],),
                )
                assert cursor.fetchone()[0] == "invalidated"

            engine.save(
                context_a, source_a.read_text(), source_uri=str(source_a),
                source_type="document", file_path="same.md", restore_source=True,
            )
            with database_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT validation_status FROM public.project_knowledge WHERE id = %s",
                    (saved_a["chunk_id"],),
                )
                assert cursor.fetchone()[0] == "unverified"
    finally:
        if database_connection is not None:
            database_connection.close()
        try:
            if created:
                with admin.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()",
                        (database_name,),
                    )
                    cursor.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name))
                    )
        finally:
            admin.close()
