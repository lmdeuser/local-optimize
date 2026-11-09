"""Retirement must survive a re-save, and the duplicate report must be true.

Two failures that let a store rot quietly:

* Re-saving text that was previously retired brought it back to life. Agents save
  what they learn on every session, so the same fact gets written repeatedly —
  which meant a correction had a shelf life of exactly one re-save.
* `memory_optimize` grouped by `dedup_key` to find duplicates. That column
  carries a UNIQUE index, so `HAVING count(*) > 1` is unsatisfiable and the
  report was structurally always empty — while the live store held 446 chunk
  pairs at cosine >= 0.995.
"""
import pytest

import rag_engine
from project_context import ProjectContext, ReadScope, ValidationStatus, WriteVisibility


pytestmark = pytest.mark.unit


def make_context(tmp_path, *, validation_status=ValidationStatus.UNVERIFIED):
    return ProjectContext(
        "project-a", "Project A", "root-a", str(tmp_path), (str(tmp_path),),
        WriteVisibility.SHARED, ReadScope.SHARED, "agent-a", "session-a",
        validation_status=validation_status,
    )


class RecordingCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        self.connection.calls.append((str(query), list(params)))

    def fetchone(self):
        return (7,)

    def fetchall(self):
        return self.connection.rows

    @property
    def rowcount(self):
        return 1


class RecordingConnection:
    def __init__(self, rows=()):
        self.calls = []
        self.rows = list(rows)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def build_engine(connection):
    engine = object.__new__(rag_engine.RagEngine)
    engine._get_conn = lambda: connection
    engine._put_conn = lambda *_args, **_kwargs: None
    engine.encode = lambda _text, **_kw: __import__("numpy").zeros(4, dtype="float32")
    return engine


def test_resaving_does_not_resurrect_a_retired_fact(tmp_path):
    """A correction must outlive the next routine save of the same text.

    `DO UPDATE SET validation_status = EXCLUDED.validation_status` applied the
    incoming status unconditionally, so an agent re-saving a fact it had already
    retired quietly restored it to `unverified` and back into retrieval.
    """
    connection = RecordingConnection()
    engine = build_engine(connection)

    engine.save(make_context(tmp_path), "a fact that was already retired")

    insert = next(q for q, _ in connection.calls if "INSERT INTO public.project_knowledge" in q)
    normalised = " ".join(insert.split())
    assert "ON CONFLICT" in normalised
    assert "deprecated" in normalised and "invalidated" in normalised, (
        "the conflict clause applies the incoming status unconditionally, so a "
        f"re-save revives a retired fact: {normalised[normalised.find('ON CONFLICT'):]}"
    )


def test_duplicate_report_does_not_group_by_a_unique_column(tmp_path):
    """Grouping by `dedup_key` cannot ever find a duplicate — it is UNIQUE."""
    connection = RecordingConnection()
    engine = build_engine(connection)

    engine.memory_optimize(make_context(tmp_path))

    queries = [" ".join(q.split()) for q, _ in connection.calls]
    assert not any(
        "GROUP BY dedup_key HAVING count(*) > 1" in q for q in queries
    ), "duplicate detection still groups by a column with a UNIQUE index"


def test_duplicate_report_uses_the_similarity_threshold_it_advertises(tmp_path):
    """`sim_threshold` was accepted, echoed back, and never used in any query."""
    connection = RecordingConnection()
    engine = build_engine(connection)

    engine.memory_optimize(make_context(tmp_path), sim_threshold=0.97)

    assert any(
        0.97 in [p for p in params if isinstance(p, float)]
        for _q, params in connection.calls
    ), "sim_threshold never reached the database"


def test_duplicate_search_is_bounded(tmp_path):
    """Pairwise similarity over a growing store must not become unbounded work."""
    connection = RecordingConnection()
    engine = build_engine(connection)

    engine.memory_optimize(make_context(tmp_path))

    similarity_queries = [
        q for q, _ in connection.calls if "embedding" in q and "project_knowledge" in q
    ]
    assert similarity_queries, "no similarity query was issued at all"
    assert all("LIMIT" in q for q in similarity_queries), similarity_queries


def test_two_parts_of_one_file_are_never_treated_as_duplicates(tmp_path):
    """Different offsets in the same file are different content, not copies.

    Structurally repetitive files — SQL DDL, JSON manifests, shell case arms —
    have chunks that score near-identical to each other. Measured on the live
    store, parts 3/7 and 6/7 of one migration scored 0.9968. Retiring either
    would delete content while reporting a duplicate removed.
    """
    connection = RecordingConnection()
    engine = build_engine(connection)

    engine.memory_optimize(make_context(tmp_path))

    similarity_query = next(
        " ".join(q.split()) for q, _ in connection.calls if "n_file_path" in q
    )
    assert "a.file_path = n_file_path" in similarity_query
    assert "start_locator IS DISTINCT FROM n_start_locator" in similarity_query


def test_the_duplicate_threshold_is_far_above_the_noise_floor(tmp_path):
    """A cosine threshold is meaningless without the model's scale.

    0.92 was reasonable under all-MiniLM-L6-v2, where unrelated text sits near
    0.35. Under multilingual-e5-small unrelated text sits at 0.86, so the same
    number sits almost on the noise floor.
    """
    profile = rag_engine.get_model_profile()

    assert rag_engine.DUPLICATE_SIM_THRESHOLD > profile["cos_hi"], (
        "the duplicate threshold is below where a *true match* already scores, "
        "so consolidation would retire merely-related knowledge"
    )


def test_query_expansion_drops_the_word_reversal_variant():
    """Reversing word order yields an identical tsquery and a near-identical vector.

    `websearch_to_tsquery` is order-insensitive for a bag of terms, so the
    reversed variant produced the exact same lexical candidate set while costing
    a second full vector scan and a second FTS scan per search.
    """
    engine = object.__new__(rag_engine.RagEngine)

    expansions = engine._expand_query("session private visibility rules")

    assert "rules visibility private session" not in expansions


def test_duplicate_search_parameter_alignment_with_session_scope(tmp_path):
    """Parameters in LATERAL join must align with placeholders when read_scope includes owners."""
    connection = RecordingConnection()
    engine = build_engine(connection)
    ctx = ProjectContext(
        "project-a", "Project A", "root-a", str(tmp_path), (str(tmp_path),),
        WriteVisibility.SHARED, ReadScope.SESSION, "agent-a", "session-a",
    )
    engine.memory_optimize(ctx)
    similarity_call = next(
        (q, params) for q, params in connection.calls if "n_file_path" in q
    )
    _q, params = similarity_call
    proj_idx = params.index("project-a")
    assert params[proj_idx + 1] == "agent-a", "session scope owner was displaced by DUPLICATE_NEIGHBOURS"
    assert params[proj_idx + 4] == rag_engine.DUPLICATE_NEIGHBOURS, "DUPLICATE_NEIGHBOURS should follow params_b"

