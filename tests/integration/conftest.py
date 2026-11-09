"""Non-PostgreSQL integration tests must never reach a real database."""
import pytest


@pytest.fixture(autouse=True)
def isolate_component_tests(request, monkeypatch):
    if request.node.get_closest_marker("postgres"):
        return
    import psycopg2

    def forbidden(*args, **kwargs):
        raise AssertionError("Component test attempted a live PostgreSQL connection")

    monkeypatch.setattr(psycopg2, "connect", forbidden)
