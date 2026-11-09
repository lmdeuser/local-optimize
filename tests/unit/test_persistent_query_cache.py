"""The shared cache tier must be fast when it works and invisible when it does not.

`PersistentQueryCache` sits on the retrieval hot path and talks to Postgres. Two
properties matter more than its hit rate:

* it fails open. A cache that is down has to look exactly like a cache that is
  cold, because the alternative is that a database hiccup turns into a failed
  search. Every method here is exercised against a connection factory that
  raises, and every one of them must return a miss rather than propagate.

* it does not vectorize a question it did not need to vectorize. The embedding
  arrives as a callable precisely so an exact hit skips it, and on the CPU
  fallback path that call is most of the latency the cache exists to remove.

The database is faked rather than mocked at the driver level: these tests are
about the class's own decisions - which statement it runs when, what it counts,
when it sweeps - not about SQL semantics, which the integration suite covers.
"""

import contextlib

import pytest

from query_cache import PersistentQueryCache


pytestmark = pytest.mark.unit


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._conn.statements.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._conn.rows.pop(0) if self._conn.rows else None

    @property
    def rowcount(self):
        return self._conn.rowcount


class FakeConnection:
    def __init__(self, rows=None, rowcount=0):
        self.rows = list(rows or [])
        self.rowcount = rowcount
        self.statements = []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


def factory_for(conn):
    @contextlib.contextmanager
    def factory():
        yield conn

    return factory


def failing_factory():
    @contextlib.contextmanager
    def factory():
        raise RuntimeError("connection pool is not available")
        yield  # pragma: no cover

    return factory


def make_cache(conn=None, **overrides):
    settings = {
        "ttl_seconds": 900,
        "max_rows": 100,
        "semantic_threshold": 0.98,
    }
    settings.update(overrides)
    return PersistentQueryCache(
        factory_for(conn) if conn is not None else failing_factory(), **settings
    )


def test_exact_hit_never_vectorizes_the_question():
    conn = FakeConnection(rows=[({"status": "success", "results": []},)])
    cache = make_cache(conn)
    calls = []

    response, provenance = cache.lookup(
        scope_hash="scope",
        query_hash="query",
        generation=3,
        embedding_provider=lambda: calls.append(1) or [0.1] * 384,
    )

    assert response == {"status": "success", "results": []}
    assert provenance == {"layer": "l2-exact"}
    assert calls == [], "an exact hit must not pay to embed the query"
    assert cache.metrics()["exact_hits"] == 1
    assert len(conn.statements) == 1


def test_semantic_hit_discloses_the_question_it_actually_answered():
    conn = FakeConnection(
        rows=[None, ({"status": "success"}, "как кешируются запросы", 0.9912)]
    )
    cache = make_cache(conn)

    response, provenance = cache.lookup(
        scope_hash="scope",
        query_hash="query",
        generation=1,
        embedding_provider=lambda: [0.1] * 384,
    )

    assert response == {"status": "success"}
    assert provenance["layer"] == "l2-semantic"
    assert provenance["matched_query"] == "как кешируются запросы"
    assert provenance["similarity"] == 0.9912
    assert cache.metrics()["semantic_hits"] == 1


def test_threshold_of_one_disables_the_near_miss_path_entirely():
    conn = FakeConnection(rows=[None])
    cache = make_cache(conn, semantic_threshold=1.0)

    response, provenance = cache.lookup(
        scope_hash="scope",
        query_hash="query",
        generation=1,
        embedding_provider=lambda: [0.1] * 384,
    )

    assert (response, provenance) == (None, {})
    assert len(conn.statements) == 1, "only the exact lookup should have run"
    assert cache.metrics()["misses"] == 1


def test_an_unembeddable_query_degrades_to_exact_only():
    """A broken encoder must cost the near-miss path, not the search."""
    conn = FakeConnection(rows=[None])
    cache = make_cache(conn)

    def exploding_provider():
        raise RuntimeError("model is not loaded")

    response, provenance = cache.lookup(
        scope_hash="scope",
        query_hash="query",
        generation=1,
        embedding_provider=exploding_provider,
    )

    assert (response, provenance) == (None, {})
    assert cache.metrics()["misses"] == 1


@pytest.mark.parametrize(
    "call",
    [
        lambda cache: cache.lookup(scope_hash="s", query_hash="q", generation=0),
        lambda cache: cache.store(
            project_id="p",
            scope_hash="s",
            query_hash="q",
            query_text="t",
            generation=0,
            response={"status": "success"},
        ),
        lambda cache: cache.sweep(),
        lambda cache: cache.invalidate_all(),
    ],
)
def test_every_operation_fails_open_when_the_database_is_gone(call):
    cache = make_cache()
    result = call(cache)
    assert result in (None, 0, False, (None, {}))
    assert cache.metrics()["errors"] >= 1


def test_a_swallowed_failure_still_says_why():
    """Failing open must not mean failing silently forever.

    The counter alone tells the next person that something broke and nothing
    about what, which is how a fail-open path stays broken: nobody can act on
    a number. One retained message is the difference between a mystery and a
    bug report.
    """
    cache = make_cache()
    assert cache.last_error is None

    cache.lookup(scope_hash="s", query_hash="q", generation=0)

    assert cache.metrics()["errors"] == 1
    assert cache.last_error is not None
    assert cache.last_error == "RuntimeError"
    assert "connection pool is not available" not in cache.last_error


def test_generation_distinguishes_unreadable_from_untouched():
    """None means "do not trust any cached row"; 0 is a real generation."""
    assert make_cache().generation("project") is None
    assert make_cache(FakeConnection(rows=[])).generation("project") == 0
    assert make_cache(FakeConnection(rows=[(7,)])).generation("project") == 7


def test_bump_generation_returns_the_new_value():
    conn = FakeConnection(rows=[(8,)])
    cache = make_cache(conn)
    assert cache.bump_generation("project") == 8
    assert cache.metrics()["generation_bumps"] == 1
    assert "rag_cache_generation" in conn.statements[0][0]


def test_an_unserializable_response_is_declined_rather_than_raised():
    conn = FakeConnection()
    cache = make_cache(conn)
    stored = cache.store(
        project_id="p",
        scope_hash="s",
        query_hash="q",
        query_text="t",
        generation=0,
        response={"results": object()},
    )
    assert stored is False
    assert conn.statements == []
    assert cache.metrics()["errors"] == 1


def test_housekeeping_is_amortized_over_writes_not_run_on_every_one():
    conn = FakeConnection()
    cache = make_cache(conn, sweep_interval=3)
    for _ in range(2):
        cache.store(
            project_id="p",
            scope_hash="s",
            query_hash="q",
            query_text="t",
            generation=0,
            response={"status": "success"},
        )
    assert cache.metrics()["sweeps"] == 0

    cache.store(
        project_id="p",
        scope_hash="s",
        query_hash="q",
        query_text="t",
        generation=0,
        response={"status": "success"},
    )
    assert cache.metrics()["sweeps"] == 1
    assert any("DELETE" in sql for sql, _ in conn.statements)


def test_disabled_cache_touches_nothing():
    conn = FakeConnection(rows=[({"status": "success"},)])
    cache = make_cache(conn, ttl_seconds=0)
    assert cache.enabled is False
    assert cache.lookup(scope_hash="s", query_hash="q", generation=0) == (None, {})
    assert cache.generation("p") is None
    assert conn.statements == []


def test_vector_literal_renders_what_pgvector_parses():
    assert PersistentQueryCache._vector_literal([0.5, -1.0]) == "[0.5,-1.0]"
    assert PersistentQueryCache._vector_literal(None) is None
    assert PersistentQueryCache._vector_literal([]) is None
    assert PersistentQueryCache._vector_literal(["not a number"]) is None
