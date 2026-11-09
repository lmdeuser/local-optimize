# Memory & 2-Layer RAG Protocol (v4.0)

This protocol defines how agents manage long-term memory, session state, delta tracking, and LLM escalation within the Antigravity ecosystem.

---

## 1. Architecture Overview

Memory is backed by a centralized PostgreSQL 18 database (`aiagent`) managed by the FastAPI server on `http://127.0.0.1:8181`.

### Data Flow
1. **Query RAG**: Agent calls `rag_search(query)` or `rag_agentic_search(query)`.
2. **Score Evaluation**:
   - `top_score >= 0.65`: **Context Sufficient**. Local Qwen model or immediate RAG response is used (`rag_answer`).
   - `top_score < 0.65`: **Escalation Required**. Return a routing signal only. Never attach RAG chunks, citations, metadata, scores, summaries, or derived facts to a cloud request.
   - The cut is one setting (`RAG_RELEVANCE_FLOOR`) shared by the engine, the HTTP
     `route` decision and the escalation router — measured, not chosen. At 0.65 it
     keeps 92.5% of answerable queries and admits none of the unanswerable ones.
     Do not move it without re-running the calibration probe; see
     `docs/score-calibration.md`.
3. **Session Context Delta**:
   - The memory server tracks hashes (`content_hash`) per `agent_id` and `session_id`.
   - Only new or modified chunks are returned to prevent context window inflation.
4. **Crystallize Experience**:
   - When a bug fix, architectural decision, or optimal prompt response is generated, the agent calls `rag_save(text)` to store it in long-term vector memory.

---

## 2. Agent Guidelines for Memory Use

- **Search First**: Before asking the user or generating complex code from scratch, query `rag_search`.
- **Save Best Solutions**: Always call `rag_save` when resolving non-trivial issues or establishing project conventions.
- **No Spamming**: Do not save raw temporary logs or unverified trial-and-error code. Save only validated, high-value technical facts.
- **Correct With `rag_deprecate`, Not `rag_save`**: When a stored fact turns out to
  be wrong or superseded, call `rag_deprecate(chunk_id=...)`. Saving the correction
  only adds a row — the stale fact keeps being retrieved, and outranks the
  correction whenever it is older or carries a higher `validation_status`. Check
  `updated` in the response: `0` means nothing matched and the store is unchanged.
- **Visibility**: `rag_save` defaults to `shared`, which is what the next session
  can read. `session-private` rows are unreadable once the session ends — the
  server mints a fresh session id per process — so use it only for genuine scratch.
- **Re-Confirm What You Re-Checked**: call `rag_verify(chunk_id=...)` after
  actually verifying a stored fact against reality. Ranking ages a chunk from its
  last confirmation, not from when it was written, so this is what stops a
  still-true fact from decaying into `stale`. For facts about external systems
  (DOM selectors, undocumented endpoints, UI behaviour) pass
  `recheck_after_days=N`; past that deadline the fact is still returned but
  tagged `freshness_status: overdue`. `rag_stale_report` lists what is due.
- **Maintenance**: `rag_memory_optimize` reports near-duplicate pairs by cosine
  similarity; with `apply=true` it retires the later copy as `deprecated` with a
  `superseded_by` link. It never deletes, so provenance survives.
- **Multi-Agent Messaging (A2A)**: Utilize `agent_messages` table for inter-agent asynchronous communication (`knowledge_push`, `query`, `correction`, `invalidate`).
