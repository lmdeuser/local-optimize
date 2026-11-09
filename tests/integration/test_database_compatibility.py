import pytest

psycopg2 = pytest.importorskip("psycopg2")

from config import settings
from migration_runner import verify_runtime_schema

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_configured_agent_database_has_latest_compatible_schema():
    try:
        connection = psycopg2.connect(**settings.get_db_params())
    except Exception as exc:
        pytest.skip(
            "Configured PostgreSQL agent database is unavailable "
            f"({exc.__class__.__name__})"
        )
    try:
        assert verify_runtime_schema(connection), (
            "aiagent.public is not at the required verified migration version"
        )
    finally:
        connection.rollback()
        connection.close()
