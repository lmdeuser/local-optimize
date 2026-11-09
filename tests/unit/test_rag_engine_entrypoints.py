import sys

import pytest

import rag_engine

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("arguments", "method_name"),
    [
        (["save", "fixture text"], "save"),
        (["search", "fixture query"], "search"),
        (["agentic-search", "fixture query"], "agentic_search"),
        (["index-code", "fixture.py"], "index_code"),
        (["log-feedback", "positive", "fixture query"], "log_feedback"),
        (["consolidate"], "consolidate"),
        (["memory-optimize"], "memory_optimize"),
    ],
)
def test_cli_operations_pass_boundary_context(monkeypatch, arguments, method_name):
    boundary_context = object()
    calls = []

    class FakeEngine:
        def __getattr__(self, name):
            def operation(*args, **kwargs):
                calls.append((name, args, kwargs))
                return {"status": "success"}

            return operation

        def close(self):
            calls.append(("close", (), {}))

    monkeypatch.setattr(rag_engine, "RagEngine", FakeEngine)
    monkeypatch.setattr(
        rag_engine, "build_default_project_context", lambda *_args, **_kwargs: boundary_context
    )
    monkeypatch.setattr(sys, "argv", ["rag_engine.py", *arguments])

    rag_engine.main()

    operation = next(call for call in calls if call[0] == method_name)
    assert operation[1][0] is boundary_context
    assert calls[-1][0] == "close"


@pytest.mark.parametrize(
    "method_name",
    ["agentic_search", "auto_index_codebase", "index_document", "index_code", "consolidate", "memory_optimize"],
)
def test_scoped_methods_reject_missing_project_context(method_name):
    engine = object.__new__(rag_engine.RagEngine)
    method = getattr(engine, method_name)
    extra_args = {
        "agentic_search": ("query",),
        "index_document": ("fixture.md",),
        "index_code": ("fixture.py",),
    }.get(method_name, ())
    with pytest.raises(TypeError, match="ProjectContext"):
        method(None, *extra_args)
