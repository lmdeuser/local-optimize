import asyncio
import threading

import pytest
from fastapi import HTTPException

import gpu_rag_server as server

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_timeout_keeps_slot_until_worker_exits_then_recovers(monkeypatch):
    monkeypatch.setattr(server, "_search_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(server, "SEARCH_EXECUTION_TIMEOUT", 0.05)
    release = threading.Event()
    finished = threading.Event()

    def slow():
        try:
            release.wait(2)
        finally:
            finished.set()

    try:
        with pytest.raises(HTTPException) as first:
            await server.run_bounded_search(slow)
        assert first.value.detail["error"]["code"] == "RAG_SEARCH_TIMEOUT"
        with pytest.raises(HTTPException) as second:
            await server.run_bounded_search(lambda: None)
        assert second.value.detail["error"]["code"] == "RAG_SEARCH_BUSY"
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 1)
    assert await server.run_bounded_search(lambda: 42) == 42
