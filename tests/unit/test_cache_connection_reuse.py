"""A writer must not compete with itself for the connection pool.

Every mutation ends by retiring the cached answers it invalidated. That call
used to reach into `ThreadedConnectionPool` for a second connection while the
write still held its first one. With `maxconn=16`, sixteen concurrent writers
each holding one and each asking for another exhaust the pool, `getconn()`
raises, and - because the cache path fails open by design - nothing says so. The
write commits. The shared generation counter never moves. Every other process
keeps serving the answers that write was supposed to retire, until their rows
expire fifteen minutes later.

That was not hypothetical: a running server carried the symptom (a frozen count
of swallowed errors) for hours before it was traced, and the mechanism was then
reproduced exactly - `PoolError: connection pool exhausted`, with
`generation_bumps` stuck at zero while `save()` returned success.

The fix is that writers lend the connection they already hold. The first test
here is the one that matters, because it does not test a writer - it tests every
writer there will ever be, including the ones not written yet.
"""

import ast
import inspect
import pathlib
import re

import pytest

import rag_engine
from project_context import ProjectContext, ReadScope, ValidationStatus, WriteVisibility


pytestmark = pytest.mark.unit


ENGINE_SOURCE = pathlib.Path(inspect.getfile(rag_engine)).read_text(encoding="utf-8")


def make_context(tmp_path, **overrides):
    values = {
        "project_id": "project-a",
        "project_name": "Project A",
        "root_id": "root-a",
        "project_root": str(tmp_path),
        "allowed_roots": (str(tmp_path),),
        "write_visibility": WriteVisibility.SHARED,
        "read_scope": ReadScope.SHARED,
        "agent_id": "agent-a",
        "session_id": "session-a",
        "validation_status": ValidationStatus.VALIDATED,
    }
    values.update(overrides)
    return ProjectContext(**values)


def invalidation_call_sites():
    """Every `_invalidate_search_cache(...)` in the engine, with its context.

    Yields (function name, line, holds_a_connection, passes_one). "Holds a
    connection" is decided by scanning the enclosing function up to the call: an
    acquisition more recent than any release means one is still in hand. The
    release usually sits in a `finally` further down the source, which is why the
    scan stops at the call rather than reading the whole body.
    """
    tree = ast.parse(ENGINE_SOURCE)
    lines = ENGINE_SOURCE.split("\n")
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "_invalidate_search_cache"
            ):
                continue
            preceding = "\n".join(lines[node.lineno - 1 : call.lineno - 1])
            holds = preceding.rfind("_get_conn()") > preceding.rfind("_put_conn(")
            passes = any(kw.arg == "connection" for kw in call.keywords)
            yield node.name, call.lineno, holds, passes


def test_no_writer_asks_the_pool_for_a_second_connection():
    offenders = [
        (name, line)
        for name, line, holds, passes in invalidation_call_sites()
        if holds and not passes
    ]
    assert not offenders, (
        "these invalidate the search cache while still holding a pooled "
        f"connection, without lending it: {offenders}. Under concurrent writers "
        "the pool runs out, the generation bump fails open, and the write "
        "commits with its invalidation never reaching another process. Pass "
        "connection=conn."
    )


def test_the_guard_above_can_actually_see_a_call_site():
    """A guard that finds nothing passes forever. Prove it is looking."""
    sites = list(invalidation_call_sites())
    assert len(sites) >= 7, f"only found {len(sites)} call sites; the walk is broken"
    assert any(holds for _, _, holds, _ in sites), "no site holds a connection?"
    assert any(passes for _, _, _, passes in sites), "no site lends one?"


def test_the_bump_uses_a_lent_connection_instead_of_the_pool(tmp_path, monkeypatch):
    """The whole point: given a connection, do not open another."""
    engine = object.__new__(rag_engine.RagEngine)
    engine._init_query_caches()

    def forbidden():
        raise AssertionError("the shared cache went to the pool despite being lent one")

    monkeypatch.setattr(engine._persistent_cache, "_connection_factory", forbidden)

    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            statements.append((sql, params))

        def fetchone(self):
            return (42,)

    class Connection:
        def __init__(self):
            self.commits = 0

        def cursor(self):
            return Cursor()

        def commit(self):
            self.commits += 1

    connection = Connection()
    engine._invalidate_search_cache(make_context(tmp_path), connection=connection)

    assert len(statements) == 1
    assert "rag_cache_generation" in statements[0][0]
    assert connection.commits == 1
    assert engine._persistent_cache.metrics()["generation_bumps"] == 1
    assert engine._persistent_cache.metrics()["errors"] == 0


def test_a_lent_connection_is_handed_back_clean_after_a_failure():
    """It returns to the pool; it must not carry a failed transaction with it."""
    from query_cache import PersistentQueryCache

    rolled_back = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            raise RuntimeError("relation rag_cache_generation does not exist")

        def fetchone(self):  # pragma: no cover
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):  # pragma: no cover
            raise AssertionError("must not commit a failed statement")

        def rollback(self):
            rolled_back.append(True)

    def unusable_factory():  # pragma: no cover
        raise AssertionError("must not fall back to the pool")

    cache = PersistentQueryCache(
        unusable_factory, ttl_seconds=900, max_rows=100, semantic_threshold=0.98
    )
    assert cache.bump_generation("project", connection=Connection()) is None
    assert rolled_back == [True]
    assert cache.metrics()["errors"] == 1
    assert cache.last_error == "RuntimeError"


class TestUnknownGeneration:
    """An unreadable counter must not be mistaken for an unchanged one.

    `generation()` returning None means this worker cannot tell whether a peer
    has invalidated anything. Its own remembered number still matches rows it
    wrote earlier, so keying a lookup on it would return answers a peer's write
    has already superseded. The shared tier has to be skipped, not guessed at.
    """

    def engine(self, monkeypatch, *, shared):
        engine = object.__new__(rag_engine.RagEngine)
        engine._init_query_caches()
        monkeypatch.setattr(
            engine._persistent_cache, "generation", lambda _project: shared
        )
        return engine

    def test_unreadable_reports_itself_as_unknown(self, monkeypatch):
        engine = self.engine(monkeypatch, shared=None)
        generation, known = engine._project_cache_generation("project-a")
        assert (generation, known) == (0, False)

    def test_readable_is_trusted_and_adopted(self, monkeypatch):
        engine = self.engine(monkeypatch, shared=9)
        assert engine._project_cache_generation("project-a") == (9, True)

    def test_a_worker_never_moves_backwards(self, monkeypatch, tmp_path):
        """A peer reporting an older number must not resurrect what we retired."""
        engine = self.engine(monkeypatch, shared=0)
        engine._project_cache_generations["project-a"] = 5
        generation, known = engine._project_cache_generation("project-a")
        assert (generation, known) == (5, True)

    def test_an_unknown_generation_skips_the_shared_tier_entirely(
        self, monkeypatch, tmp_path
    ):
        engine = self.engine(monkeypatch, shared=None)
        monkeypatch.setattr(
            engine._persistent_cache,
            "lookup",
            lambda **_kw: pytest.fail("looked up a tier whose generation is unknown"),
        )
        monkeypatch.setattr(
            engine._persistent_cache,
            "store",
            lambda **_kw: pytest.fail("stored into a tier whose generation is unknown"),
        )
        engine._search_uncached = lambda *_a, **_kw: {"status": "success", "results": []}

        result = engine.search(make_context(tmp_path), "вопрос", limit=3)

        assert result["status"] == "success"
        assert result["cache"] == {"layer": "unshared"}, (
            "the response must say the tier was skipped, not report a miss - "
            "a miss claims the cache was consulted and had nothing"
        )

    def test_a_known_generation_does_use_the_shared_tier(self, monkeypatch, tmp_path):
        engine = self.engine(monkeypatch, shared=3)
        seen = {}
        monkeypatch.setattr(
            engine._persistent_cache,
            "lookup",
            lambda **kw: (seen.update(kw) or (None, {})),
        )
        monkeypatch.setattr(engine._persistent_cache, "store", lambda **_kw: True)
        engine._search_uncached = lambda *_a, **_kw: {"status": "success", "results": []}

        result = engine.search(make_context(tmp_path), "вопрос", limit=3)

        assert seen["generation"] == 3
        assert result["cache"] == {"layer": "miss"}


def test_docstring_and_code_agree_on_what_none_means():
    """The class documents None as "do not trust any cached row". Hold it to that."""
    doc = inspect.getdoc(rag_engine.RagEngine._project_cache_generation) or ""
    assert "untrusted" in doc or "does not know" in doc
    source = inspect.getsource(rag_engine.RagEngine._project_cache_generation)
    assert re.search(r"if shared is None:\s*\n\s*return local, False", source), (
        "the unreadable branch must return an explicit 'unknown', not fall "
        "through to the trusted path"
    )
