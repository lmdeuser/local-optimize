"""Transport contract tests against an isolated loopback HTTP server."""
import asyncio
import fcntl
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import local_inference as inference

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    state = {"models": [{"name": "fixture", "size": 10, "size_vram": 10,
                         "context_length": 8192}], "chat": [], "stall": False,
             "answer": "Verified answer", "entered": threading.Event(),
             "disconnected": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            data = json.dumps({"models": state["models"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["chat"].append(payload)
            state["entered"].set()
            if state["stall"]:
                if state.get("headers_sent"):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.flush()
                self.connection.settimeout(2)
                if self.connection.recv(1) == b"":
                    state["disconnected"].set()
                return
            data = (json.dumps({"message": {"content": state["answer"]},
                                "done": True, "done_reason": "stop"})+"\n").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(inference, "LOCK_PATH", tmp_path / "inference.lock")
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join(2)


def generate(state, **options):
    return inference.generate(state["url"], "fixture", "question", "system",
                              timeout=options.get("timeout", 2), num_ctx=8192, num_predict=64)


def test_success_uses_one_fixed_context_and_keep_alive(runtime):
    assert generate(runtime) == "Verified answer"
    assert runtime["chat"][0]["keep_alive"] == -1
    assert runtime["chat"][0]["options"]["num_ctx"] == 8192


@pytest.mark.parametrize("models,code", [
    ([{"name": "other"}], "MODEL_POLICY_CONFLICT"),
    ([{"name": "fixture"}, {"name": "other"}], "MODEL_POLICY_CONFLICT"),
    ([{"name": "fixture", "size": 10, "size_vram": 9}], "LOCAL_MODEL_NOT_FULLY_ON_GPU"),
    ([{"name": "fixture", "size": 10, "size_vram": 10, "context_length": 2048}], "MODEL_CONTEXT_CONFLICT"),
])
def test_policy_rejects_before_generating(runtime, models, code):
    runtime["models"] = models
    with pytest.raises(inference.InferenceUnavailable, match=code):
        generate(runtime)
    assert runtime["chat"] == []


def test_empty_answer_is_not_success(runtime):
    runtime["answer"] = " "
    with pytest.raises(inference.InferenceUnavailable, match="EMPTY_MODEL_RESPONSE"):
        generate(runtime)


def test_deadline_closes_socket_and_next_request_works(runtime):
    runtime["stall"] = True
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        generate(runtime, timeout=0.2)
    assert time.monotonic()-start < 1
    assert runtime["disconnected"].wait(1)
    runtime["stall"] = False
    assert generate(runtime) == "Verified answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("headers_sent", [False, True])
async def test_mcp_cancellation_closes_socket_and_releases_slot(runtime, headers_sent):
    runtime["headers_sent"] = headers_sent
    runtime["stall"] = True
    task = asyncio.create_task(inference.run_cancellable(generate, runtime))
    assert await asyncio.to_thread(runtime["entered"].wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(runtime["disconnected"].wait, 1)
    # Wait for the worker's finally block, not for its original 2s timeout.
    for _ in range(20):
        with inference.LOCK_PATH.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.02)
    else:
        pytest.fail("cancelled worker retained the inference slot")
    runtime["stall"] = False
    assert await inference.run_cancellable(generate, runtime) == "Verified answer"


def test_second_process_slot_is_rejected_without_http(runtime):
    with inference.LOCK_PATH.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(inference.InferenceUnavailable, match="LOCAL_MODEL_BUSY"):
            generate(runtime)
    assert runtime["chat"] == []


def test_cyrillic_prompt_within_character_budget_is_accepted(runtime):
    """Cyrillic UTF-8 uses 2 bytes/char; budget check must use character length, not UTF-8 bytes."""
    cyrillic_prompt = "Проверка кириллического контекста " * 140  # ~4760 chars (>9500 UTF-8 bytes)
    assert inference.generate(
        runtime["url"], "fixture", cyrillic_prompt, "Системный промпт",
        timeout=2, num_ctx=8192, num_predict=512,
    ) == "Verified answer"

