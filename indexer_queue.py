"""One bounded indexing worker with durable, success-only reconciliation state."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import time
from pathlib import Path
from threading import Condition, Thread

logger = logging.getLogger(__name__)
CODE_EXTENSIONS = frozenset({".py"})
DOCUMENT_EXTENSIONS = frozenset({".md", ".txt", ".jsonl", ".json", ".yaml", ".yml", ".sql", ".sh"})
IGNORED_DIRS = frozenset({
    "__pycache__", ".git", "venv", ".venv", "rag-venv", "node_modules", ".mimocode",
    "artifacts", "evaluation", "backup", "backups", "models", "refactored-agent-skills",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", "brain", "logs",
    ".agents", ".claude", ".cursor", ".vscode", "Scretch", "BlenderContent",
})


class IndexQueue:
    def __init__(self, root, process, indexable, *, capacity=500, debounce=1.0, scan_limit=10000):
        self.root = Path(root).resolve()
        self.process = process
        self.indexable = indexable
        self.capacity = capacity
        self.debounce = debounce
        if scan_limit < 1:
            raise ValueError("scan_limit must be positive")
        self.scan_limit = scan_limit
        self._scan = None
        self._scan_seen = set()
        self._scan_complete = True
        self.pending = {}
        self.condition = Condition()
        self.stopping = False
        self.worker = None
        state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))).expanduser()
        if not state_home.is_absolute():
            raise ValueError("XDG_STATE_HOME must be an absolute path")
        state_key = hashlib.sha256(os.fsencode(str(self.root))).hexdigest()
        self.state_path = state_home / "local-optimize" / "indexer" / f"{state_key}.json"
        if self.state_path.resolve().is_relative_to(self.root):
            raise ValueError("Indexer state must be outside the watched root")
        legacy_path = self.root / ".rag-index-state.json"
        read_path = self.state_path if self.state_path.exists() else legacy_path
        self.confirmed = {}
        if read_path.exists():
            if read_path.is_symlink() or not read_path.is_file():
                raise ValueError("Indexer state path must be a regular non-symlink file")
            if read_path.stat().st_size <= 2_000_000:
                try:
                    fd = os.open(read_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(fd, "r", encoding="utf-8") as stream:
                        data = json.load(stream)
                    if data.get("version") == 1 and data.get("root") == str(self.root):
                        self.confirmed = {key: value for key, value in data.get("files", {}).items()
                                          if self._allowed(key)}
                except (ValueError, OSError, TypeError, AttributeError):
                    self.confirmed = {}

    def _allowed(self, path):
        path = Path(path)
        try:
            relative = path.relative_to(self.root)
            return ".." not in path.parts and path.resolve() == path and self.indexable(relative)
        except (ValueError, OSError, RuntimeError):
            return False

    @staticmethod
    def fingerprint(path):
        try:
            stat = Path(path).stat()
            return [stat.st_mtime_ns, stat.st_size] if Path(path).is_file() else None
        except FileNotFoundError:
            return None

    def enqueue(self, action, path, destination=None, *, attempt=0):
        path = str(path)
        if not self._allowed(path):
            return False
        with self.condition:
            if attempt and path in self.pending:
                return True  # A newer filesystem event supersedes this retry.
            if self.stopping or (path not in self.pending and len(self.pending) >= self.capacity):
                return False  # Reconciliation finds work missed during a burst.
            self.pending[path] = (time.monotonic() + self.debounce * (2 ** attempt),
                                  action, destination, attempt)
            self.condition.notify()
        return True

    def start(self):
        if self.worker is None:
            self.worker = Thread(target=self._run, name="rag-index-worker", daemon=True)
            self.worker.start()

    def close(self):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        if self.worker is not None:
            self.worker.join(timeout=10)

    def _scan_paths(self):
        def scan_error(_error):
            self._scan_complete = False
        for directory, dirs, files in os.walk(self.root, followlinks=False, onerror=scan_error):
            yield None
            dirs[:] = [name for name in dirs if self.indexable(Path(directory).relative_to(self.root) / name / "probe.py")
                       and not (Path(directory) / name).is_symlink()]
            for name in files:
                path = str(Path(directory) / name)
                if not self._allowed(path):
                    yield None
                    continue
                yield path

    def reconcile(self):
        """Resume a bounded metadata scan; delete only after a complete scan."""
        if self._scan is None:
            self._scan_seen = set()
            self._scan_complete = True
            self._scan = self._scan_paths()
        for _ in range(self.scan_limit):
            try:
                path = next(self._scan)
            except StopIteration:
                self._scan = None
                if self._scan_complete:
                    with self.condition:
                        missing = set(self.confirmed) - self._scan_seen
                    for missing_path in missing:
                        self.enqueue("delete", missing_path)
                self._scan_seen = set()
                return
            if path is None:
                continue
            self._scan_seen.add(path)
            with self.condition:
                old = self.confirmed.get(path)
            if self.fingerprint(path) != old:
                self.enqueue("modify", path)

    def process_next(self):
        with self.condition:
            if self.stopping or not self.pending:
                return False
            path, entry = min(self.pending.items(), key=lambda item: item[1][0])
            due, action, destination, attempt = entry
            if due > time.monotonic():
                return False
            del self.pending[path]
        try:
            before = self.fingerprint(path)
            success = self.process(action, path, destination)
            if not success or before != self.fingerprint(path):
                if attempt < 2:
                    self.enqueue(action, path, destination, attempt=attempt + 1)
                return True
            with self.condition:
                updated = dict(self.confirmed)
                if before is None or action == "move":
                    updated.pop(path, None)
                else:
                    updated[path] = before
                payload = json.dumps({"version": 1, "root": str(self.root), "files": updated})
            temporary = self.state_path.with_suffix(".pending")
            if self.state_path.is_symlink() or temporary.is_symlink():
                raise ValueError("Indexer state path must not be a symlink")
            self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(payload)
            os.replace(temporary, self.state_path)
            os.chmod(self.state_path, 0o600)
            with self.condition:
                self.confirmed = updated
            if action == "move" and destination:
                self.enqueue("modify", destination)
                if before is not None:
                    self.enqueue("modify", path)
        except Exception as exc:
            logger.warning("Index queue processing failed: %s", type(exc).__name__)
            if attempt < 2:
                self.enqueue(action, path, destination, attempt=attempt + 1)
        return True

    def _run(self):
        while True:
            with self.condition:
                if self.stopping:
                    return
            if not self.process_next():
                with self.condition:
                    if not self.stopping:
                        self.condition.wait(0.2)
