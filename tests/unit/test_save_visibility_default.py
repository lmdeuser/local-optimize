"""`rag_save` must default to a visibility the next session can read.

The store exists so agents stop re-deriving knowledge every session. A default
that writes rows only the current session can see inverts that: the obvious call
appears to succeed, returns a chunk_id, and produces knowledge nobody will ever
retrieve — `build_read_scope_sql` requires a matching `session_id`, and the MCP
server mints a fresh one per process.
"""
import ast
from pathlib import Path

import pytest

from rag_engine import build_read_scope_sql


pytestmark = pytest.mark.unit

MCP_SERVER = Path(__file__).resolve().parents[2] / "mcp_server.py"


def default_of(tool_name: str, parameter: str):
    tree = ast.parse(MCP_SERVER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == tool_name:
            args = node.args.args + node.args.kwonlyargs
            defaults = list(node.args.defaults) + [
                d for d in node.args.kw_defaults if d is not None
            ]
            offset = len(args) - len(defaults)
            for index, arg in enumerate(args):
                if arg.arg == parameter and index >= offset:
                    return ast.literal_eval(defaults[index - offset])
    raise AssertionError(f"{tool_name}({parameter}=...) not found")


def test_rag_save_defaults_to_a_readable_visibility():
    assert default_of("rag_save", "write_visibility") == "shared"


def test_the_previous_default_really_was_unreadable_across_sessions():
    """Pins *why* the default changed, so it is not casually reverted."""
    sql, params = build_read_scope_sql("session", "agent-a", "session-b")

    assert "session-private" in sql
    assert params == ["agent-a", "agent-a", "session-b"]


def test_session_scope_without_identity_matches_nothing():
    """The fail-closed branch behind the lost-knowledge default."""
    assert build_read_scope_sql("session", "", "")[0] == "1=0"
    assert build_read_scope_sql("agent", "", "")[0] == "1=0"
