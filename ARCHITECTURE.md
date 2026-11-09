# Local-First Hybrid Inference Architecture

## Architecture Overview

`Query → Local RAG Engine → Adaptive Model Router → [Layer 1: Local Model in VRAM] ──(if confident)──> Final Local Response (0 Cloud Tokens)`
                                                     `│`
                                                     `└──(if escalate)──> [Isolated cloud handoff: user request only; zero RAG data]`

The system implements a **Local-First Hybrid Inference Architecture** with a **Two-Layer Cascade (локальная двухслойка)**:

- **Layer 1 (Local In-Memory Execution)**:
  - **Single local LLM**: `local_model.json` selects `qwen2.5-coder:7b` and an 8,192-token context. Conflicting process overrides are rejected.
  - **Shared inference transport**: MCP direct, delegation, router, compatibility warm-up and harvester use `local_inference.py` through `fast_router.query_local_ollama`. A nonblocking cross-process lock permits one generation. Startup does not preload. First use loads the selected model with `keep_alive=-1`; another resident model, partial GPU placement, or conflicting context yields explicit unavailability. Ollama telemetry is not independent proof of physical VRAM residency.
  - **Finite budgets**: default inference deadline 90 seconds, maximum 180; cancellation closes the HTTP socket, including after response headers. Evidence defaults to 3,500 characters; a conservative character budget rejects oversized input. No LLM CPU fallback. Embedding/similarity placement is a separate reported runtime property.
  - **Zero Cloud Tokens**: When RAG evidence is authoritative and sufficient, queries are answered 100% locally by Layer 1, spending zero prompt/generation tokens on external Frontier LLMs.
- **Cloud Handoff Boundary**:
  - Cloud escalation may receive only the user's original request, isolated from the LM Studio conversation history.
  - RAG text, answers grounded in RAG, citations, source metadata, scores, coverage, summaries, and derived facts are never included in a cloud payload.
  - The cloud adapter must not expose RAG MCP tools, project files, or database credentials. Cloud-capable clients, including Kimi Code running a cloud model, must not register the RAG MCP server directly. If isolation cannot be guaranteed, escalation is blocked.

## Component: Adaptive Model Router (`fast_router.py`)

The **Adaptive Model Router** (`AdaptiveModelRouter`) replaces naive string-matching with a multi-factor decision engine that evaluates 7 key signals:

1. **RAG Context Confidence (`rag_score`)**: Presence and relevance score of retrieved evidence against the configured floor (0.50 by default).
2. **Local Model Answer Completeness**: Self-verification check for `INSUFFICIENT DATA` or incomplete formatting.
3. **Task Complexity Analysis**: Evaluates structural indicators (architectural design vs factual lookup).
4. **Local Infrastructure Health & Acceleration**: Detection of Ollama availability (`http://127.0.0.1:11434`), Intel GPU runtime health, and memory pinning.
5. **Privacy Restrictions (`privacy_mode`)**: Hard local lock when privacy constraints prohibit cloud escalation.
6. **Tool Execution Errors**: Fallback to Frontier LLM if local tool or runtime execution encounters errors.
7. **Provenance & Quality Coverage**: Verifies evidence coverage before committing to local resolution.

Structured retrieved evidence is gated by result coverage: ordinary queries need at least one of the five requested chunks; complex queries need at least two and at least one `validated` or `authoritative` source. The local formatter must cite existing `[Evidence N]` identifiers. A missing or invalid citation sends a routing signal only; escalation responses omit all RAG-derived fields. Local answers may include `response_sources` for local use.

---

## Production Call Path

`MCP tool → RagClient → FastAPI → RagEngine → aiagent.public → retrieved evidence → local formatter → cited answer`

Every search, save, index, delete, move, feedback, maintenance, and GraphRAG request carries one validated `ProjectContext`. Clients send `project_id` and `root_id`; FastAPI resolves canonical names and paths from its own registry before calling the engine. RagEngine has no project-name or root fallback.

## Project and Path Boundary

`ProjectContext` contains server-resolved `project_id`, `project_name`, `root_id`, `project_root`, and registered roots plus independent `write_visibility`, `read_scope`, owner IDs, source/language filters, validation status, score threshold, and metadata policy.

One `root_id` selects exactly one active root. Other registered roots are alternatives and cannot be traversed through the active context. Paths use `resolve(strict=False)` (or strict resolution for existing inputs); `..`, symlink escapes, prefix confusion, sibling-root access, and absolute paths outside the active root are rejected. Client path hints may narrow but never expand server policy.

## Security Scope

Write visibility is `public`, `shared`, `agent-private`, or `session-private`. Read scope is independently `public`, `shared`, `agent`, or `session`; broader authenticated scopes include public/shared rows but private rows still require matching owner IDs. Validation status describes content quality and is never accepted as an access value.

Session-private access requires both matching `agent_id` and `session_id`. Agent-private access is the separately named broader scope. Graph entities, relations, cache keys, and SQL predicates include the project/security scope.

## Health

Every response carries one of three canonical status values: `success`, `error`, or `unavailable`. `RagClient` rejects any other value as a protocol violation.

- `GET /liveness` returns HTTP 200 with `{"status":"success"}` and checks only the HTTP process.
- `GET /readiness` returns HTTP 200 with `{"status":"success","ready":true}` only after engine, PostgreSQL, migration version, schema invariants, and a minimal query are ready.
- An unavailable dependency returns HTTP 503 with `{"status":"unavailable","ready":false}` and a safe non-secret reason. The recoverable engine state retries initialization after a bounded interval without claiming readiness.

## Installation Path

`installer → mutation inventory → backup manifest → secret migration → DB connectivity → migrations → MCP configs → systemd → health verification`

Filesystem phases register idempotent rollback actions. PostgreSQL migrations commit transactionally with their registry rows and form an explicit committed boundary: a later failure restores files/units but reports committed DB versions separately and never claims that PostgreSQL was rolled back. User systemd units are verified before replacement and both services must be enabled and active.

No credential value, DSN, HTTP header value, or external MCP entry is logged.

## Runtime failure isolation (October 2026 audit)

Работающий HTTP-процесс и готовая база проверяются отдельно: `/liveness`
проверяет только процесс, `/readiness` — инициализацию моделей, PostgreSQL,
миграции и минимальный запрос. При недоступности зависимости readiness возвращает
503; liveness может оставаться 200. Это не доказательство качества поиска.

Search workers have four admission slots and a finite response deadline. A timed-out
worker keeps its slot until it actually exits; queued work cannot grow indefinitely.
PostgreSQL connections have connect, statement and lock deadlines. HTTP, retrieval,
ranking and inference logs use UUID correlation IDs and durations without prompt text.
A missing cache generation bypasses both memory and persistent search caches.
Freshness warnings are returned from source metadata; citation validity does not
independently establish claim entailment or absence of contradictions.

See `artifacts/rag-audit-2026-10-09/COVERAGE.md` for measured coverage and outstanding
host-level VRAM, stale-client and privileged PostgreSQL-extension boundaries.

## Evidence retrieval and freshness (2026-10-29)

E5 embeddings and hybrid SQL retrieval select candidates. A local
Qwen3-Reranker-0.6B scores evidence against the original question on Intel XPU.
It computes yes/no logits, without generating an answer or calling a cloud model.
`retrieval_policy.json` pins the model revision, candidate/token/batch limits,
minimum relevance and deadline. The score estimates relevance, not factual truth.
An additional property check rejects matches that only share a code identifier.
An empty evidence set returns `llm_needed` and `insufficient_evidence`; a failed
ranker returns unavailable, without CPU fallback. Exact queries alone reuse cached
decisions; project, read scope, identity where applicable, generation and policy
partition the caches. Candidate scoring is not exposed as final relevance.

Indexed files record size, nanosecond mtime and a content hash. On every retrieval,
including cache hits, size/mtime are compared with the source: missing or changed
snapshots are omitted. This stat check does not detect a deliberate rewrite that
preserves both size and mtime. Older notes without snapshots are labelled
`untracked`; model output is labelled `agent_note`, without promoting validation.
Test fixtures are labelled separately. Filesystem currency and factual verification
are distinct fields; `current` never means the claims were independently verified.

Test fixtures participate only when the question explicitly concerns tests
(`test…` / `тест…`). Evaluation, probe and artifact paths are also excluded at
retrieval time, including old index rows. A benchmark question is not evidence
answering itself. Identifier-property checks include single-word properties.

The watchdog has one worker, at most 500 pending paths and three attempts per event.
It reconciles metadata at startup and every five minutes, up to 10,000 files;
ignored directories and symlinks are excluded. A full queue is recovered on the
next scan. Its private `.rag-index-state.json` acknowledges only successful,
unchanged source versions. Reconciliation resumes after restart. Model weights,
artifacts and evaluation fixtures are never indexed by this watcher.

## Intel Arc execution contract (2026-10-29)

The retrieval engine uses only PyTorch XPU: SentenceTransformer embeddings and
`XpuReranker` cosine similarity run on Intel Arc. CPU/NVIDIA/ONNX model execution
paths have been removed. `MODEL_PATH` must identify a local SentenceTransformer
directory. Missing XPU, misplaced model tensors or failed GPU execution fails
closed; `RAG_STRICT_GPU=0` no longer enables an alternative execution path.
Install the pinned `deploy/intel-xpu-requirements.txt` before application requirements.

`GET /runtime` verifies model parameter/buffer placement and reports the Intel
device, warm-up status and disabled CPU fallback. It is subject to the same
HTTP authentication as other routes. Tokenization, HTTP, JSON, SQL/pgvector,
metadata scoring and copies still require CPU; the service cannot promise zero
CPU usage. Torch host computation uses one thread.

`/search` omits candidates below the calibrated relevance floor before applying
context budgets, including responses from old caches. No accepted evidence means
`llm_needed`, empty results and zero evidence coverage. This does not prove that
an accepted passage entails an answer; source validation remains separate.
