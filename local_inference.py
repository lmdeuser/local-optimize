"""Bounded, cancellable, loopback-only inference shared by every MCP path."""
from __future__ import annotations

import asyncio
import contextvars
import errno
import fcntl
import http.client
import json
import logging
import os
import socket
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import ParamSpec, TypeVar

log = logging.getLogger(__name__)
P = ParamSpec("P")
T = TypeVar("T")
_cancel: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar("inference_cancel", default=None)
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("inference_request_id", default="")
_STATE_HOME = Path(os.getenv("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
LOCK_PATH = Path(os.getenv("LOCAL_OPTIMIZE_STATE_DIR", str(_STATE_HOME / "local-optimize"))) / "inference.lock"


def correlation_id(value: str | None = None) -> str:
    """Set or obtain a per-request identifier without any user data."""
    if value is not None:
        _request_id.set(str(uuid.UUID(value)))
    return _request_id.get() or str(uuid.uuid4())


class InferenceUnavailable(RuntimeError):
    """Safe machine-readable reason, never a response body or prompt."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


async def run_cancellable(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Propagate MCP cancellation to the socket owned by a synchronous worker."""
    event = threading.Event()
    token = _cancel.set(event)
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    finally:
        event.set()
        _cancel.reset(token)


def _exchange(base_url: str, path: str, payload: dict | None, deadline: float) -> dict:
    """Close the actual socket on cancellation/deadline, including header wait."""
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise InferenceUnavailable("LOCAL_ENDPOINT_REQUIRED")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Local inference deadline exceeded")
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=remaining)
    done = threading.Event()
    cancelled = _cancel.get()
    request_id = _request_id.get()
    transport_socket = None
    response = None

    def watchdog() -> None:
        while not done.wait(0.05):
            if time.monotonic() >= deadline or (cancelled is not None and cancelled.is_set()):
                current = transport_socket or conn.sock
                if current is not None:
                    try:
                        current.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        return
                return

    watcher = threading.Thread(target=watchdog, daemon=True, name="inference-deadline")
    watcher.start()
    started = time.monotonic()
    try:
        if cancelled is not None and cancelled.is_set():
            raise InferenceUnavailable("LOCAL_MODEL_CANCELLED")
        conn.connect()
        transport_socket = conn.sock
        data = json.dumps(payload).encode() if payload is not None else None
        conn.request("POST" if data else "GET", path, data,
                     {"Content-Type": "application/json", "X-Request-ID": request_id})
        # HTTP/1.0 may detach conn.sock when response headers arrive. Keep the
        # original socket so cancellation can still interrupt a streaming read.
        transport_socket = conn.sock or transport_socket
        response = conn.getresponse()
        if response.status != 200:
            raise InferenceUnavailable("LOCAL_MODEL_HTTP_ERROR")
        if payload is None or not payload.get("stream"):
            result = json.loads(response.read(2_000_001))
            if not isinstance(result, dict):
                raise InferenceUnavailable("INVALID_MODEL_RESPONSE")
            return result
        pieces: list[str] = []
        size = 0
        first_token = None
        while True:
            line = response.readline(65537)
            if not line:
                break
            if len(line) > 65536:
                raise InferenceUnavailable("INVALID_MODEL_RESPONSE")
            item = json.loads(line)
            if not isinstance(item, dict) or item.get("error"):
                raise InferenceUnavailable("INVALID_MODEL_RESPONSE")
            message = item.get("message", {})
            content = message.get("content", "") if isinstance(message, dict) else None
            if not isinstance(content, str):
                raise InferenceUnavailable("INVALID_MODEL_RESPONSE")
            if content and first_token is None:
                first_token = round((time.monotonic() - started) * 1000)
            size += len(content)
            if size > 32000:
                raise InferenceUnavailable("MODEL_OUTPUT_LIMIT")
            pieces.append(content)
            if item.get("done") is True:
                answer = "".join(pieces).strip()
                if not answer:
                    raise InferenceUnavailable("EMPTY_MODEL_RESPONSE")
                if item.get("done_reason") == "length":
                    raise InferenceUnavailable("MODEL_OUTPUT_TRUNCATED")
                log.info("inference request_id=%s ttft_ms=%s total_ms=%d prompt_tokens=%s output_tokens=%s",
                         request_id, first_token, (time.monotonic()-started)*1000,
                         item.get("prompt_eval_count"), item.get("eval_count"))
                return {"answer": answer}
        raise InferenceUnavailable("INCOMPLETE_MODEL_RESPONSE")
    except (OSError, http.client.HTTPException, ValueError) as exc:
        if time.monotonic() >= deadline:
            raise TimeoutError("Local inference deadline exceeded") from exc
        if cancelled is not None and cancelled.is_set():
            raise InferenceUnavailable("LOCAL_MODEL_CANCELLED") from exc
        raise InferenceUnavailable("LOCAL_MODEL_TRANSPORT_ERROR") from exc
    finally:
        done.set()
        if response is not None:
            response.close()
        conn.close()
        watcher.join(timeout=0.2)


def _resident(base_url: str, model: str, deadline: float) -> dict | None:
    result = _exchange(base_url, "/api/ps", None, min(deadline, time.monotonic()+3))
    models = result.get("models")
    if not isinstance(models, list):
        raise InferenceUnavailable("MODEL_PLACEMENT_UNKNOWN")
    if not models:
        return None
    if len(models) != 1 or not isinstance(models[0], dict) or model not in {models[0].get("name"), models[0].get("model")}:
        raise InferenceUnavailable("MODEL_POLICY_CONFLICT")
    return models[0]


def _require_gpu(item: dict | None, num_ctx: int) -> None:
    if item is None:
        raise InferenceUnavailable("MODEL_PLACEMENT_UNKNOWN")
    size, vram = item.get("size"), item.get("size_vram")
    if type(size) is not int or type(vram) is not int or size <= 0 or vram < size:
        raise InferenceUnavailable("LOCAL_MODEL_NOT_FULLY_ON_GPU")
    if item.get("context_length") != num_ctx:
        raise InferenceUnavailable("MODEL_CONTEXT_CONFLICT")


def generate(base_url: str, model: str, prompt: str, system_prompt: str, *,
             timeout: float, num_ctx: int, num_predict: int) -> str:
    """One process-wide and cross-process slot; no queue, cloud or CPU fallback."""
    if not 0 < timeout <= 180 or not 0 < num_predict <= 1024:
        raise ValueError("Inference budget outside allowed range")
    # Character count conservatively bounds token count across Latin and Cyrillic text.
    if len(prompt+system_prompt) + num_predict + 128 > num_ctx:
        raise InferenceUnavailable("MODEL_INPUT_LIMIT")
    deadline = time.monotonic()+timeout
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if LOCK_PATH.is_symlink():
        raise InferenceUnavailable("INVALID_LOCK_FILE")
    token = _request_id.set(_request_id.get() or str(uuid.uuid4()))
    try:
        try:
            lock_fd = os.open(
                LOCK_PATH,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise InferenceUnavailable("INVALID_LOCK_FILE") from exc
            raise
        with os.fdopen(lock_fd, "r+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InferenceUnavailable("LOCAL_MODEL_BUSY") from exc
            item = _resident(base_url, model, deadline)
            if item is None:
                loaded = _exchange(base_url, "/api/generate", {
                    "model": model, "stream": False, "keep_alive": -1,
                    "options": {"num_ctx": num_ctx},
                }, deadline)
                if loaded.get("done") is not True:
                    raise InferenceUnavailable("LOCAL_MODEL_LOAD_FAILED")
                item = _resident(base_url, model, deadline)
            _require_gpu(item, num_ctx)
            messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]
            result = _exchange(base_url, "/api/chat", {
                "model": model, "messages": messages, "stream": True, "keep_alive": -1,
                "options": {"temperature": 0.0, "num_ctx": num_ctx, "num_predict": num_predict},
            }, deadline)
            _require_gpu(_resident(base_url, model, deadline), num_ctx)
            return result["answer"]
    finally:
        _request_id.reset(token)
