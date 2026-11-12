"""The prompt hook runs before every message. It has one job when things break.

Whatever happens - the server is down, hung, lying, or the state file is
garbage - the hook must exit 0 and print either nothing or a well-formed hook
envelope. Anything else lands in the editor: a traceback becomes visible noise,
a hang becomes a stall before every prompt the user types, and malformed stdout
is text the model reads as if someone had said it.

Fail-open is easy to claim and easy to get almost right, so this walks the
failure modes as a subprocess - the way the editor actually invokes it - rather
than importing the module and trusting its try blocks.

HOME is redirected per test so the hook's state directory lands in tmp_path and
these never touch the real one.
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


HOOKS = Path(__file__).resolve().parents[2] / "hooks"
PROMPT_HOOK = HOOKS / "rag_user_prompt.py"
SESSION_HOOK = HOOKS / "rag_session_start.py"
HARVEST_HOOK = HOOKS / "rag_harvest.py"

VALID_PAYLOAD = {
    "session_id": "abcd-1234",
    "cwd": str(Path(__file__).resolve().parents[2]),
    "hook_event_name": "UserPromptSubmit",
    "prompt": "как устроено кеширование запросов",
}


class Behaviour:
    """What the stand-in server does when the hook calls it."""

    def __init__(self, mode, delay=0.0, body=b"", status=200):
        self.mode = mode
        self.delay = delay
        self.body = body
        self.status = status


def serve(behaviour):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            from config import settings
            data = json.dumps({"models": [{"name": settings.local_model,
                "size": 10, "size_vram": 10, "context_length": settings.local_model_context}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if behaviour.delay:
                time.sleep(behaviour.delay)
            if behaviour.mode == "reset":
                self.close_connection = True
                return
            self.send_response(behaviour.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(behaviour.body)))
            self.end_headers()
            self.wfile.write(behaviour.body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run_hook(payload, tmp_path, *, url, env=None, hook=PROMPT_HOOK, timeout=20):
    environment = dict(os.environ)
    environment["HOME"] = str(tmp_path)
    environment["RAG_HOOK_URL"] = url
    environment.pop("RAG_HOOK_DISABLE", None)
    environment.update(env or {})
    stdin = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    if isinstance(stdin, str):
        stdin = stdin.encode("utf-8")
    started = time.monotonic()
    command = [sys.executable, str(hook)]
    if hook == HARVEST_HOOK:
        # The production lock is intentionally shared across real clients;
        # isolated fixtures must not contend with a live inference request.
        command = [sys.executable, "-c",
                   "import sys,runpy; from pathlib import Path; "
                   "sys.path.insert(0,sys.argv[1]); import local_inference; "
                   "local_inference.LOCK_PATH=Path(sys.argv[2]); "
                   "runpy.run_path(sys.argv[3],run_name='__main__')",
                   str(HOOKS.parent), str(tmp_path / "inference.lock"), str(hook)]
    finished = subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        timeout=timeout,
        # Without this the hook inherits the real environment: it talks to the
        # live server instead of the stand-in and writes into the real state
        # directory. Every assertion below would still pass, against the wrong
        # process. Omitting it once already turned this whole file green while
        # testing almost nothing.
        env=environment,
    )
    return finished, time.monotonic() - started


def assert_silent_success(finished, note):
    assert finished.returncode == 0, (
        f"{note}: exited {finished.returncode}. A non-zero hook is an error the "
        f"editor shows the user. stderr: {finished.stderr.decode()[:400]}"
    )
    body = finished.stdout.decode().strip()
    if not body:
        return
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        pytest.fail(f"{note}: printed something that is not JSON: {body[:300]!r}")
    assert "hookSpecificOutput" in parsed, f"{note}: printed unexpected JSON: {body[:300]}"


@pytest.fixture
def dead_port():
    """A port nobody is listening on, obtained by closing one we just bound."""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


class TestTheServerMisbehaves:
    def test_nothing_is_listening(self, tmp_path, dead_port):
        finished, _ = run_hook(VALID_PAYLOAD, tmp_path, url=dead_port)
        assert_silent_success(finished, "connection refused")

    def test_it_hangs(self, tmp_path):
        server = serve(Behaviour("ok", delay=30, body=b"{}"))
        try:
            port = server.server_address[1]
            finished, elapsed = run_hook(
                VALID_PAYLOAD, tmp_path,
                url=f"http://127.0.0.1:{port}",
                env={"RAG_HOOK_TIMEOUT": "1.0"},
            )
            assert_silent_success(finished, "server hung")
            assert elapsed < 12, (
                f"took {elapsed:.1f}s. This runs before every prompt the user "
                "types; the timeout has to be the thing that ends it."
            )
        finally:
            server.shutdown()

    def test_it_drops_the_connection(self, tmp_path):
        server = serve(Behaviour("reset"))
        try:
            finished, _ = run_hook(
                VALID_PAYLOAD, tmp_path, url=f"http://127.0.0.1:{server.server_address[1]}"
            )
            assert_silent_success(finished, "connection reset")
        finally:
            server.shutdown()

    @pytest.mark.parametrize(
        "name,behaviour",
        [
            ("html error page", Behaviour("ok", body=b"<html>502 Bad Gateway</html>")),
            ("truncated json", Behaviour("ok", body=b'{"injected": tr')),
            ("json but not an object", Behaviour("ok", body=b"[1, 2, 3]")),
            ("empty body", Behaviour("ok", body=b"")),
            ("server error", Behaviour("ok", body=b'{"detail":"boom"}', status=500)),
            ("unauthorized", Behaviour("ok", body=b'{"detail":"no"}', status=401)),
            ("object without the keys", Behaviour("ok", body=b'{"unexpected": true}')),
            ("text is not a string", Behaviour("ok", body=b'{"injected":true,"text":[1]}')),
            ("injected without text", Behaviour("ok", body=b'{"injected":true}')),
            ("enormous body", Behaviour("ok", body=b'{"injected":true,"text":"' + b"x" * 900000 + b'"}')),
        ],
    )
    def test_a_lying_server_changes_nothing(self, tmp_path, name, behaviour):
        server = serve(behaviour)
        try:
            finished, _ = run_hook(
                VALID_PAYLOAD, tmp_path, url=f"http://127.0.0.1:{server.server_address[1]}"
            )
            assert_silent_success(finished, name)
        finally:
            server.shutdown()


class TestTheEditorSendsSomethingOdd:
    @pytest.mark.parametrize(
        "name,payload",
        [
            ("empty stdin", b""),
            ("not json", b"this is not json at all"),
            ("json array", b"[]"),
            ("json null", b"null"),
            ("no prompt key", {"session_id": "s", "cwd": "/tmp"}),
            ("prompt is not a string", {"session_id": "s", "cwd": "/tmp", "prompt": 42}),
            ("no session id", {"cwd": "/tmp", "prompt": "вопрос про кеш"}),
            ("session id with slashes", {"session_id": "../../etc/passwd", "cwd": "/tmp", "prompt": "вопрос про кеш"}),
            ("cwd does not exist", {"session_id": "s", "cwd": "/nonexistent/xyz", "prompt": "вопрос про кеш"}),
            ("lone surrogate", {"session_id": "s", "cwd": "/tmp", "prompt": "вопрос \ud800 хвост"}),
            ("null bytes", {"session_id": "s", "cwd": "/tmp", "prompt": "вопрос\x00про\x00кеш"}),
            ("very long prompt", {"session_id": "s", "cwd": "/tmp", "prompt": "кеш " * 60000}),
        ],
    )
    def test_the_hook_still_exits_clean(self, tmp_path, dead_port, name, payload):
        finished, elapsed = run_hook(payload, tmp_path, url=dead_port)
        assert_silent_success(finished, name)
        assert elapsed < 12, f"{name}: took {elapsed:.1f}s"

    def test_a_traversal_session_id_stays_inside_the_state_directory(self, tmp_path, dead_port):
        run_hook(
            {"session_id": "../../../../tmp/escaped", "cwd": "/tmp", "prompt": "вопрос про кеш"},
            tmp_path, url=dead_port,
        )
        assert not Path("/tmp/escaped.json").exists()
        stray = [p for p in tmp_path.rglob("*.json") if "local-optimize" not in str(p)]
        assert not stray, f"wrote state outside its own directory: {stray}"


class TestTheStateFileIsBroken:
    def state_file(self, tmp_path, session="abcd-1234"):
        directory = tmp_path / ".local/state/local-optimize/hooks"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{session}.json"

    @pytest.mark.parametrize(
        "content", [b"", b"{ truncated", b"[]", b'{"hashes": "not a list"}', b"\x00\x01\x02"]
    )
    def test_garbage_is_survivable(self, tmp_path, dead_port, content):
        self.state_file(tmp_path).write_bytes(content)
        finished, _ = run_hook(VALID_PAYLOAD, tmp_path, url=dead_port)
        assert_silent_success(finished, f"state file {content[:12]!r}")

    def test_an_unwritable_state_directory_is_survivable(self, tmp_path, dead_port):
        directory = tmp_path / ".local/state/local-optimize/hooks"
        directory.mkdir(parents=True)
        directory.chmod(0o500)
        try:
            finished, _ = run_hook(VALID_PAYLOAD, tmp_path, url=dead_port)
            assert_silent_success(finished, "read-only state directory")
        finally:
            directory.chmod(0o700)

    def test_a_file_where_the_directory_should_be(self, tmp_path, dead_port):
        parent = tmp_path / ".local/state/local-optimize"
        parent.mkdir(parents=True)
        (parent / "hooks").write_text("not a directory")
        finished, _ = run_hook(VALID_PAYLOAD, tmp_path, url=dead_port)
        assert_silent_success(finished, "state path is a file")


class TestConfiguration:
    @pytest.mark.parametrize(
        "env",
        [
            {"RAG_HOOK_TIMEOUT": "not-a-number"},
            {"RAG_HOOK_LIMIT": "-5"},
            {"RAG_HOOK_MAX_CHARS": ""},
            {"RAG_HOOK_URL": "not a url"},
            {"RAG_HOOK_URL": "ftp://example.com"},
            {"RAG_HOOK_TIMEOUT": "0"},
        ],
    )
    def test_nonsense_settings_disable_rather_than_crash(self, tmp_path, dead_port, env):
        finished, elapsed = run_hook(VALID_PAYLOAD, tmp_path, url=dead_port, env=env)
        assert_silent_success(finished, f"env {env}")
        assert elapsed < 12

    def test_the_off_switch_reaches_the_server_not_at_all(self, tmp_path):
        """RAG_HOOK_DISABLE=1 must short-circuit before any request."""
        server = serve(Behaviour("ok", body=b'{"injected":true,"context":"x"}'))
        try:
            finished, _ = run_hook(
                VALID_PAYLOAD, tmp_path,
                url=f"http://127.0.0.1:{server.server_address[1]}",
                env={"RAG_HOOK_DISABLE": "1"},
            )
            assert finished.returncode == 0
            assert finished.stdout.decode().strip() == ""
        finally:
            server.shutdown()


class TestCloudHookPrivacyBoundary:
    def test_good_response_is_never_injected_into_cloud_hook(self, tmp_path):
        body = json.dumps(
            {
                "status": "success",
                "injected": True,
                "text": "<local-rag-memory>\nФАКТ: кеш инвалидируется по поколениям.\n</local-rag-memory>",
                "hashes": ["deadbeef"],
                "chars": 42,
                "top_score": 0.74,
            }
        ).encode()
        server = serve(Behaviour("ok", body=body))
        try:
            finished, _ = run_hook(
                VALID_PAYLOAD, tmp_path,
                url=f"http://127.0.0.1:{server.server_address[1]}",
            )
            assert finished.returncode == 0
            # Cloud-capable editor hooks are deliberately inert even when
            # a backend could return useful RAG data.
            assert finished.stdout.strip() == b""
            assert not (tmp_path / ".local/state/local-optimize/hooks/abcd-1234.json").exists()

        finally:
            server.shutdown()

    def test_nothing_relevant_means_nothing_printed(self, tmp_path):
        """Below the relevance floor the server says so, and the hook stays quiet."""
        server = serve(
            Behaviour(
                "ok",
                body=b'{"status":"success","injected":false,'
                     b'"reason":"below_relevance_floor","top_score":0.11,'
                     b'"text":"","hashes":[]}',
            )
        )
        try:
            finished, _ = run_hook(
                VALID_PAYLOAD, tmp_path, url=f"http://127.0.0.1:{server.server_address[1]}"
            )
            assert finished.returncode == 0
            assert finished.stdout.decode().strip() == ""
        finally:
            server.shutdown()


class TestTheSessionHook:
    @pytest.mark.parametrize("source", ["startup", "resume", "compact", "clear", ""])
    def test_every_start_reason_exits_clean(self, tmp_path, dead_port, source):
        finished, _ = run_hook(
            {"session_id": "s", "cwd": str(HOOKS.parent), "hook_event_name": "SessionStart",
             "source": source},
            tmp_path, url=dead_port, hook=SESSION_HOOK,
        )
        assert_silent_success(finished, f"SessionStart source={source!r}")


class TestTheHarvester:
    """The one hook that writes. Off unless asked, and silent when it cannot work.

    `rag_harvest.py` reads the transcript at the end of a turn, asks a *local*
    model for durable facts, and queues them for review rather than saving them.
    Two properties carry the safety of that: it does nothing at all unless
    explicitly enabled, and anything that looks like a credential never reaches
    the queue. The rest is fail-open like its siblings - a turn must not end
    slower or louder because a local model was unreachable.
    """

    def transcript(self, tmp_path, lines):
        path = tmp_path / "transcript.jsonl"
        path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
        return path

    def message(self, role, text):
        return {"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}}

    def run(self, tmp_path, *, transcript, ollama, env=None, session="harvest-1"):
        payload = {
            "session_id": session,
            "transcript_path": str(transcript),
            "cwd": str(HOOKS.parent),
            "hook_event_name": "Stop",
        }
        environment = {"RAG_HOOK_HARVEST": "1", "OLLAMA_HOST": ollama}
        environment.update(env or {})
        return run_hook(payload, tmp_path, url=ollama, env=environment, hook=HARVEST_HOOK, timeout=60)

    def queue(self, tmp_path):
        path = tmp_path / ".local/state/local-optimize/hooks/harvest-queue.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def responding(self, candidates):
        body = json.dumps({"message": {"content": json.dumps(candidates, ensure_ascii=False)}, "done": True, "done_reason": "stop"}).encode()
        return serve(Behaviour("ok", body=body))

    def test_it_does_nothing_unless_asked(self, tmp_path, dead_port):
        """Not enabled means not enabled: no model call, no queue, no output."""
        server = self.responding([{"text": "x" * 60, "why": "reason"}])
        try:
            transcript = self.transcript(tmp_path, [self.message("user", "что-то важное")])
            finished, _ = self.run(
                tmp_path, transcript=transcript,
                ollama=f"http://127.0.0.1:{server.server_address[1]}",
                env={"RAG_HOOK_HARVEST": "0"},
            )
            assert finished.returncode == 0
            assert finished.stdout.decode().strip() == ""
            assert self.queue(tmp_path) == []
        finally:
            server.shutdown()

    def test_the_global_off_switch_outranks_the_harvest_switch(self, tmp_path):
        server = self.responding([{"text": "x" * 60, "why": "reason"}])
        try:
            transcript = self.transcript(tmp_path, [self.message("user", "что-то важное")])
            finished, _ = self.run(
                tmp_path, transcript=transcript,
                ollama=f"http://127.0.0.1:{server.server_address[1]}",
                env={"RAG_HOOK_DISABLE": "1"},
            )
            assert finished.returncode == 0
            assert self.queue(tmp_path) == []
        finally:
            server.shutdown()

    def test_a_credential_never_reaches_the_queue(self, tmp_path):
        """The model is local, the queue is a file, and neither is a reason to relax."""
        server = self.responding([
            {"text": "The deploy password is hunter2 and it lives in the vault." + "." * 20,
             "why": "operational"},
            {"text": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345 opens staging." + "." * 10,
             "why": "operational"},
            {"text": "Инвалидация кеша выполняется счётчиком поколений на проект, "
                     "а не удалением строк, потому что удаление гоняется с читателями.",
             "why": "architecture"},
        ])
        try:
            transcript = self.transcript(tmp_path, [self.message("user", "обсуждение доступа и кеша")])
            finished, _ = self.run(
                tmp_path, transcript=transcript,
                ollama=f"http://127.0.0.1:{server.server_address[1]}",
            )
            assert finished.returncode == 0
            queued = self.queue(tmp_path)
            assert len(queued) == 1, f"expected only the architecture note, got {queued}"
            assert "поколений" in queued[0]["text"]
            body = json.dumps(queued, ensure_ascii=False)
            assert "hunter2" not in body and "Bearer" not in body
        finally:
            server.shutdown()

    def test_trivia_is_dropped(self, tmp_path):
        """A fact too short to be a fact is noise the review queue does not need."""
        server = self.responding([{"text": "it works", "why": "short"}])
        try:
            transcript = self.transcript(tmp_path, [self.message("user", "обсуждение")])
            self.run(tmp_path, transcript=transcript,
                     ollama=f"http://127.0.0.1:{server.server_address[1]}")
            assert self.queue(tmp_path) == []
        finally:
            server.shutdown()

    def test_the_interval_holds_within_a_session(self, tmp_path):
        server = self.responding([
            {"text": "Инвалидация кеша выполняется счётчиком поколений на проект, "
                     "а не удалением строк.", "why": "architecture"}
        ])
        try:
            transcript = self.transcript(tmp_path, [self.message("user", "обсуждение кеша")])
            url = f"http://127.0.0.1:{server.server_address[1]}"
            self.run(tmp_path, transcript=transcript, ollama=url)
            first = len(self.queue(tmp_path))
            self.run(tmp_path, transcript=transcript, ollama=url)
            assert len(self.queue(tmp_path)) == first, (
                "Stop fires on every turn; harvesting on every turn would queue "
                "the same conversation over and over"
            )
        finally:
            server.shutdown()

    @pytest.mark.parametrize(
        "name,candidates",
        [
            ("not a list", '{"response": "{\\"text\\": \\"x\\"}"}'),
            ("prose instead of json", '{"response": "I could not find any durable facts."}'),
            ("truncated array", '{"response": "[{\\"text\\": \\"abc"}'),
            ("empty array", '{"response": "[]"}'),
        ],
    )
    def test_a_model_that_does_not_answer_in_json_queues_nothing(self, tmp_path, name, candidates):
        server = serve(Behaviour("ok", body=candidates.encode()))
        try:
            transcript = self.transcript(tmp_path, [self.message("user", "обсуждение")])
            finished, _ = self.run(
                tmp_path, transcript=transcript,
                ollama=f"http://127.0.0.1:{server.server_address[1]}",
            )
            assert finished.returncode == 0, name
            assert self.queue(tmp_path) == [], name
        finally:
            server.shutdown()

    def test_an_unreachable_model_ends_the_turn_anyway(self, tmp_path, dead_port):
        transcript = self.transcript(tmp_path, [self.message("user", "обсуждение")])
        finished, elapsed = self.run(tmp_path, transcript=transcript, ollama=dead_port)
        assert finished.returncode == 0
        assert self.queue(tmp_path) == []
        assert elapsed < 20

    @pytest.mark.parametrize(
        "name,lines",
        [
            ("empty file", []),
            ("not jsonl", None),
            ("json but wrong shape", [{"unexpected": True}]),
            ("content is not a list", [{"type": "user", "message": {"content": 42}}]),
        ],
    )
    def test_an_unusable_transcript_queues_nothing(self, tmp_path, dead_port, name, lines):
        if lines is None:
            path = tmp_path / "transcript.jsonl"
            path.write_text("this is not jsonl at all\n{{{", encoding="utf-8")
        else:
            path = self.transcript(tmp_path, lines)
        finished, _ = self.run(tmp_path, transcript=path, ollama=dead_port)
        assert finished.returncode == 0, name
        assert self.queue(tmp_path) == [], name

    def test_a_missing_transcript_is_not_an_error(self, tmp_path, dead_port):
        finished, _ = self.run(
            tmp_path, transcript=tmp_path / "does-not-exist.jsonl", ollama=dead_port
        )
        assert finished.returncode == 0
        assert self.queue(tmp_path) == []

    def test_harvest_queue_and_state_dir_enforce_private_permissions(self, tmp_path):
        server = self.responding([
            {
                "text": "Инвалидация кеша выполняется счётчиком поколений на проект, а не удалением строк.",
                "why": "architecture",
            }
        ])
        try:
            transcript = self.transcript(tmp_path, [self.message("user", "обсуждение кеша")])
            finished, _ = self.run(
                tmp_path,
                transcript=transcript,
                ollama=f"http://127.0.0.1:{server.server_address[1]}",
            )
            assert finished.returncode == 0
            state_dir = tmp_path / ".local/state/local-optimize/hooks"
            queue_path = state_dir / "harvest-queue.jsonl"
            assert queue_path.exists()
            assert (state_dir.stat().st_mode & 0o777) == 0o700
            assert (queue_path.stat().st_mode & 0o777) == 0o600
        finally:
            server.shutdown()

    def test_query_local_model_uses_safe_parser_on_malformed_output(self, monkeypatch):
        import hooks.rag_harvest as rag_harvest
        import fast_router

        monkeypatch.setattr(fast_router, "query_local_ollama", lambda *_args, **_kwargs: "[broken json")
        assert rag_harvest.query_local_model("prompt") == []
