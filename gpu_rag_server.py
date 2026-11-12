#!/usr/bin/env python3
from __future__ import annotations
"""
RAG Memory Server with Intel Arc XPU embeddings and cosine similarity.

Fully hardened:
- FastAPI lifespan management (no module-level DB connection)
- Path traversal protection & allowed index roots check
- Header/Body context priority resolution
- End-to-end propagation of search parameters
- Strict CORS & sanitized error responses
"""

import hashlib
import hmac
import json
import logging
import os
import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from local_inference import correlation_id
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import settings
from project_context import (
    GlobalKnowledgeContext,
    KnowledgeContext,
    ProjectContext,
    get_project_registry,
    ReadScope,
    ValidationStatus,
    WriteVisibility,
)
from response_format import compact_hit, render_context_block

if TYPE_CHECKING:
    from rag_engine import RagEngine

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
log = logging.getLogger("gpu_rag_server")

# The `route` this server publishes is the caller's cue to trust a hit or escalate,
# so it has to sit on the engine's calibrated scale rather than on a local guess.
# Defaults to `rag_engine.RELEVANCE_FLOOR`; see `docs/score-calibration.md`.
RAG_CONFIDENCE_THRESHOLD = float(
    os.getenv("RAG_CONFIDENCE_THRESHOLD", str(settings.relevance_floor))
)
SERVER_HOST = settings.bind_address
SERVER_PORT = settings.server_port
SERVER_PROJECT_REGISTRY = get_project_registry(settings)
BACKEND_SUCCESS = "success"
BACKEND_ERROR = "error"
BACKEND_UNAVAILABLE = "unavailable"

def _load_model_registry() -> list[tuple[str, dict]]:
    env_registry = os.getenv("MODEL_REGISTRY_JSON")
    if env_registry:
        try:
            parsed = json.loads(env_registry)
            if isinstance(parsed, list):
                return [(item[0], item[1]) for item in parsed if isinstance(item, list) and len(item) == 2]
        except Exception as exc:
            log.warning("Failed to parse MODEL_REGISTRY_JSON: %s", exc)
    return [
        ("gpt-4o",                     {"tier": "max",      "rag_budget": 32_000}),
        ("gpt-4o-mini",                {"tier": "mid",      "rag_budget": 4_000}),
        ("gpt-3.5",                    {"tier": "mid",      "rag_budget": 3_000}),
        ("gemini-1.5-flash",           {"tier": "mid",      "rag_budget": 8_000}),
        ("qwen2.5-coder:7b",           {"tier": "local",    "rag_budget": 2_000}),
        ("qwen2.5-coder",              {"tier": "local",    "rag_budget": 1_200}),
        ("llama3",                     {"tier": "local",    "rag_budget": 1_500}),
        ("deepseek-coder",             {"tier": "local",    "rag_budget": 2_000}),
        ("antigravity",                {"tier": "max",      "rag_budget": 48_000}),
        ("cursor",                     {"tier": "frontier", "rag_budget": 16_000}),
        ("mcp-client",                 {"tier": "frontier", "rag_budget": 24_000}),
        ("opencode",                   {"tier": "frontier", "rag_budget": 16_000}),
        ("copilot",                    {"tier": "mid",      "rag_budget": 8_000}),
        ("ollama",                     {"tier": "local",    "rag_budget": 1_200}),
    ]


MODEL_REGISTRY: list[tuple[str, dict]] = _load_model_registry()

MODEL_DEFAULT = {"tier": "mid", "rag_budget": 6_000}


def content_delta_hash(content: str) -> str:
    """Stable delta-tracking digest. Not a security primitive, but FIPS-safe."""
    return hashlib.sha256(content.encode("utf-8"), usedforsecurity=False).hexdigest()

TASK_SIGNALS: list[tuple[str, float, list[str]]] = [
    ("analysis",     1.5, ["изучи", "проанализируй", "найди паттерн", "история", "compare", "overview"]),
    ("architecture", 1.0, ["спроектируй", "архитектура", "переработай", "design", "refactor", "улучши"]),
    ("debug",        0.7, ["ошибка", "traceback", "баг", "не работает", "почему", "error", "fix", "debug"]),
    ("code",         0.6, ["напиши", "реализуй", "добавь", "write", "implement", "create", "generate"]),
    ("fact",         0.3, ["что такое", "покажи", "список", "версия", "статус", "what is", "show", "list"]),
]


def get_model_cfg(agent_id: str = "", model_name: str = "") -> dict:
    combined = (agent_id + " " + model_name).lower()
    for key, cfg in MODEL_REGISTRY:
        if key.lower() in combined:
            return cfg
    return MODEL_DEFAULT


def detect_task_multiplier(query: str) -> float:
    q = query.lower()
    for _, mult, keywords in TASK_SIGNALS:
        if any(kw in q for kw in keywords):
            return mult
    return 0.7


def compute_budget(model_cfg: dict, query: str, ctx_tokens_used: int = 0) -> int:
    base = model_cfg["rag_budget"]
    task_mult = detect_task_multiplier(query)
    max_ctx = 32_000 if model_cfg["tier"] in ("max", "frontier") else 8_000
    fill_ratio = min(1.0, ctx_tokens_used / max_ctx) if ctx_tokens_used > 0 else 0.0
    headroom = max(0.10, 1.0 - fill_ratio)
    return max(500, int(base * task_mult * headroom))


_engine_init_lock = threading.Lock()


def _default_engine_factory():
    from rag_engine import RagEngine

    return RagEngine()


def _safe_initialization_reason(_exc: Exception) -> str:
    return "RAG engine initialization is incomplete"


def _try_initialize_engine(app: FastAPI) -> Any | None:
    current = getattr(app.state, "rag_engine", None)
    if current is not None:
        return current
    now = time.monotonic()
    if now < getattr(app.state, "next_engine_retry", 0.0):
        return None
    with _engine_init_lock:
        current = getattr(app.state, "rag_engine", None)
        if current is not None:
            return current
        try:
            factory = getattr(app.state, "engine_factory", _default_engine_factory)
            current = factory()
            app.state.rag_engine = current
            app.state.engine_error_reason = ""
            app.state.next_engine_retry = 0.0
            return current
        except Exception as exc:
            log.error("RAG engine initialization failed", exc_info=True)
            app.state.rag_engine = None
            app.state.engine_error_reason = _safe_initialization_reason(exc)
            app.state.next_engine_retry = now + 5.0
            return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.require_safe_bind(SERVER_HOST)
    app.state.rag_engine = None
    app.state.engine_error_reason = "RAG engine initialization has not completed"
    app.state.next_engine_retry = 0.0
    await asyncio.to_thread(_try_initialize_engine, app)
    # Recoverable mode: the HTTP process remains live while readiness is 503.
    # Subsequent readiness calls retry initialization after a bounded interval.
    try:
        yield
    finally:
        engine = getattr(app.state, "rag_engine", None)
        if engine is not None:
            try:
                engine.close()
            except Exception:
                log.warning("Error closing RagEngine during shutdown", exc_info=True)


_search_slots = threading.BoundedSemaphore(4)
_search_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rag-search")
SEARCH_EXECUTION_TIMEOUT = max(0.01, settings.rag_search_timeout_seconds - 5.0)


async def run_bounded_search(function):
    """Bound work, including cancelled workers that cannot be killed safely."""
    if not _search_slots.acquire(blocking=False):
        raise HTTPException(503, detail={"error": {
            "code": "RAG_SEARCH_BUSY", "message": "Search workers are occupied",
            "request_id": correlation_id(),
        }})
    slots = _search_slots

    def work():
        try:
            return function()
        finally:
            slots.release()

    future = _search_executor.submit(contextvars.copy_context().run, work)
    # Shield keeps ownership with the real worker after HTTP/MCP cancellation.
    wrapped = asyncio.wrap_future(future)
    wrapped.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
    try:
        return await asyncio.wait_for(asyncio.shield(wrapped), SEARCH_EXECUTION_TIMEOUT)
    except TimeoutError as exc:
        raise HTTPException(503, detail={"error": {
            "code": "RAG_SEARCH_TIMEOUT", "message": "Search execution deadline exceeded",
            "request_id": correlation_id(),
        }}) from exc


app = FastAPI(
    title="GPU RAG Memory Server",
    description="Centralized RAG memory server for multi-agent access.",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID", "X-Local-RAG-Session"],
)

@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    try:
        request_id = correlation_id(request.headers.get("x-request-id") or str(uuid.uuid4()))
    except ValueError:
        request_id = correlation_id(str(uuid.uuid4()))
    request.state.request_id = request_id
    configured_token = settings.bearer_token.get_secret_value()
    if configured_token:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {configured_token}"
        if not hmac.compare_digest(supplied, expected):
            return Response(
                content=json.dumps({"detail": {"error": {"code": "UNAUTHORIZED", "message": "Bearer authentication required"}}}),
                status_code=status.HTTP_401_UNAUTHORIZED,
                media_type="application/json",
            )

    request.state.agent_id = settings.server_agent_id
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        session_id = request.headers.get("x-local-rag-session", "")
        if not session_id:
            return Response(
                content=json.dumps({"detail": {"error": {"code": "MISSING_SESSION", "message": "X-Local-RAG-Session header is required for stateful requests"}}}),
                status_code=status.HTTP_400_BAD_REQUEST,
                media_type="application/json",
            )
        import re
        if not re.match(r"^[A-Za-z0-9._:-]{16,128}$", session_id):
            return Response(
                content=json.dumps({"detail": {"error": {"code": "MALFORMED_SESSION", "message": "X-Local-RAG-Session header is malformed"}}}),
                status_code=status.HTTP_400_BAD_REQUEST,
                media_type="application/json",
            )
        request.state.session_id = session_id
    else:
        request.state.session_id = request.headers.get("x-local-rag-session", "anonymous-session")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.max_request_body_bytes:
                req_id = str(uuid.uuid4())
                return Response(
                    content=json.dumps({
                        "detail": {
                            "error": {
                                "code": "FILE_TOO_LARGE",
                                "message": "Request body size exceeds configured maximum",
                                "request_id": req_id
                            }
                        }
                    }),
                    status_code=413,
                    media_type="application/json"
                )
        except ValueError:
            pass
    if request.method in {"POST", "PUT", "PATCH"}:
        body = await request.body()
        if len(body) > settings.max_request_body_bytes:
            return Response(
                content=json.dumps({"detail": {"error": {"code": "FILE_TOO_LARGE", "message": "Request body size exceeds configured maximum"}}}),
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                media_type="application/json",
            )
    started = time.monotonic()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    log.info("http request_id=%s path=%s status=%d elapsed_ms=%d",
             request_id, request.url.path, response.status_code,
             (time.monotonic()-started)*1000)
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        body = {"detail": detail}
    elif isinstance(detail, str):
        req_id = str(uuid.uuid4())
        body = {"detail": {"error": {"code": "HTTP_ERROR", "message": detail, "request_id": req_id}}}
    else:
        body = {"detail": detail}
    return Response(
        content=json.dumps(body),
        status_code=exc.status_code,
        media_type="application/json"
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    req_id = str(uuid.uuid4())
    log.error("Unhandled server exception [%s]: %s", req_id, exc, exc_info=True)
    return Response(
        content=json.dumps({
            "detail": {
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "An internal server error occurred",
                    "request_id": req_id
                }
            }
        }),
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        media_type="application/json"
    )


def get_rag_engine(request: Request) -> RagEngine:
    engine = getattr(request.app.state, "rag_engine", None)
    if engine is None or getattr(engine, "_pool", None) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "ENGINE_UNAVAILABLE", "message": "RAG Engine is not initialized or unavailable"}}
        )
    return engine


class ProjectContextRequest(BaseModel):
    project_id: Optional[str] = None
    root_id: Optional[str] = None
    working_directory: Optional[str] = None
    write_visibility: WriteVisibility = WriteVisibility.SESSION_PRIVATE
    read_scope: ReadScope = ReadScope.SESSION
    agent_id: str = ""
    session_id: str = ""
    source_types: List[str] = Field(default_factory=list)
    language: str = "en"
    validation_status: ValidationStatus = ValidationStatus.UNVERIFIED
    min_score: float = 0.0
    include_metadata: bool = True
    project_root: Optional[str] = None
    allowed_roots: Optional[List[str]] = None
    registry_fingerprint: Optional[str] = Field(default=None, min_length=8, max_length=128)
    context_fingerprint: Optional[str] = Field(default=None, min_length=8, max_length=128)

    def to_context(self, *, agent_id: str, session_id: str) -> ProjectContext:
        if self.working_directory and (self.project_id or self.root_id):
            raise HTTPException(status_code=422, detail="working_directory cannot be combined with project/root")
        if not self.working_directory and (not self.project_id or not self.root_id):
            raise HTTPException(status_code=422, detail="Explicit project/root or client working_directory is required")
        if not self.working_directory and not self.context_fingerprint and not self.registry_fingerprint:
            raise HTTPException(status_code=422, detail="Context fingerprint is required")
        try:
            return get_project_registry(settings).resolve(
                project_id=self.project_id,
                root_id=self.root_id,
                working_directory=self.working_directory,
                write_visibility=self.write_visibility.value,
                read_scope=self.read_scope.value,
                agent_id=agent_id,
                session_id=session_id,
                source_types=self.source_types,
                language=self.language,
                validation_status=self.validation_status.value,
                min_score=self.min_score,
                include_metadata=self.include_metadata,
                project_root_hint=self.project_root,
                allowed_roots_hint=self.allowed_roots,
                registry_fingerprint_hint=self.registry_fingerprint,
                context_fingerprint_hint=self.context_fingerprint,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=403,
                detail={"error": {"code": "CONTEXT_NOT_AUTHORIZED", "message": str(exc)}},
            ) from exc


class KnowledgeContextRequest(BaseModel):
    project_id: Optional[str] = None
    root_id: Optional[str] = None
    working_directory: Optional[str] = None
    global_search: bool = False
    read_scope: ReadScope = ReadScope.SESSION
    agent_id: str = ""
    session_id: str = ""
    source_types: List[str] = Field(default_factory=list)
    language: str = "en"
    min_score: float = 0.0
    include_metadata: bool = True
    registry_fingerprint: Optional[str] = Field(default=None, min_length=8, max_length=128)
    context_fingerprint: Optional[str] = Field(default=None, min_length=8, max_length=128)

    def to_context(self, *, agent_id: str, session_id: str) -> KnowledgeContext | ProjectContext | GlobalKnowledgeContext:
        if self.global_search:
            if self.project_id or self.root_id or self.working_directory:
                raise HTTPException(status_code=422, detail="global_search cannot be combined with project/root/cwd")
            return GlobalKnowledgeContext(
                read_scope=self.read_scope,
                agent_id=agent_id,
                session_id=session_id,
                source_types=tuple(self.source_types),
                language=self.language,
                min_score=self.min_score,
                include_metadata=self.include_metadata,
            )
        if self.working_directory and (self.project_id or self.root_id):
            raise HTTPException(status_code=422, detail="working_directory cannot be combined with project/root")
        if not self.working_directory and not self.project_id:
            raise HTTPException(status_code=422, detail="Explicit project, client working_directory or global_search is required")
        if self.root_id and not self.project_id:
            raise HTTPException(status_code=422, detail="root_id requires project_id")
        if not self.working_directory and not self.context_fingerprint and not self.registry_fingerprint:
            raise HTTPException(status_code=422, detail="Context fingerprint is required")
        try:
            registry = get_project_registry(settings)
            if self.root_id:
                return registry.resolve(
                    project_id=self.project_id,
                    root_id=self.root_id,
                    write_visibility=WriteVisibility.SESSION_PRIVATE.value,
                    read_scope=self.read_scope.value,
                    agent_id=agent_id,
                    session_id=session_id,
                    source_types=self.source_types,
                    language=self.language,
                    min_score=self.min_score,
                    include_metadata=self.include_metadata,
                    registry_fingerprint_hint=self.registry_fingerprint,
                    context_fingerprint_hint=self.context_fingerprint,
                )
            return registry.resolve_knowledge(
                project_id=self.project_id,
                working_directory=self.working_directory,
                read_scope=self.read_scope.value,
                agent_id=agent_id,
                session_id=session_id,
                source_types=self.source_types,
                language=self.language,
                min_score=self.min_score,
                include_metadata=self.include_metadata,
                registry_fingerprint_hint=self.registry_fingerprint,
                context_fingerprint_hint=self.context_fingerprint,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=403,
                detail={"error": {"code": "CONTEXT_NOT_AUTHORIZED", "message": str(exc)}},
            ) from exc


class SearchRequest(BaseModel):
    context: KnowledgeContextRequest
    query: str = Field(min_length=1, max_length=settings.max_query_chars)
    limit: int = Field(default=5, ge=1, le=settings.max_search_limit)
    model_name: str = ""
    ctx_tokens_used: int = 0
    injected_hashes: List[str] = Field(default_factory=list)


class LocalAnswerCacheLookupRequest(BaseModel):
    context: KnowledgeContextRequest
    prompt_hash: str = Field(min_length=64, max_length=64)


class LocalAnswerCacheStoreRequest(LocalAnswerCacheLookupRequest):
    answer: str = Field(min_length=1, max_length=12000)
    expected_generation: int = Field(ge=0)


class AgenticSearchRequest(BaseModel):
    context: KnowledgeContextRequest
    query: str = Field(min_length=1, max_length=settings.max_query_chars)
    limit: int = Field(default=5, ge=1, le=settings.max_agentic_limit)
    max_depth: int = Field(default=2, ge=1, le=settings.max_agentic_depth)
    max_entities: int = Field(default=20, ge=1, le=settings.max_agentic_entities)
    max_relations: int = Field(default=36, ge=1, le=settings.max_agentic_relations)
    timeout_ms: int = Field(default=5000, ge=1, le=settings.max_agentic_timeout_ms)
    model_name: str = ""
    ctx_tokens_used: int = 0
    injected_hashes: List[str] = Field(default_factory=list)


class SaveRequest(BaseModel):
    context: ProjectContextRequest
    content: str = Field(min_length=10, max_length=settings.max_save_chars)
    metadata: dict = Field(default_factory=dict)
    source_uri: str = "agent_generated"
    source_type: str = "llm_output"
    file_path: str = ""
    document_id: Optional[int] = None
    start_locator: str = ""
    end_locator: str = ""


class FeedbackRequest(BaseModel):
    context: ProjectContextRequest
    query: str
    feedback_type: str
    correction: str = ""


class IndexRequest(BaseModel):
    context: ProjectContextRequest
    path: str


class DeleteRequest(BaseModel):
    context: ProjectContextRequest
    path: str


class MoveRequest(BaseModel):
    context: ProjectContextRequest
    source_path: str
    destination_path: str


class DeprecateRequest(BaseModel):
    context: ProjectContextRequest
    chunk_id: Optional[int] = None
    content_hash: str = ""
    status: str = "deprecated"
    reason: str = Field(default="", max_length=2000)
    superseded_by: Optional[int] = None


class VerifyRequest(BaseModel):
    context: ProjectContextRequest
    chunk_id: Optional[int] = None
    content_hash: str = ""
    recheck_after_days: Optional[int] = Field(default=None, gt=0, le=3650)
    note: str = Field(default="", max_length=2000)


class StaleReportRequest(BaseModel):
    context: ProjectContextRequest
    limit: int = Field(default=25, ge=1, le=200)
    min_importance: int = Field(default=1, ge=1, le=10)
    include_undeclared_after_days: Optional[int] = Field(default=None, gt=0, le=3650)


class ConsolidateRequest(BaseModel):
    context: ProjectContextRequest
    decay_days: float = 30.0
    min_importance: int = 1


class MemoryOptimizeRequest(BaseModel):
    context: ProjectContextRequest
    sim_threshold: float = 0.98
    apply: bool = False


@app.get("/health")
@app.get("/healthz")
@app.get("/liveness")
def health_live():
    """Process-only liveness contract; never touches PostgreSQL or the model."""
    return {"status": BACKEND_SUCCESS}


@app.get("/runtime")
def runtime_status(response: Response, engine: RagEngine = Depends(get_rag_engine)):
    """Expose model placement; liveness alone is not GPU readiness evidence."""
    try:
        return {"status": BACKEND_SUCCESS, **engine.runtime_status()}
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": BACKEND_UNAVAILABLE, "reason": "Intel XPU runtime is unavailable"}


@app.get("/readiness")
@app.get("/health/ready")
def health_ready(request: Request, response: Response):
    """Dependency readiness contract with a safe, non-secret failure reason."""
    engine = _try_initialize_engine(request.app)
    if engine is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": BACKEND_UNAVAILABLE,
            "ready": False,
            "reason": getattr(
                request.app.state,
                "engine_error_reason",
                "RAG engine initialization is incomplete",
            ),
        }
    try:
        if not engine.check_readiness():
            raise RuntimeError("schema or database readiness check failed")
    except Exception:
        log.error("RAG readiness check failed", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": BACKEND_UNAVAILABLE,
            "ready": False,
            "reason": "PostgreSQL schema is not ready",
        }
    return {"status": BACKEND_SUCCESS, "ready": True}


def apply_delta_and_budget(
    results: List[dict],
    injected_hashes,
    budget: int,
    include_metadata: bool,
) -> tuple[List[dict], int]:
    """Drop what the caller already has, then stop at the token budget.

    Both retrieval endpoints computed this identically and a third was about to.
    It is the one place where the server decides how many tokens a caller is
    made to spend, so having two copies of it meant two places where that
    decision could quietly drift apart.
    """
    injected = set(injected_hashes or ())
    delta: List[dict] = []
    tokens_used = 0
    for hit in results:
        chunk_hash = hit.get("content_hash") or content_delta_hash(hit["content"])
        if chunk_hash in injected:
            continue
        chunk_tokens = len(hit["content"]) // 4
        if tokens_used + chunk_tokens > budget:
            break
        hit["content_hash"] = chunk_hash
        if not include_metadata:
            hit.pop("metadata", None)
        delta.append(hit)
        tokens_used += chunk_tokens
    return delta, tokens_used


def _with_project_origin(raw: dict, context: KnowledgeContext) -> dict:
    results = []
    for hit in raw.get("results", []):
        results.append({
            **hit,
            "project_id": context.project_id,
            "project_name": context.project_name,
        })
    return {**raw, "results": results}


async def _global_search(
    req: SearchRequest,
    context: GlobalKnowledgeContext,
    engine: RagEngine,
) -> dict:
    registry = get_project_registry(settings)
    deadline = time.monotonic() + SEARCH_EXECUTION_TIMEOUT
    completed: list[str] = []
    omitted: list[str] = []
    results: list[dict] = []
    caches: dict[str, dict] = {}
    candidates = 0
    projects = list(registry.projects)

    for start in range(0, len(projects), 2):
        batch_projects = projects[start:start + 2]
        if time.monotonic() >= deadline:
            omitted.extend(project.project_id for project in batch_projects)
            continue
        contexts = [
            registry.resolve_knowledge(
                project_id=project.project_id,
                read_scope=context.read_scope.value,
                agent_id=context.agent_id,
                session_id=context.session_id,
                source_types=context.source_types,
                language=context.language,
                min_score=context.min_score,
                include_metadata=context.include_metadata,
            )
            for project in batch_projects
        ]

        async def run_project(current):
            raw = await run_bounded_search(lambda: engine.search(
                context=current,
                query=req.query,
                limit=min(req.limit * 3, 30),
            ))
            return current, raw

        try:
            remaining = max(0.001, deadline - time.monotonic())
            finished = await asyncio.wait_for(
                asyncio.gather(*(run_project(current) for current in contexts), return_exceptions=True),
                timeout=remaining,
            )
        except TimeoutError:
            omitted.extend(project.project_id for project in batch_projects)
            continue
        for current, outcome in zip(contexts, finished, strict=True):
            if isinstance(outcome, Exception):
                omitted.append(current.project_id)
                continue
            _, raw = outcome
            if raw.get("status") != BACKEND_SUCCESS:
                omitted.append(current.project_id)
                continue
            tagged = _with_project_origin(raw, current)
            completed.append(current.project_id)
            caches[current.project_id] = tagged.get("cache", {})
            candidates += int((tagged.get("composite_metrics") or {}).get("candidates_evaluated", 0))
            results.extend(tagged.get("results", []))

    results.sort(key=lambda hit: hit.get("final_score", hit.get("score", 0.0)), reverse=True)
    partial = bool(omitted)
    return {
        "status": BACKEND_UNAVAILABLE if partial else BACKEND_SUCCESS,
        "code": "GLOBAL_SEARCH_INCOMPLETE" if partial else None,
        "partial": partial,
        "completed_project_ids": completed,
        "omitted_project_ids": omitted,
        "results": results,
        "cache": {"projects": caches},
        "composite_metrics": {"candidates_evaluated": candidates},
    }


@app.post("/search")
@app.post("/v1/search")
async def search(
    req: SearchRequest,
    request: Request,
    engine: RagEngine = Depends(get_rag_engine)
):
    context = req.context.to_context(
        agent_id=request.state.agent_id, session_id=request.state.session_id
    )

    model_cfg = get_model_cfg(context.agent_id, req.model_name)
    budget = compute_budget(model_cfg, req.query, req.ctx_tokens_used)

    is_global = isinstance(context, GlobalKnowledgeContext)
    try:
        if is_global:
            raw = await _global_search(req, context, engine)
        else:
            raw = await run_bounded_search(
                lambda: engine.search(
                    context=context,
                    query=req.query,
                    limit=min(req.limit * 3, 30),
                )
            )
            raw = _with_project_origin(raw, context)
            if raw.get("status", BACKEND_SUCCESS) not in (BACKEND_SUCCESS, "ok"):
                raise RuntimeError("search backend returned an error status")
    except HTTPException:
        raise
    except Exception as e:
        req_id = request.state.request_id
        log.error("Search error request_id=%s type=%s", req_id, type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "SEARCH_FAILED", "message": "Search query processing failed", "request_id": req_id}}
        )

    results = [
        hit for hit in raw.get("results", [])
        if hit.get("final_score", hit.get("score", 0.0)) >= max(
            context.min_score, RAG_CONFIDENCE_THRESHOLD
        )
    ]
    partial = bool(raw.get("partial"))
    global_fields = {
        key: raw[key]
        for key in ("partial", "completed_project_ids", "omitted_project_ids")
        if key in raw
    }
    response_status = raw.get("status", BACKEND_SUCCESS) if is_global else BACKEND_SUCCESS

    if not results:
        return {
            "status": response_status,
            "route": "llm_needed",
            "reason": raw.get("reason", "insufficient_evidence"),
            "score_kind": raw.get("score_kind", "relevance_not_factual_certainty"),
            "unusable_sources_omitted": raw.get("unusable_sources_omitted", 0),
            "top_score": 0.0,
            "evidence_coverage": 0.0,
            "composite_metrics": raw.get("composite_metrics", {}),
            "results": [],
            "budget_tokens": budget,
            "tokens_used": 0,
            "agent_tier": model_cfg["tier"],
            "cache": raw.get("cache", {}),
            **global_fields,
        }

    top_score = results[0].get("final_score", results[0].get("score", 0.0))
    route = "rag_answer" if top_score >= RAG_CONFIDENCE_THRESHOLD and not partial else "llm_needed"

    delta_results, tokens_used = apply_delta_and_budget(
        results, req.injected_hashes, budget, context.include_metadata
    )

    return {
        "status": response_status,
        "route": route,
        "reason": raw.get("reason", "evidence_found"),
        "score_kind": raw.get("score_kind", "relevance_not_factual_certainty"),
        "unusable_sources_omitted": raw.get("unusable_sources_omitted", 0),
        "top_score": round(top_score, 4),
        "evidence_coverage": round(min(1.0, len(results) / min(req.limit * 3, 30)), 2),
        "composite_metrics": raw.get("composite_metrics", {}),
        "results": delta_results[:req.limit],
        "budget_tokens": budget,
        "tokens_used": tokens_used,
        "agent_tier": model_cfg["tier"],
        "cache": raw.get("cache", {}),
        **global_fields,
    }


@app.post("/answer-cache/lookup")
def local_answer_cache_lookup(
    req: LocalAnswerCacheLookupRequest,
    request: Request,
    engine: RagEngine = Depends(get_rag_engine),
):
    context = req.context.to_context(
        agent_id=request.state.agent_id, session_id=request.state.session_id
    )
    if isinstance(context, GlobalKnowledgeContext):
        raise HTTPException(status_code=422, detail="Local answer cache requires one project context")
    try:
        return engine.lookup_local_answer(context, req.prompt_hash)
    except Exception:
        log.warning("Local answer cache lookup failed open", exc_info=True)
        return {"status": BACKEND_SUCCESS, "hit": False, "generation": None}


@app.post("/answer-cache/store")
def local_answer_cache_store(
    req: LocalAnswerCacheStoreRequest,
    request: Request,
    engine: RagEngine = Depends(get_rag_engine),
):
    context = req.context.to_context(
        agent_id=request.state.agent_id, session_id=request.state.session_id
    )
    if isinstance(context, GlobalKnowledgeContext):
        raise HTTPException(status_code=422, detail="Local answer cache requires one project context")
    try:
        stored = engine.store_local_answer(
            context,
            req.prompt_hash,
            req.answer,
            req.expected_generation,
        )
        return {"status": BACKEND_SUCCESS, "stored": stored}
    except Exception:
        log.warning("Local answer cache store failed open", exc_info=True)
        return {"status": BACKEND_SUCCESS, "stored": False}


async def _global_agentic_search(
    req: AgenticSearchRequest,
    context: GlobalKnowledgeContext,
    engine: RagEngine,
) -> dict:
    registry = get_project_registry(settings)
    deadline = time.monotonic() + min(SEARCH_EXECUTION_TIMEOUT, req.timeout_ms / 1000)
    completed: list[str] = []
    omitted: list[str] = []
    results: list[dict] = []
    graph: list[dict] = []
    projects = list(registry.projects)
    traversed = 0
    partial = False

    for start in range(0, len(projects), 2):
        batch_projects = projects[start:start + 2]
        if time.monotonic() >= deadline:
            omitted.extend(project.project_id for project in batch_projects)
            continue
        contexts = [
            registry.resolve_knowledge(
                project_id=project.project_id,
                read_scope=context.read_scope.value,
                agent_id=context.agent_id,
                session_id=context.session_id,
                source_types=context.source_types,
                language=context.language,
                min_score=context.min_score,
                include_metadata=context.include_metadata,
            )
            for project in batch_projects
        ]

        async def run_project(current):
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            raw = await run_bounded_search(lambda: engine.agentic_search(
                context=current,
                query=req.query,
                limit=req.limit,
                max_depth=req.max_depth,
                max_entities=req.max_entities,
                max_relations=req.max_relations,
                timeout_ms=min(req.timeout_ms, remaining_ms),
            ))
            return current, raw

        try:
            remaining = max(0.001, deadline - time.monotonic())
            finished = await asyncio.wait_for(
                asyncio.gather(*(run_project(current) for current in contexts), return_exceptions=True),
                timeout=remaining,
            )
        except TimeoutError:
            omitted.extend(project.project_id for project in batch_projects)
            continue
        for current, outcome in zip(contexts, finished, strict=True):
            if isinstance(outcome, Exception):
                omitted.append(current.project_id)
                continue
            _, raw = outcome
            if raw.get("status") != BACKEND_SUCCESS:
                omitted.append(current.project_id)
                continue
            completed.append(current.project_id)
            partial = partial or bool(raw.get("partial"))
            traversed += int(raw.get("traversed_relations", 0))
            for hit in raw.get("results", []):
                results.append({
                    **hit,
                    "project_id": current.project_id,
                    "project_name": current.project_name,
                })
            for node in raw.get("graph_context", []):
                graph.append({
                    **node,
                    "project_id": current.project_id,
                    "project_name": current.project_name,
                })

    partial = partial or bool(omitted)
    results.sort(key=lambda hit: hit.get("final_score", hit.get("score", 0.0)), reverse=True)
    return {
        "status": BACKEND_UNAVAILABLE if partial else BACKEND_SUCCESS,
        "code": "GLOBAL_SEARCH_INCOMPLETE" if partial else None,
        "partial": partial,
        "completed_project_ids": completed,
        "omitted_project_ids": omitted,
        "results": results[:req.limit],
        "graph_context": graph[:req.max_entities],
        "security_scope": {
            "project_ids": completed,
            "read_scope": context.read_scope.value,
        },
        "applied_limits": {
            "max_depth": req.max_depth,
            "max_entities": req.max_entities,
            "max_relations": req.max_relations,
            "limit": req.limit,
            "timeout_ms": req.timeout_ms,
        },
        "traversed_relations": min(traversed, req.max_relations),
    }


@app.post("/agentic-search")
@app.post("/agentic_search")
@app.post("/v1/search/agentic")
async def agentic_search(
    req: AgenticSearchRequest,
    request: Request,
    engine: RagEngine = Depends(get_rag_engine)
):
    context = req.context.to_context(
        agent_id=request.state.agent_id, session_id=request.state.session_id
    )

    model_cfg = get_model_cfg(context.agent_id, req.model_name)
    budget = compute_budget(model_cfg, req.query, req.ctx_tokens_used)

    is_global = isinstance(context, GlobalKnowledgeContext)
    try:
        if is_global:
            raw = await _global_agentic_search(req, context, engine)
        else:
            raw = await run_bounded_search(
                lambda: engine.agentic_search(
                    context=context,
                    query=req.query,
                    limit=req.limit,
                    max_depth=req.max_depth,
                    max_entities=req.max_entities,
                    max_relations=req.max_relations,
                    timeout_ms=req.timeout_ms,
                )
            )
            raw = _with_project_origin(raw, context)
    except HTTPException:
        raise
    except Exception as e:
        req_id = request.state.request_id
        log.error("Agentic search error request_id=%s type=%s", req_id, type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "AGENTIC_SEARCH_FAILED", "message": "Agentic graph search failed", "request_id": req_id}}
        )

    if not is_global and raw.get("status", BACKEND_SUCCESS) not in (BACKEND_SUCCESS, "ok"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "AGENTIC_BACKEND_ERROR", "message": "Agentic backend is unavailable"}},
        )

    delta_hits, tokens_used = apply_delta_and_budget(
        raw.get("results", []),
        req.injected_hashes,
        int(budget * 0.6),
        context.include_metadata,
    )

    raw["results"] = delta_hits[:req.limit]
    if not is_global:
        raw["status"] = BACKEND_SUCCESS
    raw["budget_tokens"] = budget
    raw["tokens_used"] = tokens_used
    raw["agent_tier"] = model_cfg["tier"]
    return raw


class HookContextRequest(BaseModel):
    """One retrieval, rendered for injection, for a caller that has no context.

    Editor hooks run on every prompt, so they must start fast and must not carry
    a copy of this repository's authorization model around with them. Here the
    server builds the ProjectContext itself from its own registry - which it is
    already the authority on - and hands back finished text. The hook stays a
    stdlib HTTP call with no dependency on the engine at all.
    """

    query: str = Field(min_length=1, max_length=settings.max_query_chars)
    limit: int = Field(default=4, ge=1, le=settings.max_search_limit)
    max_chars: int = Field(default=1400, ge=200, le=16000)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    read_scope: ReadScope = ReadScope.SHARED
    project_id: Optional[str] = None
    root_id: Optional[str] = None
    working_directory: Optional[str] = None
    registry_fingerprint: Optional[str] = None
    context_fingerprint: Optional[str] = None
    injected_hashes: List[str] = Field(default_factory=list)
    heading: Optional[str] = None


class CacheInvalidateRequest(BaseModel):
    project_id: Optional[str] = None
    purge: bool = False


@app.post("/hook/context")
async def hook_context(
    req: HookContextRequest,
    request: Request,
    engine: RagEngine = Depends(get_rag_engine),
):
    if not req.working_directory and (not req.project_id or not req.root_id):
        raise HTTPException(status_code=422, detail="Explicit project/root or client working_directory is required")
    try:
        context = get_project_registry(settings).resolve(
            project_id=req.project_id,
            root_id=req.root_id,
            working_directory=req.working_directory,
            agent_id=request.state.agent_id,
            session_id=request.state.session_id,
            write_visibility=WriteVisibility.SESSION_PRIVATE.value,
            read_scope=req.read_scope.value,
            min_score=req.min_score,
            include_metadata=True,
            registry_fingerprint_hint=req.registry_fingerprint,
            context_fingerprint_hint=req.context_fingerprint,
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Hook context is not authorized") from exc
    try:
        raw = await run_bounded_search(
            lambda: engine.search(
                context=context, query=req.query, limit=min(req.limit * 3, 24)
            )
        )
    except Exception as exc:
        req_id = str(uuid.uuid4())
        log.error("Hook context error [%s]: %s", req_id, exc, exc_info=True)
        # A hook must never be the reason a prompt fails. Report the miss.
        return {"status": "unavailable", "injected": False, "text": "", "hashes": []}

    results = raw.get("results") or []
    top_score = float(results[0].get("final_score", 0.0)) if results else 0.0

    # Below the calibrated floor the store has nothing to say about this
    # question, and injecting anyway would spend tokens on every turn to tell
    # the model things it did not ask about. See docs/score-calibration.md.
    if top_score < RAG_CONFIDENCE_THRESHOLD:
        return {
            "status": BACKEND_SUCCESS,
            "injected": False,
            "reason": "below_relevance_floor",
            "top_score": round(top_score, 4),
            "text": "",
            "hashes": [],
        }

    model_cfg = get_model_cfg(context.agent_id, "")
    budget = compute_budget(model_cfg, req.query, 0)
    delta, _ = apply_delta_and_budget(results, req.injected_hashes, budget, True)
    delta = delta[: req.limit]
    hits = [compact_hit(hit) for hit in delta]
    text = render_context_block(
        hits,
        max_chars=req.max_chars,
        **({"heading": req.heading} if req.heading else {}),
    )
    return {
        "status": BACKEND_SUCCESS,
        "injected": bool(text),
        "top_score": round(top_score, 4),
        "text": text,
        "hashes": [hit.get("content_hash") for hit in delta if hit.get("content_hash")],
        "chars": len(text),
        "cache": raw.get("cache"),
    }


@app.get("/cache/stats")
def cache_stats(engine: RagEngine = Depends(get_rag_engine)):
    """Hit rates for all three tiers. Operational, not part of retrieval."""
    try:
        return {
            "status": BACKEND_SUCCESS,
            "cache": engine.cache_metrics(),
            # Why the last failure happened, not just that it did. A fail-open
            # cache reports trouble only through this counter, so discarding the
            # reason leaves nothing to act on.
            "last_error": engine._persistent_cache.last_error,
            "local_answer_last_error": engine._persistent_answer_cache.last_error,
        }
    except Exception:
        return {"status": "unavailable", "cache": {}}


@app.post("/cache/invalidate")
def cache_invalidate(
    req: CacheInvalidateRequest,
    engine: RagEngine = Depends(get_rag_engine),
):
    """Retire cached answers by hand, for when something changed underneath.

    Routine mutations already invalidate themselves; this is for the cases the
    engine cannot see - a restored dump, a manual UPDATE, a re-embed.
    """
    project_id = req.project_id or ""
    removed = (
        engine._persistent_cache.invalidate_all()
        + engine._persistent_answer_cache.invalidate_all()
        if req.purge
        else 0
    )
    generation = None
    if project_id:
        generation = engine._persistent_cache.bump_generation(project_id)
        if hasattr(engine, "_project_cache_generations"):
            engine._project_cache_generations[project_id] = generation
        if hasattr(engine, "_project_generation_checked_at"):
            engine._project_generation_checked_at[project_id] = time.monotonic()
    elif req.purge:
        if hasattr(engine, "_project_cache_generations"):
            engine._project_cache_generations.clear()
        if hasattr(engine, "_project_generation_checked_at"):
            engine._project_generation_checked_at.clear()
    engine._query_cache.invalidate()
    return {
        "status": BACKEND_SUCCESS,
        "generation": generation,
        "rows_removed": removed,
    }


@app.post("/save")
async def save(
    req: SaveRequest,
    request: Request,
    engine: RagEngine = Depends(get_rag_engine)
):
    from asyncio import get_event_loop
    loop = get_event_loop()
    context = req.context.to_context(
        agent_id=request.state.agent_id, session_id=request.state.session_id
    )

    try:
        result = await loop.run_in_executor(
            None,
            lambda: engine.save(
                context=context,
                content=req.content,
                metadata=req.metadata,
                source_uri=req.source_uri,
                source_type=req.source_type,
                file_path=req.file_path,
                document_id=req.document_id,
                start_locator=req.start_locator,
                end_locator=req.end_locator,
                importance=req.metadata.get("importance", 5),
            )
        )
        if result.get("status", BACKEND_SUCCESS) not in (BACKEND_SUCCESS, "ok"):
            raise RuntimeError("save backend returned an error status")
        return {**result, "status": BACKEND_SUCCESS}
    except Exception as e:
        req_id = str(uuid.uuid4())
        log.error("Save error [%s]: %s", req_id, e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "SAVE_FAILED", "message": "Failed to save knowledge chunk", "request_id": req_id}}
        )


@app.post("/deprecate")
@app.post("/v1/deprecate")
async def deprecate(req: DeprecateRequest, request: Request, engine: RagEngine = Depends(get_rag_engine)):
    """Retire a stored fact in place.

    Unlike `/save`, this is expected to report `updated: 0` sometimes — a
    retraction that matched nothing is a result the caller has to see, not an
    error to swallow.
    """
    context = req.context.to_context(agent_id=request.state.agent_id, session_id=request.state.session_id)
    from asyncio import get_event_loop
    loop = get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: engine.deprecate(
                context,
                chunk_id=req.chunk_id,
                content_hash=req.content_hash,
                status=req.status,
                reason=req.reason,
                superseded_by=req.superseded_by,
            ),
        )
    except Exception as e:
        req_id = str(uuid.uuid4())
        log.error("Deprecate error [%s]: %s", req_id, e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "DEPRECATE_FAILED", "message": "Failed to retire knowledge chunk", "request_id": req_id}}
        )
    if result.get("status") == "error":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "DEPRECATE_REJECTED", "message": result.get("message", "Invalid deprecation request")}}
        )
    return {**result, "status": BACKEND_SUCCESS}


@app.post("/verify")
@app.post("/v1/verify")
async def verify(req: VerifyRequest, request: Request, engine: RagEngine = Depends(get_rag_engine)):
    """Record that a fact was re-confirmed. Reports `updated: 0` when nothing matched."""
    context = req.context.to_context(agent_id=request.state.agent_id, session_id=request.state.session_id)
    from asyncio import get_event_loop
    loop = get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: engine.verify(
                context,
                chunk_id=req.chunk_id,
                content_hash=req.content_hash,
                recheck_after_days=req.recheck_after_days,
                note=req.note,
            ),
        )
    except Exception as e:
        req_id = str(uuid.uuid4())
        log.error("Verify error [%s]: %s", req_id, e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "VERIFY_FAILED", "message": "Failed to record verification", "request_id": req_id}}
        )
    if result.get("status") == "error":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "VERIFY_REJECTED", "message": result.get("message", "Invalid verification request")}}
        )
    return {**result, "status": BACKEND_SUCCESS}


@app.post("/stale_report")
@app.post("/v1/stale_report")
async def stale_report(req: StaleReportRequest, request: Request, engine: RagEngine = Depends(get_rag_engine)):
    """List knowledge that is overdue for re-confirmation. Read-only."""
    context = req.context.to_context(agent_id=request.state.agent_id, session_id=request.state.session_id)
    from asyncio import get_event_loop
    loop = get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: engine.stale_report(
                context,
                limit=req.limit,
                min_importance=req.min_importance,
                include_undeclared_after_days=req.include_undeclared_after_days,
            ),
        )
        if result.get("status") != "success":
            raise RuntimeError("staleness backend returned an error status")
        return {**result, "status": BACKEND_SUCCESS}
    except Exception as e:
        req_id = str(uuid.uuid4())
        log.error("Stale report error [%s]: %s", req_id, e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "STALE_REPORT_FAILED", "message": "Failed to build staleness report", "request_id": req_id}}
        )


@app.post("/index_code")
@app.post("/index")
async def index_code(req: IndexRequest, request: Request, engine: RagEngine = Depends(get_rag_engine)):
    context = req.context.to_context(agent_id=request.state.agent_id, session_id=request.state.session_id)
    valid_path = context.validate_path(req.path, must_exist=True)
    from asyncio import get_event_loop
    loop = get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: engine.index_code(context, str(valid_path))
        )
        raw_status = result.get("status", BACKEND_SUCCESS)
        if raw_status not in (BACKEND_SUCCESS, "ok", "partial", "fallback_success"):
            raise RuntimeError("index backend returned an error status")
        out_status = raw_status if raw_status in ("partial", "fallback_success") else BACKEND_SUCCESS
        return {**result, "status": out_status}
    except Exception as e:
        req_id = str(uuid.uuid4())
        log.error("Index code error [%s]: %s", req_id, e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "INDEX_FAILED", "message": "Failed to index code file", "request_id": req_id}}
        )


@app.post("/index_document")
async def index_document(req: IndexRequest, request: Request, engine: RagEngine = Depends(get_rag_engine)):
    context = req.context.to_context(agent_id=request.state.agent_id, session_id=request.state.session_id)
    valid_path = context.validate_path(req.path, must_exist=True)
    from asyncio import get_event_loop
    loop = get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: engine.index_document(context, str(valid_path))
        )
        if result.get("status", BACKEND_SUCCESS) not in (BACKEND_SUCCESS, "ok"):
            raise RuntimeError("index backend returned an error status")
        return {**result, "status": BACKEND_SUCCESS}
    except Exception as e:
        req_id = str(uuid.uuid4())
        log.error("Index document error [%s]: %s", req_id, e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "INDEX_DOC_FAILED", "message": "Failed to index document file", "request_id": req_id}}
        )


@app.post("/delete_document")
async def delete_document(req: DeleteRequest, request: Request, engine: RagEngine = Depends(get_rag_engine)):
    context = req.context.to_context(agent_id=request.state.agent_id, session_id=request.state.session_id)
    valid_path = context.validate_path(req.path, must_exist=False)
    from asyncio import get_event_loop
    loop = get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: engine.delete_document(context, str(valid_path))
        )
        if result.get("status", BACKEND_SUCCESS) not in (BACKEND_SUCCESS, "ok"):
            raise RuntimeError("delete backend returned an error status")
        return {**result, "status": BACKEND_SUCCESS}
    except Exception as e:
        req_id = str(uuid.uuid4())
        log.error("Delete document error [%s]: %s", req_id, e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "DELETE_DOC_FAILED", "message": "Failed to delete document", "request_id": req_id}}
        )


@app.post("/move_document")
async def move_document(req: MoveRequest, request: Request, engine: RagEngine = Depends(get_rag_engine)):
    context = req.context.to_context(agent_id=request.state.agent_id, session_id=request.state.session_id)
    source = context.validate_path(req.source_path, must_exist=False)
    destination = context.validate_path(req.destination_path, must_exist=True)
    from asyncio import get_event_loop
    loop = get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: engine.move_document(context, str(source), str(destination))
        )
        if result.get("status", BACKEND_SUCCESS) not in (BACKEND_SUCCESS, "ok"):
            raise RuntimeError("move backend returned an error status")
        if result.get("moved_count", 0) < 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": {"code": "MOVE_NOT_APPLIED", "message": "Document move was not applied"}},
            )
        return {**result, "status": BACKEND_SUCCESS}
    except HTTPException:
        raise
    except Exception:
        req_id = str(uuid.uuid4())
        log.error("Move document error [%s]", req_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "MOVE_FAILED", "message": "Failed to move document", "request_id": req_id}},
        )


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request, engine: RagEngine = Depends(get_rag_engine)):
    from asyncio import get_event_loop
    loop = get_event_loop()
    try:
        context = req.context.to_context(
            agent_id=request.state.agent_id, session_id=request.state.session_id
        )
        result = await loop.run_in_executor(
            None, lambda: engine.log_feedback(
                context, req.feedback_type, req.query, req.correction
            )
        )
        if result.get("status", BACKEND_SUCCESS) not in (BACKEND_SUCCESS, "ok"):
            raise RuntimeError("feedback backend returned an error status")
        return {**result, "status": BACKEND_SUCCESS}
    except Exception as e:
        req_id = str(uuid.uuid4())
        log.error("Feedback error [%s]: %s", req_id, e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "FEEDBACK_ERROR", "message": "Internal server error processing feedback.", "request_id": req_id}}
        )


@app.post("/consolidate")
async def consolidate(req: ConsolidateRequest, request: Request, engine: RagEngine = Depends(get_rag_engine)):
    from asyncio import get_event_loop
    loop = get_event_loop()
    try:
        context = req.context.to_context(
            agent_id=request.state.agent_id, session_id=request.state.session_id
        )
        result = await loop.run_in_executor(
            None, lambda: engine.consolidate_episodic_to_semantic(
                context, decay_days=req.decay_days, min_importance=req.min_importance
            )
        )
        return {**result, "status": BACKEND_SUCCESS}
    except Exception as e:
        req_id = str(uuid.uuid4())
        log.error("Consolidate error [%s]: %s", req_id, e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "CONSOLIDATE_ERROR", "message": "Internal server error during consolidation.", "request_id": req_id}}
        )


@app.post("/memory_optimize")
@app.post("/memory-optimize")
async def memory_optimize(req: MemoryOptimizeRequest, request: Request, engine: RagEngine = Depends(get_rag_engine)):
    from asyncio import get_event_loop
    loop = get_event_loop()
    try:
        context = req.context.to_context(
            agent_id=request.state.agent_id, session_id=request.state.session_id
        )
        kwargs = {"sim_threshold": req.sim_threshold}
        if req.apply:
            kwargs["apply"] = True
        result = await loop.run_in_executor(
            None, lambda: engine.memory_optimize(context, **kwargs)
        )
        return {**result, "status": BACKEND_SUCCESS}
    except Exception as e:
        req_id = str(uuid.uuid4())
        log.error("Memory optimize error [%s]: %s", req_id, e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "MEMORY_OPTIMIZE_ERROR", "message": "Internal server error during memory optimization.", "request_id": req_id}}
        )


if __name__ == "__main__":
    import uvicorn
    settings.require_safe_bind(SERVER_HOST)
    log.info("🚀 GPU RAG Memory Server starting on %s:%d", SERVER_HOST, SERVER_PORT)
    access_log = os.getenv("UVICORN_ACCESS_LOG", "0").lower() in {"1", "true", "yes", "on"}
    uvicorn.run(
        "gpu_rag_server:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level="info",
        access_log=access_log,
    )
