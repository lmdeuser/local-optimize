"""Small process-local caches used by the RAG engine."""

from __future__ import annotations

import collections
import copy
import json
import threading
import time
from collections.abc import Callable
from typing import Any, Generic, TypeVar


K = TypeVar("K")
V = TypeVar("V")


class BoundedTTLCache(Generic[K, V]):
    """Thread-safe LRU/TTL cache with request coalescing and copy isolation."""

    def __init__(
        self,
        max_entries: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        wait_timeout_seconds: float = 10.0,
    ) -> None:
        self.max_entries = max(0, int(max_entries))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._clock = clock
        self.wait_timeout_seconds = max(0.001, float(wait_timeout_seconds))
        self._lock = threading.RLock()
        self._entries: collections.OrderedDict[K, tuple[float, V]] = collections.OrderedDict()
        self._inflight: dict[K, threading.Event] = {}
        self._metrics = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "expirations": 0,
            "invalidations": 0,
        }

    @property
    def enabled(self) -> bool:
        return self.max_entries > 0 and self.ttl_seconds > 0

    def _get_locked(self, key: K) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= self._clock():
            del self._entries[key]
            self._metrics["expirations"] += 1
            return None
        self._entries.move_to_end(key)
        return copy.deepcopy(value)

    def get_or_compute(
        self,
        key: K,
        factory: Callable[[], V],
        cache_if: Callable[[V], bool] | None = None,
    ) -> tuple[V, bool]:
        should_cache = cache_if or (lambda _value: True)
        if not self.enabled:
            with self._lock:
                self._metrics["misses"] += 1
            return factory(), False

        wait_deadline = time.monotonic() + self.wait_timeout_seconds
        while True:
            with self._lock:
                cached = self._get_locked(key)
                if cached is not None:
                    self._metrics["hits"] += 1
                    return cached, True
                event = self._inflight.get(key)
                if event is None:
                    event = threading.Event()
                    self._inflight[key] = event
                    self._metrics["misses"] += 1
                    break
            remaining = wait_deadline - time.monotonic()
            if remaining <= 0 or not event.wait(remaining):
                raise TimeoutError("Cache computation is still occupied")

        try:
            value = factory()
            if should_cache(value):
                stored = copy.deepcopy(value)
                with self._lock:
                    self._entries[key] = (self._clock() + self.ttl_seconds, stored)
                    self._entries.move_to_end(key)
                    while len(self._entries) > self.max_entries:
                        self._entries.popitem(last=False)
                        self._metrics["evictions"] += 1
            return copy.deepcopy(value), False
        finally:
            with self._lock:
                completed = self._inflight.pop(key, None)
                if completed is not None:
                    completed.set()

    def invalidate(self, predicate: Callable[[K], bool] | None = None) -> int:
        with self._lock:
            if predicate is None:
                removed = len(self._entries)
                self._entries.clear()
            else:
                keys = [key for key in self._entries if predicate(key)]
                for key in keys:
                    del self._entries[key]
                removed = len(keys)
            self._metrics["invalidations"] += removed
            return removed

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return {**self._metrics, "entries": len(self._entries)}


class PersistentQueryCache:
    """Postgres-backed second cache tier, shared by every process.

    The in-process cache above is correct but private and mortal: it dies with
    the worker and is invisible to the CLI, the indexer and any second worker.
    This tier moves the same answers into the database the engine already owns,
    so a restart is not amnesia and a warm answer computed by one process serves
    all of them.

    Two things make that safe rather than merely fast.

    `scope_hash` is computed by the caller from the complete authorized context
    plus the model and ranking parameters, and it is part of the primary lookup
    key. Callers with different visibility hash differently and therefore cannot
    reach each other's rows. This class never relaxes that key, and never
    queries without it.

    `generation` is a per-project counter the caller bumps on every successful
    knowledge mutation. A row is only served for the generation it was computed
    under, so one process saving a chunk retires every other process's cached
    answers without having to reach into their memory. Rows are not deleted on
    invalidation - deletion races with in-flight readers and cannot reach a peer
    holding a copy in RAM, while a generation mismatch is decided at read time
    by whoever is reading.

    Every method fails open. A cache that is down must look exactly like a cache
    that is cold, never like an error: the caller loses speed, never an answer.
    """

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        ttl_seconds: float,
        max_rows: int,
        semantic_threshold: float,
        sweep_interval: int = 64,
    ) -> None:
        self._connection_factory = connection_factory
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_rows = max(0, int(max_rows))
        self.semantic_threshold = min(1.0, max(0.0, float(semantic_threshold)))
        self.sweep_interval = max(1, int(sweep_interval))
        self._lock = threading.RLock()
        self._stores_since_sweep = 0
        self._last_error: str | None = None
        self._metrics = {
            "exact_hits": 0,
            "semantic_hits": 0,
            "misses": 0,
            "stores": 0,
            "errors": 0,
            "sweeps": 0,
            "rows_removed": 0,
            "generation_bumps": 0,
        }

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0 and self.max_rows > 0

    def _count(self, metric: str, amount: int = 1) -> None:
        with self._lock:
            self._metrics[metric] += amount

    def _fail(self, exc: BaseException) -> None:
        """Count the failure and keep the reason.

        Failing open is right - a cache that is down must look like a cache that
        is cold - but a counter with the reason discarded means the next person
        to see a non-zero number has nothing to work with, and the cache stays
        broken because nobody can tell what broke. Keep the last message; it is
        one string and it is the difference between a mystery and a bug report.
        """
        with self._lock:
            self._metrics["errors"] += 1
            self._last_error = type(exc).__name__

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @staticmethod
    def _vector_literal(embedding) -> str | None:
        """Render an embedding the way pgvector parses it, or give up quietly.

        A malformed vector must degrade to "no semantic lookup", not to an
        exception on the retrieval path.
        """
        if embedding is None:
            return None
        try:
            values = [float(value) for value in embedding]
        except (TypeError, ValueError):
            return None
        if not values:
            return None
        return "[" + ",".join(repr(value) for value in values) + "]"

    def generation(self, project_id: str) -> int | None:
        """Current invalidation generation, or None when it cannot be read.

        None is distinct from 0: 0 is a real generation for a project nobody has
        written to yet, while None means the caller must not trust any cached
        row, because it cannot tell which generation it is in.
        """
        if not self.enabled:
            return None
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT generation FROM public.rag_cache_generation WHERE project_id = %s",
                        (project_id,),
                    )
                    row = cur.fetchone()
                conn.commit()
            return int(row[0]) if row else 0
        except Exception as exc:
            self._fail(exc)
            return None

    _BUMP_SQL = """
        INSERT INTO public.rag_cache_generation (project_id, generation, updated_at)
        VALUES (%s, 1, now())
        ON CONFLICT (project_id) DO UPDATE
            SET generation = public.rag_cache_generation.generation + 1,
                updated_at = now()
        RETURNING generation
    """

    def bump_generation(self, project_id: str, connection: Any = None) -> int | None:
        """Retire every cached answer for a project, for every process.

        `connection` exists because of where this gets called from: the last act
        of a write, while that write still holds a pooled connection. Reaching
        into the pool for a second one there is how a writer competes with
        itself - and under enough concurrent writers the pool runs out, the bump
        fails, and the failure is invisible because this path fails open. The
        write commits, every peer keeps serving the answer it should have
        retired, and nothing anywhere says so. Lend this the connection the
        caller already has and the second acquisition does not exist.

        The supplied connection must have no pending work: this commits it.
        Every caller invalidates immediately after its own commit, which is what
        makes that safe - and the ordering matters beyond tidiness. Bumping
        strictly after the commit is what stops a peer from reading pre-write
        rows and then filing them under the post-write generation, where nothing
        would ever retire them.
        """
        if not self.enabled:
            return None
        if connection is not None:
            return self._bump_on(connection, project_id)
        try:
            with self._connection_factory() as conn:
                return self._bump_on(conn, project_id)
        except Exception as exc:
            self._fail(exc)
            return None

    def _bump_on(self, connection: Any, project_id: str) -> int | None:
        try:
            with connection.cursor() as cur:
                cur.execute(self._BUMP_SQL, (project_id,))
                row = cur.fetchone()
            connection.commit()
        except Exception as exc:
            # A borrowed connection goes back to the pool, so it cannot be left
            # in a failed transaction for whoever gets it next.
            try:
                connection.rollback()
            except Exception:
                pass
            self._fail(exc)
            return None
        self._count("generation_bumps")
        return int(row[0]) if row else None

    def lookup(
        self,
        *,
        scope_hash: str,
        query_hash: str,
        generation: int,
        embedding_provider: Callable[[], Any] | None = None,
    ) -> tuple[dict | None, dict]:
        """Return (response, provenance). A miss is (None, {}).

        Exact first, then the nearest neighbour above the similarity threshold.
        The provenance says which path answered and how close the stored
        question was, because a semantic hit answers a question nobody asked and
        the caller is entitled to see that in the response.

        The embedding arrives as a callable rather than a value so that an exact
        hit never pays to vectorize a question it did not need to vectorize -
        which, on the CPU fallback path, is most of the latency the cache exists
        to remove.
        """
        if not self.enabled:
            return None, {}
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.rag_query_cache
                           SET hit_count = hit_count + 1, last_hit_at = now()
                         WHERE scope_hash = %s
                           AND query_hash = %s
                           AND generation = %s
                           AND expires_at > now()
                        RETURNING response
                        """,
                        (scope_hash, query_hash, generation),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        conn.commit()
                        self._count("exact_hits")
                        return row[0], {"layer": "l2-exact"}

                    vector = None
                    if embedding_provider is not None and self.semantic_threshold < 1.0:
                        try:
                            vector = self._vector_literal(embedding_provider())
                        except Exception:
                            vector = None
                    if vector is None:
                        conn.commit()
                        self._count("misses")
                        return None, {}

                    cur.execute(
                        """
                        WITH candidate AS (
                            SELECT id,
                                   1 - (query_embedding <=> %(vector)s::vector) AS similarity
                              FROM public.rag_query_cache
                             WHERE scope_hash = %(scope)s
                               AND generation = %(generation)s
                               AND expires_at > now()
                               AND query_embedding IS NOT NULL
                             ORDER BY query_embedding <=> %(vector)s::vector
                             LIMIT 1
                        )
                        UPDATE public.rag_query_cache AS c
                           SET hit_count = c.hit_count + 1, last_hit_at = now()
                          FROM candidate
                         WHERE c.id = candidate.id
                           AND candidate.similarity >= %(threshold)s
                        RETURNING c.response, c.query_text, candidate.similarity
                        """,
                        {
                            "vector": vector,
                            "scope": scope_hash,
                            "generation": generation,
                            "threshold": self.semantic_threshold,
                        },
                    )
                    row = cur.fetchone()
                conn.commit()
            if row is None:
                self._count("misses")
                return None, {}
            self._count("semantic_hits")
            return row[0], {
                "layer": "l2-semantic",
                "matched_query": row[1],
                "similarity": round(float(row[2]), 4),
            }
        except Exception as exc:
            self._fail(exc)
            return None, {}

    def store(
        self,
        *,
        project_id: str,
        scope_hash: str,
        query_hash: str,
        query_text: str,
        generation: int,
        response: dict,
        embedding=None,
        json_dumps: Callable[[Any], str] | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        dumps = json_dumps or (lambda value: json.dumps(value, ensure_ascii=False))
        try:
            payload = dumps(response)
        except (TypeError, ValueError) as exc:
            self._fail(exc)
            return False
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.rag_query_cache
                            (project_id, scope_hash, query_hash, query_text,
                             query_embedding, generation, response, expires_at)
                        VALUES (%s, %s, %s, %s, %s::vector, %s, %s::jsonb,
                                now() + make_interval(secs => %s))
                        ON CONFLICT (scope_hash, query_hash) DO UPDATE
                            SET response        = EXCLUDED.response,
                                generation      = EXCLUDED.generation,
                                expires_at      = EXCLUDED.expires_at,
                                query_text      = EXCLUDED.query_text,
                                query_embedding = EXCLUDED.query_embedding,
                                last_hit_at     = now()
                        """,
                        (
                            project_id,
                            scope_hash,
                            query_hash,
                            query_text,
                            self._vector_literal(embedding),
                            generation,
                            payload,
                            self.ttl_seconds,
                        ),
                    )
                conn.commit()
            self._count("stores")
        except Exception as exc:
            self._fail(exc)
            return False

        with self._lock:
            self._stores_since_sweep += 1
            due = self._stores_since_sweep >= self.sweep_interval
            if due:
                self._stores_since_sweep = 0
        if due:
            self.sweep()
        return True

    def sweep(self) -> int:
        """Drop expired rows, then trim the tail by least-recent use.

        Amortized over stores rather than run on a timer: the table only grows
        when something is written to it, so the write is the honest place to pay
        for keeping it bounded, and a process that never writes never needs to
        run housekeeping at all.
        """
        if not self.enabled:
            return 0
        removed = 0
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM public.rag_query_cache WHERE expires_at <= now()")
                    removed += cur.rowcount or 0
                    cur.execute(
                        """
                        DELETE FROM public.rag_query_cache
                         WHERE id IN (
                            SELECT id FROM public.rag_query_cache
                             ORDER BY last_hit_at DESC
                            OFFSET %s
                         )
                        """,
                        (self.max_rows,),
                    )
                    removed += cur.rowcount or 0
                conn.commit()
            self._count("sweeps")
            self._count("rows_removed", removed)
            return removed
        except Exception as exc:
            self._fail(exc)
            return 0

    def invalidate_all(self) -> int:
        """Empty the shared cache outright. For operators, not the hot path."""
        if not self.enabled:
            return 0
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM public.rag_query_cache")
                    removed = cur.rowcount or 0
                conn.commit()
            self._count("rows_removed", removed)
            return removed
        except Exception as exc:
            self._fail(exc)
            return 0

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return dict(self._metrics)


class PersistentAnswerCache:
    """Exact-match PostgreSQL cache for validated local model answers."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        ttl_seconds: float,
        max_rows: int,
        sweep_interval: int = 64,
    ) -> None:
        self._connection_factory = connection_factory
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_rows = max(0, int(max_rows))
        self.sweep_interval = max(1, int(sweep_interval))
        self._lock = threading.RLock()
        self._stores_since_sweep = 0
        self._last_error: str | None = None
        self._metrics = {
            "exact_hits": 0,
            "misses": 0,
            "stores": 0,
            "errors": 0,
            "sweeps": 0,
            "rows_removed": 0,
        }

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0 and self.max_rows > 0

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def _fail(self, exc: BaseException) -> None:
        with self._lock:
            self._metrics["errors"] += 1
            self._last_error = type(exc).__name__

    def lookup(
        self, *, project_id: str, scope_hash: str, prompt_hash: str, generation: int
    ) -> tuple[str | None, str]:
        if not self.enabled:
            return None, "disabled"
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.rag_local_answer_cache
                           SET hit_count = hit_count + 1, last_hit_at = now()
                         WHERE project_id = %s
                           AND scope_hash = %s
                           AND prompt_hash = %s
                           AND generation = %s
                           AND expires_at > now()
                        RETURNING answer
                        """,
                        (project_id, scope_hash, prompt_hash, generation),
                    )
                    row = cur.fetchone()
                conn.commit()
            with self._lock:
                self._metrics["exact_hits" if row else "misses"] += 1
            return (str(row[0]), "hit") if row else (None, "miss")
        except Exception as exc:
            self._fail(exc)
            return None, "unavailable"

    def store(
        self,
        *,
        project_id: str,
        scope_hash: str,
        prompt_hash: str,
        generation: int,
        answer: str,
    ) -> bool:
        if not self.enabled or not answer.strip():
            return False
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.rag_local_answer_cache
                            (project_id, scope_hash, prompt_hash, generation,
                             answer, expires_at)
                        VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))
                        ON CONFLICT (scope_hash, prompt_hash) DO UPDATE
                            SET generation = EXCLUDED.generation,
                                answer = EXCLUDED.answer,
                                expires_at = EXCLUDED.expires_at,
                                last_hit_at = now()
                        """,
                        (
                            project_id,
                            scope_hash,
                            prompt_hash,
                            generation,
                            answer,
                            self.ttl_seconds,
                        ),
                    )
                conn.commit()
            with self._lock:
                self._metrics["stores"] += 1
                self._stores_since_sweep += 1
                should_sweep = self._stores_since_sweep >= self.sweep_interval
                if should_sweep:
                    self._stores_since_sweep = 0
            if should_sweep:
                self.sweep()
            return True
        except Exception as exc:
            self._fail(exc)
            return False

    def sweep(self) -> int:
        if not self.enabled:
            return 0
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM public.rag_local_answer_cache WHERE expires_at <= now()"
                    )
                    removed = cur.rowcount or 0
                    cur.execute(
                        """
                        DELETE FROM public.rag_local_answer_cache
                         WHERE id IN (
                            SELECT id FROM public.rag_local_answer_cache
                             ORDER BY last_hit_at DESC
                            OFFSET %s
                         )
                        """,
                        (self.max_rows,),
                    )
                    removed += cur.rowcount or 0
                conn.commit()
            with self._lock:
                self._metrics["sweeps"] += 1
                self._metrics["rows_removed"] += removed
            return removed
        except Exception as exc:
            self._fail(exc)
            return 0

    def invalidate_all(self) -> int:
        if not self.enabled:
            return 0
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM public.rag_local_answer_cache")
                    removed = cur.rowcount or 0
                conn.commit()
            with self._lock:
                self._metrics["rows_removed"] += removed
            return removed
        except Exception as exc:
            self._fail(exc)
            return 0

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return dict(self._metrics)
