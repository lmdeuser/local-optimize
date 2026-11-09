import contextlib
from unittest.mock import patch

import pytest

import fast_router
from fast_router import AdaptiveModelRouter
from query_cache import PersistentAnswerCache


pytestmark = pytest.mark.unit


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.conn.rows.pop(0) if self.conn.rows else None

    @property
    def rowcount(self):
        return self.conn.rowcount


class FakeConnection:
    def __init__(self, rows=None, rowcount=1):
        self.rows = list(rows or [])
        self.rowcount = rowcount
        self.statements = []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


def make_cache(conn=None, **overrides):
    @contextlib.contextmanager
    def factory():
        if conn is None:
            raise RuntimeError("database unavailable")
        yield conn

    settings = {"ttl_seconds": 900, "max_rows": 100}
    settings.update(overrides)
    return PersistentAnswerCache(factory, **settings)


EVIDENCE = (
    "[Evidence 1 | source_uri=fixture://document | source_type=document "
    "| source_is_primary=true | validation_status=validated]\nA verified local fact."
)
VALID_ANSWER = "Verified answer [Evidence 1]."


def test_answer_cache_exact_hit_and_fail_open():
    conn = FakeConnection(rows=[(VALID_ANSWER,)])
    cache = make_cache(conn)

    answer, state = cache.lookup(
        project_id="project-a",
        scope_hash="scope",
        prompt_hash="a" * 64,
        generation=7,
    )

    assert (answer, state) == (VALID_ANSWER, "hit")
    assert "prompt_hash = %s" in conn.statements[0][0]
    assert conn.statements[0][1] == ("project-a", "scope", "a" * 64, 7)

    answer, state = make_cache().lookup(
        project_id="project-a",
        scope_hash="scope",
        prompt_hash="a" * 64,
        generation=7,
    )
    assert answer is None
    assert state == "unavailable"


def test_answer_cache_store_uses_generation_and_bounds_rows():
    conn = FakeConnection()
    cache = make_cache(conn)

    assert cache.store(
        project_id="project-a",
        scope_hash="scope",
        prompt_hash="b" * 64,
        generation=4,
        answer=VALID_ANSWER,
    )
    sql, params = conn.statements[0]
    assert "INSERT INTO public.rag_local_answer_cache" in sql
    assert params[:5] == ("project-a", "scope", "b" * 64, 4, VALID_ANSWER)
    assert cache.metrics()["stores"] == 1


def test_local_answer_cache_key_covers_model_input_and_is_repeatable(monkeypatch):
    key = fast_router.local_answer_cache_key("prompt", "system", False)
    assert key == fast_router.local_answer_cache_key("prompt", "system", False)
    assert len(key) == 64
    assert key != fast_router.local_answer_cache_key("changed prompt", "system", False)
    assert key != fast_router.local_answer_cache_key("prompt", "system", True)

    monkeypatch.setattr(fast_router, "LOCAL_ANSWER_CACHE_VERSION", "next")
    assert key != fast_router.local_answer_cache_key("prompt", "system", False)


def test_router_uses_cached_answer_without_calling_ollama():
    lookup = lambda _key: {
        "status": "success",
        "hit": True,
        "answer": VALID_ANSWER,
        "generation": 3,
        "cache_layer": "postgres",
    }
    router = AdaptiveModelRouter(confidence_threshold=0.5)

    with patch.object(fast_router, "query_local_ollama", side_effect=AssertionError("must hit cache")):
        result = router.evaluate(
            query="What is the verified fact?",
            context=EVIDENCE,
            rag_score=0.9,
            answer_cache_lookup=lookup,
        )

    assert result["decision"] == "respond"
    assert result["response"] == VALID_ANSWER
    assert result["answer_cache"] == {"layer": "postgres", "hit": True, "stored": False}


def test_router_stores_only_citation_validated_local_answer():
    lookup = lambda _key: {
        "status": "success",
        "hit": False,
        "generation": 9,
        "cache_layer": "miss",
    }
    stored = []

    def store(key, answer, generation):
        stored.append((key, answer, generation))
        return {"status": "success", "stored": True}

    router = AdaptiveModelRouter(confidence_threshold=0.5)
    with patch.object(fast_router, "query_local_ollama", return_value=VALID_ANSWER):
        result = router.evaluate(
            query="What is the verified fact?",
            context=EVIDENCE,
            rag_score=0.9,
            answer_cache_lookup=lookup,
            answer_cache_store=store,
        )

    assert result["decision"] == "respond"
    assert len(stored) == 1
    assert stored[0][1:] == (VALID_ANSWER, 9)
    assert result["answer_cache"]["stored"] is True


@pytest.mark.parametrize(
    "answer",
    ["НЕДОСТАТОЧНО ДАННЫХ", "This is an answer [Evidence 99]."],
)
def test_router_does_not_store_fallback_or_invalid_citations(answer):
    lookup = lambda _key: {
        "status": "success",
        "hit": False,
        "generation": 9,
        "cache_layer": "miss",
    }
    store = patch("fast_router.query_local_ollama", return_value=answer)
    stored = []
    router = AdaptiveModelRouter(confidence_threshold=0.5)
    with store:
        result = router.evaluate(
            query="What is the verified fact?",
            context=EVIDENCE,
            rag_score=0.9,
            answer_cache_lookup=lookup,
            answer_cache_store=lambda *args: stored.append(args),
        )

    assert result["decision"] in {"respond", "escalate"}
    assert stored == []
