#!/usr/bin/env python3
"""Dynamic multi-root watchdog delegating indexing to the central RAG server."""

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

from indexer_queue import IndexQueue
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from config import settings
from project_context import get_project_registry
from rag_client import RagClient

BASE_DIR = settings.project_root
RAG_SERVER_URL = settings.rag_server_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [watchdog-indexer] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("watchdog_indexer")

CODE_EXTENSIONS = {".py"}
DOC_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".sql", ".sh"}
VALID_EXTENSIONS = CODE_EXTENSIONS | DOC_EXTENSIONS
IGNORE_DIRS = {
    "__pycache__", ".git", "venv", ".venv", "rag-venv", "node_modules", "brain", "logs",
    "artifacts", "evaluation", "backup", "backups", "models", "refactored-agent-skills",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
}
BINARY_EXTENSIONS = {".pyc", ".so", ".o", ".zip", ".tar", ".gz", ".png", ".jpg", ".db", ".onnx"}
DEBOUNCE_DELAY = 1.0
MAX_PENDING = 500


def get_max_file_size() -> int:
    return settings.max_file_size_bytes


def build_watchdog_context(*, project_id: str = "", root_id: str = "", working_directory: str = ""):
    if not working_directory and (not project_id or not root_id):
        raise ValueError("Watchdog requires every explicit registered project/root")
    return get_project_registry(settings).resolve(
        project_id=project_id or None,
        root_id=root_id or None,
        working_directory=working_directory or None,
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
    def __init__(self, client: Optional[RagClient] = None, context=None, registry=None):
        if context is None:
            raise ValueError("DebouncedIndexer requires an explicit registered context")
        self.context = context
        self.registry = registry or get_project_registry(settings)
        self.client = client or RagClient(self.context, server_url=RAG_SERVER_URL)
        self.queue = IndexQueue(
            self.context.project_root,
            self._process_now,
            self._indexable_relative,
            capacity=MAX_PENDING,
            debounce=DEBOUNCE_DELAY,
        )

    def _indexable_relative(self, relative: Path) -> bool:
        if not is_indexable_path(relative):
            return False
        candidate = Path(self.context.project_root) / relative
        return self.registry.is_owned_by(
            candidate,
            project_id=self.context.project_id,
            root_id=self.context.root_id,
        )

    def trigger(self, action: str, file_path: str, dest_path: Optional[str] = None):
        if action == "move" and dest_path and not self.queue._allowed(file_path):
            return self.queue.enqueue("modify", dest_path)
        return self.queue.enqueue(action, file_path, dest_path)

    def _is_allowed_path(self, path: Path) -> bool:
        try:
            if len(str(path)) > 4096:
                return False
            self.context.validate_path(path, must_exist=path.exists())
            return self.registry.is_owned_by(
                path,
                project_id=self.context.project_id,
                root_id=self.context.root_id,
            )
        except (OSError, RuntimeError, ValueError):
            return False

    def _process_now(self, action: str, file_path: str, dest_path: Optional[str] = None):
        try:
            path = Path(file_path)
            if path.resolve() != path:
                return False
            source_allowed = self._is_allowed_path(path)
            max_size = get_max_file_size()
            if action in ("create", "modify"):
                if not source_allowed or not path.exists() or not path.is_file():
                    return False
                if path.stat().st_size > max_size:
                    return False
                if not is_indexable_path(path.relative_to(self.context.project_root)):
                    return False
                if path.suffix.lower() in CODE_EXTENSIONS:
                    result = self.client.index_code(str(path), context=self.context)
                elif path.suffix.lower() in DOC_EXTENSIONS:
                    result = self.client.index_document(str(path), context=self.context)
                else:
                    return False
                return result.get("status") in ("success", "ok", "fallback_success")
            if action == "delete":
                if not source_allowed:
                    return False
                if path.exists():
                    return self._process_now("modify", str(path))
                result = self.client.delete_document(str(path), context=self.context)
                return result.get("status") == "success"
            if action == "move" and dest_path:
                destination = Path(dest_path)
                if destination.resolve() != destination:
                    return False
                destination_allowed = self._is_allowed_path(destination)
                destination_indexable = (
                    destination_allowed
                    and is_indexable_path(destination.relative_to(self.context.project_root))
                    and destination.exists()
                    and destination.is_file()
                    and destination.stat().st_size <= max_size
                )
                if source_allowed and destination_indexable:
                    result = self.client.move_document(str(path), str(destination), context=self.context)
                    return result.get("status") == "success" and result.get("moved_count", 0) > 0
                if source_allowed:
                    result = self.client.delete_document(str(path), context=self.context)
                    return result.get("status") == "success"
                return False
        except Exception as exc:
            logger.warning("Watchdog processing error action=%s type=%s", action, type(exc).__name__)
        return False


class CodebaseChangeHandler(FileSystemEventHandler):
    def __init__(self, indexer: DebouncedIndexer):
        super().__init__()
        self.indexer = indexer

    def _should_handle(self, path_str: str) -> bool:
        path = Path(path_str)
        try:
            relative = path.relative_to(Path(self.indexer.context.project_root))
        except ValueError:
            return False
        return is_indexable_path(relative) and self.indexer._is_allowed_path(path)

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


class WatchdogSupervisor:
    def __init__(self, *, reconcile_interval: float = 300.0):
        self.reconcile_interval = reconcile_interval
        self.observer = Observer()
        self.indexers: dict[tuple[str, str], DebouncedIndexer] = {}
        self.watches: dict[tuple[str, str], object] = {}
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._started = False

    def _remove(self, key: tuple[str, str]) -> None:
        watch = self.watches.pop(key, None)
        if watch is not None:
            try:
                self.observer.unschedule(watch)
            except (KeyError, ValueError):
                pass
        indexer = self.indexers.pop(key, None)
        if indexer is not None:
            indexer.queue.close()

    def reconcile_registry(self) -> None:
        registry = get_project_registry(settings)
        desired = {}
        for project in registry.projects:
            for root_id in project.roots:
                key = (project.project_id, root_id)
                desired[key] = registry.resolve(
                    project_id=project.project_id,
                    root_id=root_id,
                    agent_id="watchdog-indexer",
                    session_id="watchdog-service",
                    write_visibility=os.getenv("INDEX_VISIBILITY", "shared"),
                    read_scope="shared",
                    validation_status="unverified",
                )
        for key in list(self.indexers):
            if key not in desired or self.indexers[key].context.project_root != desired[key].project_root:
                self._remove(key)
        for key, context in desired.items():
            if key in self.indexers:
                indexer = self.indexers[key]
                indexer.registry = registry
                indexer.context = context
                indexer.client.context = context
                indexer.queue.reconcile()
                continue
            indexer = DebouncedIndexer(context=context, registry=registry)
            self.indexers[key] = indexer
            self.watches[key] = self.observer.schedule(
                CodebaseChangeHandler(indexer), path=context.project_root, recursive=True,
            )
            indexer.queue.reconcile()

    def _work(self) -> None:
        while not self._stop.is_set():
            processed = False
            for indexer in list(self.indexers.values()):
                try:
                    processed = indexer.queue.process_next() or processed
                except Exception as exc:
                    logger.warning("Root worker isolated failure type=%s", type(exc).__name__)
            if not processed:
                self._stop.wait(0.1)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.observer.start()
        self.reconcile_registry()
        self._worker = threading.Thread(target=self._work, name="rag-multi-root-index-worker", daemon=True)
        self._worker.start()

    def run(self) -> None:
        self.start()
        try:
            while not self._stop.wait(self.reconcile_interval):
                self.reconcile_registry()
        finally:
            self.close()

    def close(self) -> None:
        self._stop.set()
        if self._started:
            self.observer.stop()
        for key in list(self.indexers):
            self._remove(key)
        if self._worker is not None:
            self._worker.join(timeout=10)
        if self._started and self.observer.is_alive():
            self.observer.join(timeout=10)
        self._started = False


def start_watcher():
    interval = float(os.getenv("WATCHDOG_RECONCILE_SECONDS", "300"))
    if interval < 1:
        raise ValueError("WATCHDOG_RECONCILE_SECONDS must be at least 1")
    WatchdogSupervisor(reconcile_interval=interval).run()


if __name__ == "__main__":
    start_watcher()
