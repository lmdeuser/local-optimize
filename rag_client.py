#!/usr/bin/env python3
"""Lightweight HTTP client with mandatory ProjectContext propagation."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from config import settings
from local_inference import correlation_id
from project_context import (
    GlobalKnowledgeContext,
    GlobalWriteContext,
    KnowledgeContext,
    ProjectContext,
    WorkingDirectoryContext,
)

RequestContext = ProjectContext | KnowledgeContext | WorkingDirectoryContext | GlobalKnowledgeContext | GlobalWriteContext

log = logging.getLogger("rag_client")

CANONICAL_STATUSES = frozenset({"success", "error", "unavailable"})


class RagProtocolError(RuntimeError):
    """Raised when the server violates the versioned RAG HTTP contract."""


class RagClient:
    def __init__(
        self,
        context: RequestContext,
        model_name: str = "",
        server_url: str = "",
    ) -> None:
        if not isinstance(context, (ProjectContext, KnowledgeContext, WorkingDirectoryContext, GlobalKnowledgeContext, GlobalWriteContext)):
            raise TypeError("RagClient requires a validated ProjectContext or search context")
        self.context = context
        self.model_name = model_name
        self.server_url = server_url or settings.rag_server_url
        self._injected_hashes: set[tuple[tuple[Any, ...], str]] = set()
        self._ctx_tokens_used = 0

    @staticmethod
    def _require_range(name: str, value: int, minimum: int, maximum: int) -> int:
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return parsed

    @staticmethod
    def _require_text(name: str, value: str, maximum: int, *, minimum: int = 1) -> str:
        normalized = str(value or "")
        if not minimum <= len(normalized.strip()) <= maximum:
            raise ValueError(f"{name} length must be between {minimum} and {maximum}")
        return normalized

    def _base_payload(
        self,
        context: RequestContext | None = None,
        *,
        allow_knowledge_context: bool = False,
        allow_global_write: bool = False,
    ) -> dict[str, Any]:
        current = context or self.context
        if isinstance(current, (KnowledgeContext, GlobalKnowledgeContext)) and not allow_knowledge_context:
            raise TypeError("This operation requires a filesystem-capable context")
        if isinstance(current, GlobalWriteContext) and not allow_global_write:
            raise TypeError("This operation requires a filesystem-capable context")
        return {"context": current.to_request_dict()}

    def search(
        self,
        query: str,
        limit: int = 5,
        *,
        context: RequestContext | None = None,
    ) -> dict:
        query = self._require_text("query", query, settings.max_query_chars)
        limit = self._require_range("limit", limit, 1, settings.max_search_limit)
        current = context or self.context
        search_scope = current.search_cache_key(query, limit=limit)
        payload = self._base_payload(current, allow_knowledge_context=True)
        payload.update(
            {
                "query": query,
                "limit": limit,
                "model_name": self.model_name,
                "ctx_tokens_used": self._ctx_tokens_used,
                "injected_hashes": [
                    digest for scope, digest in self._injected_hashes if scope == search_scope
                ],
            }
        )
        result = self._post(
            "/search", payload, current.session_id,
            timeout=settings.rag_search_timeout_seconds,
        )
        for chunk in result.get("results", []):
            if chunk.get("content_hash"):
                self._injected_hashes.add((search_scope, chunk["content_hash"]))
        return result

    def lookup_local_answer(
        self,
        prompt_hash: str,
        *,
        context: RequestContext | None = None,
    ) -> dict:
        current = context or self.context
        if isinstance(current, GlobalKnowledgeContext):
            raise TypeError("Local answer cache requires one project context")
        payload = self._base_payload(current, allow_knowledge_context=True)
        payload["prompt_hash"] = prompt_hash
        return self._post("/answer-cache/lookup", payload, current.session_id, timeout=2.0)

    def store_local_answer(
        self,
        prompt_hash: str,
        answer: str,
        expected_generation: int,
        *,
        context: RequestContext | None = None,
    ) -> dict:
        current = context or self.context
        if isinstance(current, GlobalKnowledgeContext):
            raise TypeError("Local answer cache requires one project context")
        payload = self._base_payload(current, allow_knowledge_context=True)
        payload.update(
            {
                "prompt_hash": prompt_hash,
                "answer": answer,
                "expected_generation": expected_generation,
            }
        )
        return self._post("/answer-cache/store", payload, current.session_id, timeout=2.0)

    def agentic_search(
        self,
        query: str,
        limit: int = 5,
        *,
        context: RequestContext | None = None,
        max_depth: int = 2,
        max_entities: int = 20,
        max_relations: int = 36,
        timeout_ms: int = 5000,
    ) -> dict:
        query = self._require_text("query", query, settings.max_query_chars)
        limit = self._require_range("limit", limit, 1, settings.max_agentic_limit)
        max_depth = self._require_range("max_depth", max_depth, 1, settings.max_agentic_depth)
        max_entities = self._require_range("max_entities", max_entities, 1, settings.max_agentic_entities)
        max_relations = self._require_range("max_relations", max_relations, 1, settings.max_agentic_relations)
        timeout_ms = self._require_range("timeout_ms", timeout_ms, 1, settings.max_agentic_timeout_ms)
        current = context or self.context
        search_scope = current.search_cache_key(
            query,
            limit=limit,
            search_mode="agentic-search",
            parameters=(max_depth, max_entities, max_relations, timeout_ms),
        )
        payload = self._base_payload(current, allow_knowledge_context=True)
        payload.update(
            {
                "query": query,
                "limit": limit,
                "max_depth": max_depth,
                "max_entities": max_entities,
                "max_relations": max_relations,
                "timeout_ms": timeout_ms,
                "model_name": self.model_name,
                "ctx_tokens_used": self._ctx_tokens_used,
                "injected_hashes": [
                    digest for scope, digest in self._injected_hashes if scope == search_scope
                ],
            }
        )
        result = self._post(
            "/agentic-search", payload, current.session_id,
            timeout=settings.rag_search_timeout_seconds,
        )
        for chunk in result.get("results", []):
            if chunk.get("content_hash"):
                self._injected_hashes.add((search_scope, chunk["content_hash"]))
        return result

    def save(
        self,
        content: str,
        *,
        context: ProjectContext | None = None,
        metadata: dict[str, Any] | None = None,
        source_uri: str = "",
        source_type: str = "general",
        file_path: str = "",
        document_id: int | None = None,
        start_locator: str = "",
        end_locator: str = "",
    ) -> dict:
        content = self._require_text("content", content, settings.max_save_chars, minimum=10)
        payload = self._base_payload(context, allow_global_write=True)
        payload.update(
            {
                "content": content,
                "metadata": metadata or {},
                "source_uri": source_uri,
                "source_type": source_type,
                "file_path": file_path,
                "document_id": document_id,
                "start_locator": start_locator,
                "end_locator": end_locator,
            }
        )
        return self._post("/save", payload, (context or self.context).session_id)

    def index_code(self, path: str, *, context: ProjectContext | None = None) -> dict:
        payload = self._base_payload(context)
        payload["path"] = path
        return self._post("/index_code", payload, (context or self.context).session_id)

    def index_document(self, path: str, *, context: ProjectContext | None = None) -> dict:
        payload = self._base_payload(context)
        payload["path"] = path
        return self._post("/index_document", payload, (context or self.context).session_id)

    def delete_document(self, path: str, *, context: ProjectContext | None = None) -> dict:
        payload = self._base_payload(context)
        payload["path"] = path
        return self._post("/delete_document", payload, (context or self.context).session_id)

    def move_document(
        self,
        source_path: str,
        destination_path: str,
        *,
        context: ProjectContext | None = None,
    ) -> dict:
        payload = self._base_payload(context)
        payload.update({"source_path": source_path, "destination_path": destination_path})
        return self._post("/move_document", payload, (context or self.context).session_id)

    def feedback(self, query: str, feedback_type: str, correction: str = "") -> dict:
        payload = self._base_payload()
        payload.update({"query": query, "feedback_type": feedback_type, "correction": correction})
        return self._post("/feedback", payload, self.context.session_id)

    def deprecate(
        self,
        *,
        context: ProjectContext | None = None,
        chunk_id: int | None = None,
        content_hash: str = "",
        status: str = "deprecated",
        reason: str = "",
        superseded_by: int | None = None,
    ) -> dict:
        current = context or self.context
        payload = self._base_payload(current, allow_global_write=True)
        payload.update(
            {
                "chunk_id": chunk_id,
                "content_hash": content_hash,
                "status": status,
                "reason": reason,
                "superseded_by": superseded_by,
            }
        )
        return self._post("/deprecate", payload, current.session_id)

    def verify(
        self,
        *,
        context: ProjectContext | None = None,
        chunk_id: int | None = None,
        content_hash: str = "",
        recheck_after_days: int | None = None,
        note: str = "",
    ) -> dict:
        current = context or self.context
        payload = self._base_payload(current, allow_global_write=True)
        payload.update(
            {
                "chunk_id": chunk_id,
                "content_hash": content_hash,
                "recheck_after_days": recheck_after_days,
                "note": note,
            }
        )
        return self._post("/verify", payload, current.session_id)

    def stale_report(
        self,
        *,
        context: ProjectContext | None = None,
        limit: int = 25,
        min_importance: int = 1,
        include_undeclared_after_days: int | None = None,
    ) -> dict:
        current = context or self.context
        payload = self._base_payload(current)
        payload.update(
            {
                "limit": limit,
                "min_importance": min_importance,
                "include_undeclared_after_days": include_undeclared_after_days,
            }
        )
        return self._post("/stale_report", payload, current.session_id)

    def consolidate(self, decay_days: float = 30.0, min_importance: int = 1) -> dict:
        payload = self._base_payload()
        payload.update({"decay_days": decay_days, "min_importance": min_importance})
        return self._post("/consolidate", payload, self.context.session_id)

    def memory_optimize(self, sim_threshold: float = 0.98, apply: bool = False) -> dict:
        payload = self._base_payload()
        payload.update({"sim_threshold": sim_threshold})
        if apply:
            payload["apply"] = True
        return self._post("/memory_optimize", payload, self.context.session_id)

    def health(self) -> dict:
        return self._get("/liveness")

    def readiness(self) -> dict:
        return self._get("/readiness")

    def runtime(self) -> dict:
        return self._get("/runtime")

    def reset_delta(self) -> None:
        self._injected_hashes.clear()
        self._ctx_tokens_used = 0

    def update_ctx_tokens(self, tokens: int) -> None:
        self._ctx_tokens_used = tokens

    def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        session_id: str = "",
        *,
        timeout: float = 30.0,
    ) -> dict:
        url = self.server_url.rstrip("/") + endpoint
        encoded = json.dumps(payload).encode("utf-8")
        if len(encoded) > settings.max_request_body_bytes:
            raise ValueError("Request body exceeds configured maximum")
        headers = {"Content-Type": "application/json", "X-Request-ID": correlation_id()}
        if session_id:
            headers["X-Local-RAG-Session"] = session_id
        token = settings.bearer_token.get_secret_value()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            url,
            data=encoded,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return self._validate_response(endpoint, response.read())
        except urllib.error.HTTPError as exc:
            # Do not echo response bodies: upstream errors may contain sensitive data.
            if exc.code in (403, 422):
                return {
                    "status": "error",
                    "code": "CONTEXT_NOT_AUTHORIZED" if exc.code == 403 else "INVALID_CONTEXT",
                    "message": "Client context is missing, unknown, ambiguous or no longer authorized",
                }
            backend_status = "unavailable" if exc.code == 503 else "error"
            return {"status": backend_status, "message": f"HTTP {exc.code}"}
        except TimeoutError:
            return {"status": "unavailable", "message": "RAG search request timed out"}
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                return {"status": "unavailable", "message": "RAG search request timed out"}
            return {"status": "unavailable", "message": "RAG server is unavailable"}

    def _validate_response(self, endpoint: str, raw: bytes) -> dict[str, Any]:
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RagProtocolError("RAG server returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RagProtocolError("RAG server response must be an object")
        status_value = result.get("status")
        if status_value not in CANONICAL_STATUSES:
            raise RagProtocolError("RAG server response has an invalid or missing status")
        required_by_endpoint = {
            "/search": (
                "results", "route", "top_score", "evidence_coverage",
                "budget_tokens", "tokens_used", "agent_tier",
            ),
            "/agentic-search": ("results", "budget_tokens", "tokens_used", "agent_tier"),
            "/answer-cache/lookup": ("hit", "generation"),
            "/answer-cache/store": ("stored",),
            "/save": ("status",),
            "/deprecate": ("status", "updated", "validation_status"),
            "/verify": ("status", "updated"),
            "/stale_report": ("status", "overdue", "overdue_count", "ageing", "ageing_count", "limit"),
            "/index_code": ("status",),
            "/index_document": ("status",),
            "/delete_document": ("status",),
            "/move_document": ("status",),
            "/feedback": ("status",),
            "/consolidate": ("status",),
            "/memory_optimize": ("status",),
            "/liveness": ("status",),
            "/readiness": ("status", "ready"),
            "/runtime": (
                "status", "backend", "device", "embedding_device",
                "similarity_device", "cpu_fallback", "torch_cpu_threads",
                "model_warmed_up", "evidence_ranker",
            ),
        }
        missing = [key for key in required_by_endpoint.get(endpoint, ()) if key not in result]
        if missing:
            raise RagProtocolError("RAG server response is missing required fields")
        for list_field in ("results", "overdue", "ageing"):
            if list_field in result and not isinstance(result[list_field], list):
                raise RagProtocolError(f"RAG server {list_field} must be a list")
        return result

    def _get(self, endpoint: str) -> dict:
        try:
            headers = {}
            token = settings.bearer_token.get_secret_value()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            request = urllib.request.Request(
                self.server_url.rstrip("/") + endpoint, headers=headers
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return self._validate_response(endpoint, response.read())
        except urllib.error.HTTPError as exc:
            backend_status = "unavailable" if exc.code == 503 else "error"
            return {"status": backend_status, "message": f"HTTP {exc.code}"}
        except urllib.error.URLError:
            return {"status": "unavailable", "message": "RAG server is unavailable"}
