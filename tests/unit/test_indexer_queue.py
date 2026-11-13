import json
import time
from threading import Event

import pytest

from indexer_queue import IndexQueue
from watchdog_indexer import is_indexable_path

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path.parent / (tmp_path.name + "-state")))


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_restart_reconciles_offline_changes_and_deletion(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("first")
    calls = []
    def process(action, path, dest):
        calls.append((action, path))
        return True
    queue = IndexQueue(tmp_path, process, is_indexable_path, debounce=0.01)
    assert not queue.state_path.is_relative_to(tmp_path)
    assert queue.state_path != tmp_path / ".rag-index-state.json"
    queue.start()
    queue.reconcile()
    wait_for(lambda: queue.state_path.exists())
    queue.close()
    path.write_text("changed offline")
    restored = IndexQueue(tmp_path, process, is_indexable_path, debounce=0.01)
    restored.start()
    restored.reconcile()
    wait_for(lambda: restored.confirmed.get(str(path)) == restored.fingerprint(path))
    path.unlink()
    restored.reconcile()
    wait_for(lambda: str(path) not in restored.confirmed)
    restored.close()
    assert [action for action, _ in calls] == ["modify", "modify", "delete"]


def test_failure_is_not_acknowledged_and_retries_are_bounded(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("fixture")
    calls = []
    def fail(*args):
        calls.append(1)
        return False
    queue = IndexQueue(tmp_path, fail, is_indexable_path, debounce=0.01)
    queue.start()
    queue.enqueue("modify", str(path))
    wait_for(lambda: len(calls) == 3)
    queue.close()
    assert queue.confirmed == {}
    assert not queue.state_path.exists()
    assert len(calls) == 3


def test_legacy_root_state_is_read_and_new_state_is_outside_root(tmp_path):
    source = tmp_path / "note.py"
    source.write_text("original")
    fingerprint = IndexQueue.fingerprint(source)
    legacy = tmp_path / ".rag-index-state.json"
    legacy.write_text(json.dumps({"version": 1, "root": str(tmp_path), "files": {str(source): fingerprint}}))
    queue = IndexQueue(tmp_path, lambda *_: True, is_indexable_path, debounce=0)
    assert queue.confirmed[str(source)] == fingerprint
    source.write_text("new content")
    queue.reconcile()
    assert queue.process_next()
    assert queue.state_path.is_file()
    assert not queue.state_path.is_relative_to(tmp_path)
    assert json.loads(queue.state_path.read_text())["files"][str(source)] == IndexQueue.fingerprint(source)


def test_indexer_rejects_state_home_inside_watched_root(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    with pytest.raises(ValueError, match="outside the watched root"):
        IndexQueue(tmp_path, lambda *_: True, is_indexable_path)


def test_burst_uses_one_worker_and_bounded_pending_set(tmp_path):
    entered, release = Event(), Event()
    def process(*args):
        entered.set()
        release.wait(2)
        return True
    queue = IndexQueue(tmp_path, process, is_indexable_path, capacity=2, debounce=0.01)
    queue.start()
    for name in ("a.py", "b.py", "c.py", "d.py"):
        (tmp_path / name).touch()
    queue.enqueue("modify", str(tmp_path / "a.py"))
    assert entered.wait(2)
    assert queue.enqueue("modify", str(tmp_path / "b.py"))
    assert queue.enqueue("modify", str(tmp_path / "c.py"))
    assert not queue.enqueue("modify", str(tmp_path / "d.py"))
    release.set()
    queue.close()
    assert not queue.worker.is_alive()


def test_reconciliation_excludes_models_artifacts_and_symlinks(tmp_path):
    for name in ("artifacts", "models", ".git", "evaluation", "Scretch", "BlenderContent"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "unsafe.py").touch()
    (tmp_path / "file.py").touch()
    (tmp_path / "linked.py").symlink_to(tmp_path / "file.py")
    queue = IndexQueue(tmp_path, lambda *args: True, is_indexable_path)
    queue.reconcile()
    assert set(queue.pending) == {str(tmp_path / "file.py")}



def test_reconciliation_requeues_file_after_retry_exhaustion(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("fixture")
    calls = []

    def fail(*args):
        calls.append(args)
        return False

    queue = IndexQueue(tmp_path, fail, is_indexable_path, debounce=0)
    queue.enqueue("modify", str(path))
    for _ in range(3):
        assert queue.process_next()
    assert len(calls) == 3
    assert queue.pending == {}

    queue.reconcile()

    assert str(path) in queue.pending
    assert queue.pending[str(path)][3] == 0


def test_reconciliation_requeues_file_dropped_by_capacity_limit(tmp_path):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("first")
    second.write_text("second")
    queue = IndexQueue(tmp_path, lambda *args: True, is_indexable_path, capacity=1, debounce=0)

    assert queue.enqueue("modify", str(first))
    assert not queue.enqueue("modify", str(second))
    assert queue.process_next()
    assert str(second) not in queue.pending

    queue.reconcile()

    assert str(second) in queue.pending


def test_bounded_reconciliation_resumes_and_does_not_delete_before_scan_finishes(tmp_path):
    paths = [tmp_path / f"{index}.py" for index in range(5)]
    for path in paths:
        path.write_text("content")
    missing = tmp_path / "missing.py"
    queue = IndexQueue(tmp_path, lambda *_: True, is_indexable_path, scan_limit=3)
    queue.confirmed[str(missing)] = [1, 1]

    queue.reconcile()
    assert len(queue.pending) == 2
    assert str(missing) not in queue.pending
    queue.reconcile()
    assert len(queue.pending) == 5
    assert str(missing) not in queue.pending
    queue.reconcile()
    assert set(queue.pending) == {str(path) for path in paths} | {str(missing)}
    assert queue.pending[str(missing)][1] == "delete"
