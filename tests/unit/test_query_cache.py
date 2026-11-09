from concurrent.futures import ThreadPoolExecutor
import threading

import numpy as np
import pytest

import rag_engine
from project_context import (
    ProjectContext,
    ReadScope,
    ValidationStatus,
    WriteVisibility,
)


pytestmark = pytest.mark.unit


def make_context(tmp_path, **overrides):
    values = {
        "project_id": "project-a",
        "project_name": "Project A",
        "root_id": "root-a",
        "project_root": str(tmp_path),
        "allowed_roots": (str(tmp_path),),
        "write_visibility": WriteVisibility.SESSION_PRIVATE,
        "read_scope": ReadScope.SESSION,
        "agent_id": "agent-a",
        "session_id": "session-a",
        "validation_status": ValidationStatus.VALIDATED,
    }
    values.update(overrides)
    return ProjectContext(**values)


def make_engine(monkeypatch, *, query_entries=16, query_ttl=60, embedding_entries=16):
    monkeypatch.setattr(rag_engine, "QUERY_CACHE_MAX_ENTRIES", query_entries)
    monkeypatch.setattr(rag_engine, "QUERY_CACHE_TTL_SECONDS", query_ttl)
    monkeypatch.setattr(rag_engine, "EMBEDDING_CACHE_MAX_ENTRIES", embedding_entries)
    monkeypatch.setattr(rag_engine, "EMBEDDING_CACHE_TTL_SECONDS", 60)
    engine = object.__new__(rag_engine.RagEngine)
    engine._init_query_caches()
    engine._project_cache_generation = lambda _project: (0, True)
    return engine


def test_unknown_generation_does_not_serve_a_previous_l1_answer(monkeypatch, tmp_path):
    engine = make_engine(monkeypatch)
    engine._search_via_persistent_cache = lambda *a, **k: {"status": "success", "results": [{"content": "old"}]}
    ctx = make_context(tmp_path)
    assert engine.search(ctx, "query")["results"][0]["content"] == "old"
    engine._project_cache_generation = lambda _project: (0, False)
    engine._search_via_persistent_cache = lambda *a, **k: {"status": "success", "results": [{"content": "new"}]}
    assert engine.search(ctx, "query")["results"][0]["content"] == "new"


def test_bounded_cache_expires_and_evicts_lru_entries():
    now = [10.0]
    cache = rag_engine._BoundedTTLCache(2, 5, clock=lambda: now[0])

    cache.get_or_compute("first", lambda: {"value": 1})
    cache.get_or_compute("second", lambda: {"value": 2})
    cache.get_or_compute("first", lambda: pytest.fail("first should be cached"))
    cache.get_or_compute("third", lambda: {"value": 3})

    assert cache.metrics()["evictions"] == 1
    second, hit = cache.get_or_compute("second", lambda: {"value": 20})
    assert hit is False
    assert second == {"value": 20}

    now[0] += 6
    refreshed, hit = cache.get_or_compute("second", lambda: {"value": 10})
    assert hit is False
    assert refreshed == {"value": 10}
    assert cache.metrics()["expirations"] >= 1


def test_bounded_cache_coalesces_concurrent_misses():
    cache = rag_engine._BoundedTTLCache(4, 60)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def factory():
        calls.append("called")
        entered.set()
        assert release.wait(timeout=2)
        return {"value": 1}

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(cache.get_or_compute, "same", factory)
        assert entered.wait(timeout=2)
        second = executor.submit(cache.get_or_compute, "same", factory)
        release.set()
        assert first.result(timeout=2)[0] == {"value": 1}
        assert second.result(timeout=2)[0] == {"value": 1}

    assert calls == ["called"]
    assert cache.metrics()["misses"] == 1
    assert cache.metrics()["hits"] == 1


def test_search_cache_is_scope_safe_copy_isolated_and_invalidated(monkeypatch, tmp_path):
    engine = make_engine(monkeypatch)
    calls = []

    def search_uncached(context, query, limit):
        calls.append((context.session_id, query, limit))
        return {
            "status": "success",
            "results": [{"content": "cached", "metadata": {"scope": context.session_id}}],
        }

    engine._search_uncached = search_uncached
    first_context = make_context(tmp_path, session_id="one")
    second_context = make_context(tmp_path, session_id="two")

    first = engine.search(first_context, "same query", limit=5)
    first["results"][0]["metadata"].clear()
    repeated = engine.search(first_context, "same query", limit=5)
    isolated = engine.search(second_context, "same query", limit=5)

    assert repeated["results"][0]["metadata"] == {"scope": "one"}
    assert isolated["results"][0]["metadata"] == {"scope": "two"}
    assert calls == [("one", "same query", 5), ("two", "same query", 5)]

    engine._invalidate_search_cache(first_context)
    engine.search(first_context, "same query", limit=5)
    assert calls[-1] == ("one", "same query", 5)
    assert len(calls) == 3


def test_search_errors_are_not_cached(monkeypatch, tmp_path):
    engine = make_engine(monkeypatch)
    calls = []

    def search_uncached(_context, _query, _limit):
        calls.append(1)
        return {"status": "error", "message": "fixture"}

    engine._search_uncached = search_uncached
    context = make_context(tmp_path)

    assert engine.search(context, "query")["status"] == "error"
    assert engine.search(context, "query")["status"] == "error"
    assert len(calls) == 2


def test_embedding_cache_reuses_values_without_sharing_mutable_arrays(monkeypatch):
    engine = make_engine(monkeypatch)
    calls = []

    def encode_uncached(text):
        calls.append(text)
        return np.arange(rag_engine.EMBEDDING_DIM, dtype=np.float32)

    engine._encode_uncached = encode_uncached

    first = engine.encode("repeat", kind="query")
    first[0] = -100
    second = engine.encode("repeat", kind="query")
    passage = engine.encode("repeat", kind="passage")

    assert second[0] == 0
    assert passage[0] == 0
    assert len(calls) == 2
    assert engine.cache_metrics()["embedding"]["hits"] == 1


def test_inflight_wait_is_bounded_and_owner_recovers():
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from query_cache import BoundedTTLCache
    cache = BoundedTTLCache(4, 30, wait_timeout_seconds=0.03)
    started = threading.Event()
    release = threading.Event()
    def compute():
        started.set()
        release.wait(.5)
        return 7
    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = executor.submit(cache.get_or_compute, 'key', compute)
        assert started.wait(1)
        try:
            before = time.monotonic()
            with pytest.raises(TimeoutError):
                cache.get_or_compute('key', lambda: 99)
            assert time.monotonic() - before < .3
        finally:
            release.set()
        assert owner.result(timeout=1) == (7, False)
        assert cache.get_or_compute('key', lambda: 99) == (7, True)
