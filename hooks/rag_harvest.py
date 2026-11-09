#!/usr/bin/env python3
"""Post-turn knowledge harvester for Claude Code (event: Stop).

This hook inspects the recent conversation transcript at the end of a turn and
uses a small local model to propose durable architectural decisions, project
conventions, or operational facts for long-term memory.

Safety & review design:
1. Opt-in only: RAG_HOOK_HARVEST=1 must be set explicitly. Automatic memory
   extraction without human oversight quickly pollutes a shared knowledge store
   with transient debugging trivia.
2. Propose, don't commit: extracted candidates are written to an append-only
   local review queue (~/.local/state/local-optimize/hooks/harvest-queue.jsonl)
   instead of directly mutating the vector store. The hook proposes; a human
   or a secondary verification pipeline disposes.
3. Throttled: Stop fires at the end of every turn, and running an extraction
   model on every turn spends real capacity to re-read a conversation that has
   barely moved. RAG_HOOK_HARVEST_INTERVAL (default 900s) is the floor between
   two harvests of the same session.

Harvesting is local-only. The transcript can contain sensitive RAG results, so
it must never be sent to a cloud extraction backend. Unknown backend values fail
closed and produce no candidates.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

SECRET_PATTERN = re.compile(
    r"password|passwd|secret|token|api[_-]?key|BEGIN [A-Z ]*PRIVATE KEY|Bearer [A-Za-z0-9._-]{16,}",
    re.IGNORECASE,
)
MAX_TRANSCRIPT_BYTES = 120 * 1024
HARVEST_PROMPT_TEMPLATE = """Извлеки долговременные факты из рабочего диалога. / Extract durable facts from a work conversation.

Долговременный факт — это решение, соглашение или причина, которая останется верной и через неделю: почему сделано именно так, какое правило принято, какой инвариант нужно соблюдать. НЕ факты: временные ошибки, шаги отладки, содержимое файлов, планы на ближайшую минуту.

Пример входа:
«— Почему таймаут 30с, а не 5? — Потому что реиндексация большого файла занимает до 20с, а при 5с хук отваливался и индекс расходился с диском.»
Пример выхода:
[{"text":"Таймаут индексации выставлен в 30 секунд: реиндексация крупного файла занимает до 20 секунд, и при 5 секундах хук отваливался, оставляя индекс рассинхронизированным с диском.","why":"Объясняет выбор константы, останется верным до смены индексатора."}]

Верни ТОЛЬКО JSON-массив, максимум 3 объекта, на языке исходного диалога. Если долговременных фактов нет — верни [].

Диалог:
%s"""


def debug(msg: str) -> None:
    if os.getenv("RAG_HOOK_DEBUG") == "1":
        sys.stderr.write(f"[rag_harvest] {msg}\n")


def extract_recent_text(transcript_path: str, max_messages: int = 12) -> str:
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""
    file_size = os.path.getsize(transcript_path)
    with open(transcript_path, "rb") as f:
        if file_size > MAX_TRANSCRIPT_BYTES:
            f.seek(file_size - MAX_TRANSCRIPT_BYTES)
        raw_data = f.read()

    lines = raw_data.decode("utf-8", errors="ignore").splitlines()
    messages: list[str] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue

        text = ""
        if isinstance(entry, dict):
            if "message" in entry and isinstance(entry["message"], dict):
                content = entry["message"].get("content", "")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict) and "text" in c
                    )
            elif "content" in entry and isinstance(entry["content"], str):
                text = entry["content"]
            elif "text" in entry and isinstance(entry["text"], str):
                text = entry["text"]

        text = text.strip()
        if text:
            messages.append(text[:2000])
            if len(messages) >= max_messages:
                break

    messages.reverse()
    return "\n---\n".join(messages)


def _parse_candidates(raw_out: str) -> list[dict[str, Any]]:
    """Recover the JSON array from whatever the model decided to wrap it in.

    Small models fence their output, apologize before it, and explain after it.
    The first '[' to the last ']' is the only slice worth trusting, and anything
    that does not parse means "nothing to save" - never an error, because this
    hook must not turn a bad generation into a broken editor.
    """
    start_idx = raw_out.find("[")
    end_idx = raw_out.rfind("]")
    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        return []
    try:
        parsed = json.loads(raw_out[start_idx : end_idx + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def query_backend(prompt: str) -> list[dict[str, Any]]:
    backend = os.getenv("RAG_HOOK_HARVEST_BACKEND", "ollama").strip().lower()
    if backend not in {"ollama", "local"}:
        debug(f"blocked non-local harvest backend {backend!r}; RAG data is never sent to cloud")
        return []
    try:
        return query_local_model(prompt)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        debug(f"local harvest backend unavailable: {exc}")
        return []


def query_local_model(prompt: str) -> list[dict[str, Any]]:
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from fast_router import query_local_ollama
    try:
        raw_out = query_local_ollama(prompt, timeout=20, num_predict=256)
    except (RuntimeError, TimeoutError, ValueError):
        return []

    start_idx = raw_out.find("[")
    end_idx = raw_out.rfind("]")
    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        return []

    parsed = json.loads(raw_out[start_idx : end_idx + 1])
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _harvest_is_due(session_id: str) -> bool:
    """Rate-limit a hook that fires far more often than it has work to do.

    Stop runs at the end of every turn. Extraction reads a slice of transcript
    that has barely changed since the last turn, so running it every time buys
    almost nothing and costs a model call each turn. The stamp lives beside the
    other hook state and a failure to read or write it means "go ahead" - a
    throttle that breaks should degrade into doing the work, not into silence.
    """
    try:
        interval = float(os.getenv("RAG_HOOK_HARVEST_INTERVAL", "900"))
    except ValueError:
        interval = 900.0
    if interval <= 0:
        return True
    safe = "".join(ch for ch in str(session_id) if ch.isalnum() or ch in "_-")[:64]
    stamp = os.path.join(
        os.path.expanduser("~/.local/state/local-optimize/hooks"),
        f"harvest-{safe or 'default'}.stamp",
    )
    now = time.time()
    try:
        if os.path.isfile(stamp) and now - os.path.getmtime(stamp) < interval:
            return False
    except OSError:
        return True
    try:
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as handle:
            handle.write(str(int(now)))
    except OSError:
        pass
    return True


def main() -> None:
    if os.getenv("RAG_HOOK_HARVEST") != "1" or os.getenv("RAG_HOOK_DISABLE") == "1":
        sys.exit(0)

    try:
        raw_event = sys.stdin.read()
        if not raw_event.strip():
            sys.exit(0)
        event = json.loads(raw_event)
    except Exception:
        sys.exit(0)

    if not _harvest_is_due(event.get("session_id", "")):
        debug("harvest skipped: interval has not elapsed for this session")
        sys.exit(0)

    transcript_path = event.get("transcript_path", "")
    cwd = event.get("cwd", "")
    session_id = event.get("session_id", "")

    recent_text = extract_recent_text(transcript_path)
    if not recent_text:
        sys.exit(0)

    try:
        candidates = query_backend(HARVEST_PROMPT_TEMPLATE % recent_text)
    except Exception as e:
        debug(f"Local model inference failed: {e}")
        sys.exit(0)

    valid_candidates = []
    for cand in candidates:
        text = str(cand.get("text", "")).strip()
        why = str(cand.get("why", "")).strip()
        if len(text) < 40 or SECRET_PATTERN.search(text):
            continue
        valid_candidates.append({
            "text": text,
            "why": why,
            "cwd": cwd,
            "session_id": session_id,
            "ts": int(time.time()),
        })

    if not valid_candidates:
        sys.exit(0)

    try:
        state_dir = os.path.expanduser("~/.local/state/local-optimize/hooks")
        os.makedirs(state_dir, exist_ok=True)
        queue_path = os.path.join(state_dir, "harvest-queue.jsonl")
        with open(queue_path, "a", encoding="utf-8") as f:
            for item in valid_candidates:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception as e:
        debug(f"Failed to append to harvest queue: {e}")

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        debug(f"Unhandled hook failure: {exc}")
        sys.exit(0)
