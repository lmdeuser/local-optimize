# MCP tools

The project owns one canonical MCP server entry: `local-rag`. The configuration generator mutates only this entry. An owned legacy `local_rag` entry may be migrated after an exact command/argument ownership check; a conflicting foreign entry fails closed.

All other MCP entries are opaque third-party data. The generator deep-preserves them, does not inspect nested credential-bearing fields, backs up the whole JSON file, and commits with an atomic same-directory replace. Multi-file updates self-restore earlier files if a later write fails.

## Isolation boundary

`mcp_server.py` accepts only `MCP_TRANSPORT=stdio`. HTTP, SSE, and streamable HTTP fail closed before `mcp.run`: configured bearer secrets and a loopback bind are not transport authentication. Network MCP remains disabled until every endpoint enforces authorization and each client receives an isolated authorized session.

The full RAG MCP server is for approved local-only clients. Cloud-capable clients, including Kimi Code running a cloud model, must not register it directly: tool results would enter the cloud conversation. `lmstudio_rag_mcp.py` is a separate read-only LM Studio wrapper exposing only `search_local_rag`; it is not a cloud adapter.

Cloud escalation through `route_query_tool` returns only a routing signal. Retrieved text, answers grounded in RAG, citations, source metadata, scores, coverage, summaries, and derived facts are never attached to that signal.

## RAG context

Search, agentic search, and save expose one explicit context selector and access intent:

- `project_id` + `root_id` + `context_fingerprint` for explicit registered scope;
- `working_directory` for server-side longest-root resolution; one MCP `file://` root can be its workspace hint, while multiple roots require clarification;
- `global_search=true` only on read-only search/agentic search, without a client fingerprint and without project/root/cwd.

`context_fingerprint` covers visible roots and nested exclusions. `write_visibility`, `read_scope`, `agent_id`, `session_id`, `source_types`, `language`, `validation_status`, `min_score`, and `include_metadata` remain independent. Invalid access, missing/ambiguous context or validation values fail without fallback or widening. HTTP authorization identity is server-owned, so client-supplied owner fields are wire-compatible hints rather than authority. Global responses include project/root origins and `partial`/completed/omitted project metadata; mutations and local-answer cache reject global context.

## Tools

`mcp_server.py` exposes 14 tools:

- `load_skills`: strict manifest-only, bounded session loading.
- `rag_search`: scoped hybrid retrieval followed by final evidence ranking.
- `rag_agentic_search`: bounded ID-based relation traversal with evidence ACL.
- `rag_save`: atomic canonical-content UPSERT plus provenance link.
- `rag_deprecate`: retire a fact as deprecated or invalidated without deleting its history.
- `rag_verify`: confirm a fact and set its next-review interval.
- `rag_stale_report`: report facts requiring renewed verification.
- `rag_consolidate`: consolidate episodic memory into scoped semantic graph facts.
- `rag_memory_optimize`: report semantic duplicates; safe-no-delete.
- `rag_feedback`: record scoped user or agent feedback.
- `delegate_simple_task`: bounded local-model help for non-sensitive extraction, classification, normalization, formatting, or short summaries. It calls loopback Ollama only, requires full-GPU placement from live telemetry before generation, and has no cloud fallback. For project/factual work, callers must first retrieve relevant RAG evidence and pass the needed excerpts; retrieved RAG data must never be sent to a cloud model if local inference fails. The returned draft requires caller verification.
- `ask_local_model`: direct ungrounded local generation, requiring explicit `allow_ungrounded=True`.
- `route_query_tool`: confidence routing using retrieved chunks and their provenance. Optional caller text stays separate and cannot become retrieved evidence.
- `rag_assistant`: local-only RAG answer path; retrieval or inference failure returns unavailable/fallback without cloud routing.

Index/delete/move HTTP operations use the same `ProjectContext`. File moves execute server-side in one PostgreSQL transaction and return an affected-row count that callers must verify.
