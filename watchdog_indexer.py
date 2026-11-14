#!/usr/bin/env python3
"""Dynamic multi-root watchdog delegating indexing to the central RAG server."""

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

from indexer_queue import CODE_EXTENSIONS, DOCUMENT_EXTENSIONS, IGNORED_DIRS, IndexQueue
from watchdog.events import (
    FileClosedEvent, FileCreatedEvent, FileDeletedEvent, FileModifiedEvent,
    FileMovedEvent, FileSystemEventHandler,
)
from watchdog.observers import Observer

from config import settings
from project_context import get_project_registry, workspace_definition
from rag_client import RagClient

BASE_DIR = settings.project_root
RAG_SERVER_URL = settings.rag_server_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [watchdog-indexer] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("watchdog_indexer")

DOC_EXTENSIONS = DOCUMENT_EXTENSIONS
VALID_EXTENSIONS = CODE_EXTENSIONS | DOC_EXTENSIONS
IGNORE_DIRS = IGNORED_DIRS
BINARY_EXTENSIONS = {".pyc", ".so", ".o", ".zip", ".tar", ".gz", ".png", ".jpg", ".db", ".onnx"}
DEBOUNCE_DELAY = 1.0
MAX_PENDING = 500
MAX_WATCH_ROOTS = 64
WATCH_EVENTS = [FileClosedEvent, FileCreatedEvent, FileDeletedEvent, FileModifiedEvent, FileMovedEvent]


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
        return self.registry.owns_context_path(self.context, candidate)

    def trigger(self, action: str, file_path: str, dest_path: Optional[str] = None):
        if action == "move" and dest_path and not self.queue._allowed(file_path):
            return self.queue.enqueue("modify", dest_path)
        return self.queue.enqueue(action, file_path, dest_path)

    def _is_allowed_path(self, path: Path) -> bool:
        try:
            if len(str(path)) > 4096:
                return False
            self.context.validate_path(path, must_exist=path.exists())
            return self.registry.owns_context_path(self.context, path)
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
                return (
                    result.get("status") in ("success", "ok", "fallback_success")
                    and result.get("completed", True)
                )
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
                    if result.get("status") == "success" and result.get("moved_count", 0) > 0:
                        return True
                    error_code = (result.get("error") or {}).get("code") if isinstance(result.get("error"), dict) else None
                    if (
                        result.get("status") == "not_found"
                        or error_code == "MOVE_NOT_APPLIED"
                        or result.get("message") == "HTTP 409"
                    ):
                        return self._process_now("modify", str(destination))
                    return False
                if source_allowed:
                    result = self.client.delete_document(str(path), context=self.context)
                    return result.get("status") == "success"
                if destination_indexable:
                    return self._process_now("modify", str(destination))
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
    def __init__(self, *, reconcile_interval: float = 300.0, watch_paths=(), subdir_watches=()):
        self.reconcile_interval = reconcile_interval
        paths = sorted({workspace_definition(path).roots["workspace"] for path in watch_paths})
        if len(paths) > MAX_WATCH_ROOTS:
            logger.warning("Only %d watch roots are allowed; remaining paths are not watched", MAX_WATCH_ROOTS)
        self.watch_paths = set(paths[:MAX_WATCH_ROOTS])
        raw_subdirs = subdir_watches or [
            part.strip() for part in os.getenv("WATCHDOG_SUBDIRS", "").split(os.pathsep) if part.strip()
        ]
        self.subdir_watches: tuple[str, ...] = tuple(raw_subdirs)
        self.observer = Observer()
        self.indexers: dict[tuple[str, str], DebouncedIndexer] = {}
        self.watches: dict[tuple[str, str], object] = {}
        self._subdir_watch_handles: dict[tuple[str, str], list[object]] = {}
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
        for subwatch in self._subdir_watch_handles.pop(key, ()):
            try:
                self.observer.unschedule(subwatch)
            except (KeyError, ValueError):
                pass
        indexer = self.indexers.pop(key, None)
        if indexer is not None:
            indexer.queue.close()

    def reconcile_registry(self) -> None:
        registry = get_project_registry(settings)
        desired = {}
        for path in self.watch_paths:
            try:
                context = registry.resolve(
                    working_directory=str(path),
                    agent_id="watchdog-indexer",
                    session_id="watchdog-service",
                    write_visibility=os.getenv("INDEX_VISIBILITY", "shared"),
                    read_scope="shared",
                    validation_status="unverified",
                )
                if Path(context.project_root) != path:
                    logger.warning("Watch path is inside a registered root; select the registered root explicitly: %s", path)
                    continue
                desired[(context.project_id, context.root_id)] = context
            except (OSError, ValueError) as exc:
                logger.warning("Watch path is unavailable or rejected: %s (%s)", path, type(exc).__name__)
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
            handler = CodebaseChangeHandler(indexer)
            scheduled_subdirs: list[object] = []
            if self.subdir_watches:
                root_path = Path(context.project_root)
                for rel_sub in self.subdir_watches:
                    if ".." in Path(rel_sub).parts:
                        continue
                    raw_candidate = root_path / rel_sub if rel_sub != "." else root_path
                    cur_check = raw_candidate
                    has_symlink = False
                    while cur_check != root_path and cur_check != cur_check.parent:
                        if cur_check.is_symlink():
                            has_symlink = True
                            break
                        cur_check = cur_check.parent
                    if has_symlink:
                        continue
                    candidate = raw_candidate.resolve()
                    if not candidate.exists() or not candidate.is_dir():
                        continue
                    try:
                        rel_path = candidate.relative_to(root_path)
                    except ValueError:
                        continue
                    if rel_path != Path(".") and (
                        any(part in IGNORE_DIRS or part.startswith(".") for part in rel_path.parts)
                        or not registry.owns_context_path(context, candidate)
                    ):
                        continue
                    scheduled_subdirs.append(
                        self.observer.schedule(
                            handler,
                            path=str(candidate),
                            recursive=False,
                            event_filter=WATCH_EVENTS,
                        )
                    )
            if scheduled_subdirs:
                self._subdir_watch_handles[key] = scheduled_subdirs
                self.watches[key] = scheduled_subdirs[0]
            else:
                self.watches[key] = self.observer.schedule(
                    handler,
                    path=context.project_root,
                    recursive=False,
                    event_filter=WATCH_EVENTS,
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
    watch_paths = [path for path in os.getenv("WATCHDOG_PATHS", "").split(os.pathsep) if path]
    WatchdogSupervisor(reconcile_interval=interval, watch_paths=watch_paths).run()


if __name__ == "__main__":
    start_watcher()
