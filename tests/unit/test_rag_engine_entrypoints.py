import subprocess
import sys

import pytest

import rag_engine
from project_context import ProjectContext, ReadScope, WriteVisibility

pytestmark = pytest.mark.unit


def _make_context(tmp_path):
    return ProjectContext(
        "project-a", "Project A", "root-a", str(tmp_path), (str(tmp_path),),
        WriteVisibility.SESSION_PRIVATE, ReadScope.SESSION, "agent-a", "session-a",
    )


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
    [
        "agentic_search", "auto_index_codebase", "index_document",
        "index_code", "consolidate", "memory_optimize", "execute_and_learn",
    ],
)
def test_scoped_methods_reject_missing_project_context(method_name):
    engine = object.__new__(rag_engine.RagEngine)
    method = getattr(engine, method_name)
    extra_args = {
        "agentic_search": ("query",),
        "index_document": ("fixture.md",),
        "index_code": ("fixture.py",),
        "execute_and_learn": (["python3", "-V"],),
    }.get(method_name, ())
    with pytest.raises(TypeError, match="ProjectContext"):
        method(None, *extra_args)


def test_ram_page_fault_rolls_back_and_marks_pool_error_on_failure(tmp_path):
    class FailingCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            raise RuntimeError("simulated db error")

    class FailingConn:
        def __init__(self):
            self.rollbacks = 0

        def cursor(self):
            return FailingCursor()

        def rollback(self):
            self.rollbacks += 1

    conn = FailingConn()
    returned = []
    engine = object.__new__(rag_engine.RagEngine)
    engine._get_conn = lambda: conn
    engine._put_conn = lambda connection, error=False: returned.append((connection, error))

    ok = engine.ram_page_fault(_make_context(tmp_path), "missing-page")

    assert ok is False
    assert conn.rollbacks == 1
    assert returned == [(conn, True)]

    caller_conn = FailingConn()
    returned.clear()
    ok_caller = engine.ram_page_fault(_make_context(tmp_path), "missing-page", conn=caller_conn)
    assert ok_caller is False
    assert caller_conn.rollbacks == 0
    assert returned == []


def test_auto_index_codebase_uses_project_and_owner_scope(monkeypatch, tmp_path):
    target_file = tmp_path / "module.py"
    target_file.write_text("x = 1\n", encoding="utf-8")
    calls = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=()):
            calls.append((" ".join(str(query).split()), list(params)))

        def fetchone(self):
            return None

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def rollback(self):
            pass

    engine = object.__new__(rag_engine.RagEngine)
    engine._exiting = False
    engine._get_conn = lambda: FakeConn()
    engine._put_conn = lambda *_args, **_kwargs: None
    engine.index_code = lambda *_args, **_kwargs: {"status": "success"}

    def stop_sleep(_seconds):
        engine._exiting = True

    monkeypatch.setattr(rag_engine.time, "sleep", stop_sleep)

    engine.auto_index_codebase(_make_context(tmp_path))

    select_query, select_params = next(c for c in calls if c[0].startswith("SELECT metadata"))
    update_query, update_params = next(c for c in calls if c[0].startswith("UPDATE public.kb_entities"))
    for query, params in ((select_query, select_params), (update_query, update_params)):
        assert "project_id = %s" in query
        assert "visibility = %s" in query
        assert "agent_id = %s" in query
        assert "session_id = %s" in query
        assert "project-a" in params
        assert "agent-a" in params
        assert "session-a" in params
    assert "ee.root_id = %s" in select_query
    assert "root-a" in select_params


def test_execute_and_learn_uses_project_root_cwd_and_scoped_sql(monkeypatch, tmp_path):
    monkeypatch.setenv("ENABLE_EXECUTE_AND_LEARN", "1")
    recorded_subprocess = {}
    calls = []

    def fake_run(cmd, **kwargs):
        recorded_subprocess["cmd"] = cmd
        recorded_subprocess["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr='  File "broken.py", line 10, in <module>\nError\n',
        )

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=()):
            calls.append((" ".join(str(query).split()), list(params)))

        def fetchone(self):
            return (42,)

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def rollback(self):
            pass

    monkeypatch.setattr(subprocess, "run", fake_run)
    engine = object.__new__(rag_engine.RagEngine)
    engine._get_conn = lambda: FakeConn()
    engine._put_conn = lambda *_args, **_kwargs: None
    engine.save = lambda *_args, **_kwargs: {"status": "success"}
    engine._upsert_entity = lambda *_args, **_kwargs: 100
    engine._upsert_relation = lambda *_args, **_kwargs: 200

    ctx = _make_context(tmp_path)
    result = engine.execute_and_learn(ctx, ["python3", "-m", "pytest"])

    assert result["status"] == "success"
    assert recorded_subprocess["cwd"] == str(tmp_path)
    select_query, select_params = next(c for c in calls if "FROM public.kb_entities" in c[0])
    rel_query, rel_params = next(c for c in calls if "UPDATE public.kb_relations" in c[0])
    assert "project_id = %s" in select_query
    assert "visibility = %s" in select_query
    assert "ee.root_id = %s" in select_query
    assert "root-a" in select_params
    assert "project_id = %s" in rel_query
    assert "visibility = %s" in rel_query
    assert "agent_id = %s" in rel_query
    assert "session_id = %s" in rel_query
    assert rel_params[:4] == ["project-a", "session-private", "agent-a", "session-a"]
