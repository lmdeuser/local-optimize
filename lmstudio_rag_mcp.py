#!/usr/bin/env python3
"""Read-only local RAG tool for a local LM Studio model."""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from mcp.server.fastmcp import FastMCP

from mcp_server import rag_search as _rag_search


mcp = FastMCP("LM Studio Local RAG")


@mcp.tool()
async def search_local_rag(
    query: str,
    working_directory: str = "",
    project_id: str = "",
    root_id: str = "",
    global_search: bool = False,
    limit: int = 5,
) -> str:
    """Search local memory by client cwd, explicit project, or explicit global scope."""
    safe_limit = max(1, min(int(limit), 20))
    return await _rag_search(
        query=query,
        limit=safe_limit,
        include_metadata=True,
        verbosity="compact",
        max_content_chars=1800,
        project_id=project_id,
        root_id=root_id,
        working_directory=working_directory,
        global_search=global_search,
        read_scope="shared",
    )


def main() -> None:
    if sys.stdin.isatty():
        print("LM Studio Local RAG MCP expects stdio from its MCP client.", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
