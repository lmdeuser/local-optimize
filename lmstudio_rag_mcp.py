#!/usr/bin/env python3
"""Read-only local RAG tool for a local LM Studio model.

This wrapper exposes one search tool over stdio. It intentionally does not
register the full multi-agent MCP server, write tools, local generation tools,
or any cloud provider integrations.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

# Reuse the existing ACL-aware search implementation. Importing this module
# does not start its MCP server; only the small wrapper below is run.
from mcp_server import (
    DEFAULT_PROJECT_ID,
    DEFAULT_ROOT_ID,
    rag_search as _rag_search,
)


mcp = FastMCP("LM Studio Local RAG")


@mcp.tool()
async def search_local_rag(query: str) -> str:
    """Search local project memory. Read-only; results stay in this local chat."""
    return await _rag_search(
        query=query,
        limit=5,
        include_metadata=True,
        verbosity="compact",
        max_content_chars=1800,
        project_id=DEFAULT_PROJECT_ID,
        root_id=DEFAULT_ROOT_ID,
        read_scope="shared",
    )


def main() -> None:
    # MCP stdio must remain direct and local; do not enable HTTP transport here.
    if sys.stdin.isatty():
        print("LM Studio Local RAG MCP expects stdio from its MCP client.", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
