"""The cache key must cover everything the search SQL is allowed to read.

Both cache tiers key on `RagEngine._search_scope_key`. If the search SQL ever
starts filtering on a `ProjectContext` field that key does not cover, two callers
with different authorization collapse onto the same key and the cache serves one
of them the other's rows. That failure is silent, it is a data leak rather than
a slowdown, and no functional test would notice it - the cache would simply
return a plausible answer to the wrong caller.

So one test here does not exercise behaviour at all: it re-derives, from the
source of `_retrieve_candidates`, the set of context fields the search actually
consults, and fails if that set has grown beyond what the key covers. Anyone
adding a filter is then forced to decide, at the moment of adding it, whether it
belongs in the cache key.

The rest pin the other half: identity is included exactly where
`build_read_scope_sql` binds it into SQL and nowhere else. Including it anyway
would be harmless for correctness and would make the shared cache unable to hit
across sessions, since the MCP server mints a fresh session id per process.
"""

import ast
import inspect
import pathlib

import pytest

import rag_engine
from project_context import ProjectContext, ReadScope, ValidationStatus, WriteVisibility


pytestmark = pytest.mark.unit


def make_context(tmp_path, **overrides):
    values = {
        "project_id": "project-a",
        "project_name": "Project A",
        "root_id": "root-a",
        "project_root": str(tmp_path),
        "allowed_roots": (str(tmp_path),),
        "write_visibility": WriteVisibility.SESSION_PRIVATE,
        "read_scope": ReadScope.SHARED,
        "agent_id": "agent-a",
        "session_id": "session-a",
        "validation_status": ValidationStatus.VALIDATED,
    }
    values.update(overrides)
    return ProjectContext(**values)


def scope_key(context, limit=5):
    engine = object.__new__(rag_engine.RagEngine)
    return rag_engine.RagEngine._search_scope_key(engine, context, limit)


def context_fields_read_by_search() -> set[str]:
    """Every `context.<field>` the hybrid search body actually touches."""
    source = pathlib.Path(inspect.getfile(rag_engine)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_retrieve_candidates":
            return {
                child.attr
                for child in ast.walk(node)
                if isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == "context"
            }
    raise AssertionError("_retrieve_candidates not found in rag_engine")


def test_scope_key_covers_every_context_field_the_search_reads():
    consulted = context_fields_read_by_search()
    assert consulted, "the AST walk found nothing; the guard would pass vacuously"
    uncovered = consulted - set(rag_engine.RagEngine.SEARCH_SCOPE_FIELDS)
    assert not uncovered, (
        f"_retrieve_candidates now filters on {sorted(uncovered)}, which "
        "RagEngine._search_scope_key does not include. Two callers differing only "
        "in those fields would share a cache entry and see each other's rows. "
        "Add the field to the scope key and to SEARCH_SCOPE_FIELDS, or establish "
        "that it cannot change the result set."
    )


def test_the_search_does_not_hand_context_to_anyone_else():
    """The guard above reads one function; keep the reading worth something.

    It collects `context.<field>` inside `_retrieve_candidates`. That is the whole
    picture only for as long as the context stays there. Pass it to a helper and
    the helper's reads become invisible to the guard, which would then keep
    passing while the search quietly started filtering on something the cache key
    does not cover - the exact failure the guard exists to catch, now with a
    green test on top of it.

    So: the context may be type-checked and read, not handed on. A helper that
    genuinely needs it should take the fields it needs, where the guard can see
    them.
    """
    source = pathlib.Path(inspect.getfile(rag_engine)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_retrieve_candidates"
    )
    handed_to = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        arguments = list(call.args) + [kw.value for kw in call.keywords]
        if any(isinstance(a, ast.Name) and a.id == "context" for a in arguments):
            handed_to.add(ast.unparse(call.func))
    assert handed_to <= {"isinstance"}, (
        f"_retrieve_candidates passes the context to {sorted(handed_to - {'isinstance'})}. "
        "Whatever those read is invisible to the scope-key guard. Pass the "
        "individual fields instead, or extend the guard to follow the call."
    )

    dynamic = [
        ast.unparse(call)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and ast.unparse(call.func) == "getattr"
        and call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "context"
    ]
    assert not dynamic, f"getattr on the context defeats the guard: {dynamic}"


def test_shared_scope_ignores_identity_so_the_cache_can_be_shared(tmp_path):
    first = make_context(tmp_path, read_scope=ReadScope.SHARED, agent_id="a", session_id="s1")
    second = make_context(tmp_path, read_scope=ReadScope.SHARED, agent_id="b", session_id="s2")
    assert scope_key(first) == scope_key(second)


def test_agent_scope_separates_agents_but_not_sessions(tmp_path):
    base = make_context(tmp_path, read_scope=ReadScope.AGENT, agent_id="a", session_id="s1")
    other_session = make_context(
        tmp_path, read_scope=ReadScope.AGENT, agent_id="a", session_id="s2"
    )
    other_agent = make_context(
        tmp_path, read_scope=ReadScope.AGENT, agent_id="b", session_id="s1"
    )
    assert scope_key(base) == scope_key(other_session)
    assert scope_key(base) != scope_key(other_agent)


def test_session_scope_separates_sessions(tmp_path):
    first = make_context(tmp_path, read_scope=ReadScope.SESSION, session_id="s1")
    second = make_context(tmp_path, read_scope=ReadScope.SESSION, session_id="s2")
    assert scope_key(first) != scope_key(second)


def test_read_scope_itself_always_separates(tmp_path):
    keys = {
        scope_key(make_context(tmp_path, read_scope=scope))
        for scope in (ReadScope.PUBLIC, ReadScope.SHARED, ReadScope.AGENT, ReadScope.SESSION)
    }
    assert len(keys) == 4


def test_filters_and_ranking_parameters_separate(tmp_path, monkeypatch):
    base = make_context(tmp_path)
    assert scope_key(base) != scope_key(base, limit=7)
    assert scope_key(base) != scope_key(make_context(tmp_path, min_score=0.5))
    assert scope_key(base) != scope_key(make_context(tmp_path, source_types=("code",)))
    assert scope_key(base) != scope_key(make_context(tmp_path, project_id="project-b"))

    # Re-ranking under different parameters is a different answer to the same
    # question, and must not be served from the previous parameters' cache.
    before = scope_key(base)
    monkeypatch.setattr(rag_engine, "RRF_K", rag_engine.RRF_K + 1)
    assert scope_key(base) != before


def test_embedding_provenance_separates(tmp_path, monkeypatch):
    """A re-embed changes what similarity means; cached rows must not survive it."""
    base = make_context(tmp_path)
    before = scope_key(base)
    monkeypatch.setattr(rag_engine, "EMBEDDING_VERSION", "999.0")
    assert scope_key(base) != before
