"""One bounded indexing worker with durable, success-only reconciliation state."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Condition, Thread


class IndexQueue:
    def __init__(self, root, process, indexable, *, capacity=500, debounce=1.0):
        self.root = Path(root).resolve()
        self.process = process
        self.indexable = indexable
        self.capacity = capacity
        self.debounce = debounce
        self.pending = {}
        self.condition = Condition()
        self.stopping = False
        self.worker = None
        self.state_path = self.root / ".rag-index-state.json"
        self.confirmed = {}
        if self.state_path.is_file() and not self.state_path.is_symlink():
            if self.state_path.stat().st_size <= 2_000_000:
                try:
                    data = json.loads(self.state_path.read_text())
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

    def reconcile(self):
        """Scan metadata only; ignored directories and symlinks are never traversed."""
        seen = set()
        complete = True
        def scan_error(_error):
            nonlocal complete
            complete = False
        for directory, dirs, files in os.walk(self.root, followlinks=False, onerror=scan_error):
            dirs[:] = [name for name in dirs if self.indexable(Path(directory).relative_to(self.root) / name / "probe.py")
                       and not (Path(directory) / name).is_symlink()]
            for name in files:
                path = str(Path(directory) / name)
                if not self._allowed(path):
                    continue
                seen.add(path)
                if len(seen) > 10000:
                    complete = False
                    break
                with self.condition:
                    old = self.confirmed.get(path)
                current = self.fingerprint(path)
                if current != old:
                    self.enqueue("modify", path)
            if not complete:
                break
        if complete:
            with self.condition:
                missing = set(self.confirmed) - seen
            for path in missing:
                self.enqueue("delete", path)

    def _run(self):
        while True:
            with self.condition:
                if self.stopping:
                    return
                if not self.pending:
                    self.condition.wait()
                    continue
                path, entry = min(self.pending.items(), key=lambda item: item[1][0])
                due, action, destination, attempt = entry
                delay = due - time.monotonic()
                if delay > 0:
                    self.condition.wait(delay)
                    continue
                del self.pending[path]
            try:
                before = self.fingerprint(path)
                success = self.process(action, path, destination)
                if not success or before != self.fingerprint(path):
                    if attempt < 2:
                        self.enqueue(action, path, destination, attempt=attempt + 1)
                    continue
                with self.condition:
                    updated = dict(self.confirmed)
                    if before is None or action == "move":
                        updated.pop(path, None)
                    else:
                        updated[path] = before
                    payload = json.dumps({"version": 1, "root": str(self.root), "files": updated})
                temporary = self.state_path.with_suffix(".pending")
                # Private, atomic output; never follow an injected state-file symlink.
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(payload)
                os.replace(temporary, self.state_path)
                with self.condition:
                    self.confirmed = updated
                if action == "move" and destination:
                    self.enqueue("modify", destination)
                    if before is not None:
                        self.enqueue("modify", path)
            except Exception:
                # A failed operation is not acknowledged. A later metadata scan
                # retries it, with no tight loop and no source contents in logs.
                if attempt < 2:
                    self.enqueue(action, path, destination, attempt=attempt + 1)
