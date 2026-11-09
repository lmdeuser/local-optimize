#!/usr/bin/env python3
"""
Lightweight Watchdog Auto-Indexer Daemon for RAG Infrastructure.
Monitors filesystem events and delegates indexing to the central RAG HTTP Server
via RagClient without instantiating heavy Intel XPU embedding models locally.
"""

import os
import sys
import time
import logging
from pathlib import Path
from indexer_queue import IndexQueue
from typing import Optional
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

try:
    from config import settings
    BASE_DIR = settings.project_root
    RAG_SERVER_URL = settings.rag_server_url
except ImportError:
    BASE_DIR = Path(__file__).resolve().parent
    RAG_SERVER_URL = "http://127.0.0.1:8181"

sys.path.insert(0, str(BASE_DIR))
from rag_client import RagClient
from project_context import build_default_project_context

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [watchdog-indexer] %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("watchdog_indexer")

CODE_EXTENSIONS = {".py"}
DOC_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".sql", ".sh"}
VALID_EXTENSIONS = CODE_EXTENSIONS | DOC_EXTENSIONS

IGNORE_DIRS = {
    "__pycache__", ".git", "venv", ".venv", "rag-venv", "node_modules", "brain", "logs",
    "artifacts", "evaluation", "backup", "backups", "models", "refactored-agent-skills",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build"
}
BINARY_EXTENSIONS = {".pyc", ".so", ".o", ".zip", ".tar", ".gz", ".png", ".jpg", ".db", ".onnx"}

DEBOUNCE_DELAY = 1.0  # seconds
MAX_PENDING = 500


def get_max_file_size() -> int:
    return getattr(settings, "max_file_size_bytes", 10_000_000)


def build_watchdog_context():
    # The indexer is shared infrastructure, not a conversational agent: anything it
    # indexes must be readable by every agent connecting over MCP. Defaulting to
    # session-private scoped its rows to this service's own agent/session pair, so no
    # other client could ever read them. Override only for deliberate isolation.
    return build_default_project_context(
        settings,
        agent_id="watchdog-indexer",
        session_id="watchdog-service",
        write_visibility=os.getenv("INDEX_VISIBILITY", "shared"),
        read_scope="shared",
        validation_status="unverified",
    )


def is_indexable_path(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS or path.suffix.lower() not in VALID_EXTENSIONS:
        return False
    return not any(part in IGNORE_DIRS or part.startswith(".") for part in path.parts)


class DebouncedIndexer:
    def __init__(self, client: Optional[RagClient] = None, context=None):
        self.context = context or build_watchdog_context()
        self.client = client or RagClient(self.context, server_url=RAG_SERVER_URL)
        self.queue = IndexQueue(self.context.project_root, self._process_now,
                                is_indexable_path, capacity=MAX_PENDING, debounce=DEBOUNCE_DELAY)

    def trigger(self, action: str, file_path: str, dest_path: Optional[str] = None):
        if action == "move" and dest_path and not self.queue._allowed(file_path):
            return self.queue.enqueue("modify", dest_path)
        return self.queue.enqueue(action, file_path, dest_path)

    def _is_allowed_path(self, path: Path) -> bool:
        try:
            path_str = str(path)
            if len(path_str) > 4096:
                logger.warning("Path length exceeds 4096 characters: %s", path_str[:100])
                return False

            self.context.validate_path(path, must_exist=path.exists())
            return True
        except Exception:
            return False

    def _process_now(self, action: str, file_path: str, dest_path: Optional[str] = None):
        try:
            p = Path(file_path)
            if p.resolve() != p:
                return False
            src_allowed = self._is_allowed_path(p)
            max_size = get_max_file_size()

            if action in ("create", "modify"):
                if not src_allowed:
                    logger.warning("Ignoring create/modify outside allowed roots: %s", file_path)
                    return
                if not p.exists() or not p.is_file():
                    return

                # File size limit check
                if p.stat().st_size > max_size:
                    logger.warning("Skipping oversized file (%d bytes > %d): %s", p.stat().st_size, max_size, p)
                    return

                suffix = p.suffix.lower()
                if not is_indexable_path(p.relative_to(self.context.project_root)):
                    return
                if suffix in CODE_EXTENSIONS:
                    logger.info("Watchdog: Indexing code file %s via central RAG server", p)
                    res = self.client.index_code(str(p), context=self.context)
                elif suffix in DOC_EXTENSIONS:
                    logger.info("Watchdog: Indexing document file %s via central RAG server", p)
                    res = self.client.index_document(str(p), context=self.context)
                else:
                    return

                if res.get("status") in ("success", "ok"):
                    logger.info("Indexed %s -> status: %s", p, res.get("status"))
                    return True
                else:
                    logger.warning("Failed indexing %s -> status: %s", p, res.get("status"))

            elif action == "delete":
                if src_allowed:
                    if p.exists():
                        # A delayed delete event can arrive after recreation.
                        return self._process_now("modify", str(p))
                    logger.info("Watchdog: Deleting/deactivating removed file %s", p)
                    res = self.client.delete_document(str(p), context=self.context)
                    if res.get("status") != "success" or res.get("deleted_count", 0) < 1:
                        logger.warning("Delete document warning for %s", p)
                    return res.get("status") == "success"

            elif action == "move" and dest_path:
                dest_p = Path(dest_path)
                if dest_p.resolve() != dest_p:
                    return False
                dest_allowed = self._is_allowed_path(dest_p)

                logger.info("Watchdog: File moved from %s to %s", p, dest_p)

                destination_indexable = (
                    dest_allowed
                    and is_indexable_path(dest_p.relative_to(self.context.project_root))
                    and dest_p.exists()
                    and dest_p.is_file()
                    and dest_p.stat().st_size <= max_size
                )
                if src_allowed and destination_indexable:
                    result = self.client.move_document(
                        str(p), str(dest_p), context=self.context
                    )
                    if result.get("status") != "success" or result.get("moved_count", 0) < 1:
                        logger.warning("Atomic move was not applied for %s", p)
                    return result.get("status") == "success" and result.get("moved_count", 0) > 0
                elif src_allowed:
                    result = self.client.delete_document(str(p), context=self.context)
                    if result.get("status") != "success" or result.get("deleted_count", 0) < 1:
                        logger.warning("Move cleanup was not confirmed for %s", p)
                    return result.get("status") == "success"
                else:
                    logger.warning("Ignoring move source outside allowed roots: %s", file_path)

        except Exception as e:
            logger.warning("Watchdog processing error for %s (%s): %s", file_path, action, type(e).__name__)


class CodebaseChangeHandler(FileSystemEventHandler):
    def __init__(self, indexer: DebouncedIndexer):
        super().__init__()
        self.indexer = indexer

    def _should_handle(self, path_str: str) -> bool:
        try:
            relative = Path(path_str).relative_to(Path(self.indexer.context.project_root))
        except ValueError:
            return False
        return is_indexable_path(relative)

    def on_modified(self, event):
        if not event.is_directory and self._should_handle(event.src_path):
            self.indexer.trigger("modify", event.src_path)

    def on_created(self, event):
        if not event.is_directory and self._should_handle(event.src_path):
            self.indexer.trigger("create", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and self._should_handle(event.src_path):
            self.indexer.trigger("delete", event.src_path)

    def on_moved(self, event):
        if not event.is_directory and (
            self._should_handle(event.src_path) or self._should_handle(event.dest_path)
        ):
            self.indexer.trigger("move", event.src_path, dest_path=event.dest_path)


def start_watcher():
    logger.info("Initializing lightweight Watchdog Client (connecting to %s)...", RAG_SERVER_URL)
    context = build_watchdog_context()
    client = RagClient(context, server_url=RAG_SERVER_URL)
    indexer = DebouncedIndexer(client=client, context=context)
    event_handler = CodebaseChangeHandler(indexer)

    observer = Observer()
    observer.schedule(event_handler, path=context.project_root, recursive=True)
    observer.start()
    indexer.queue.start()
    indexer.queue.reconcile()
    logger.info("🚀 Lightweight Watchdog Auto-Indexer actively monitoring %s...", context.project_root)

    try:
        while True:
            time.sleep(300)
            indexer.queue.reconcile()
    except KeyboardInterrupt:
        observer.stop()
    finally:
        observer.stop()
        indexer.queue.close()
    observer.join()


if __name__ == "__main__":
    start_watcher()
