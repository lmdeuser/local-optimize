import pytest

from project_context import ProjectDefinition, ProjectRegistry


pytestmark = pytest.mark.unit


def test_parent_queue_prunes_separately_registered_nested_root(tmp_path):
    from indexer_queue import IndexQueue
    from watchdog_indexer import is_indexable_path

    parent = tmp_path / "parent"
    nested = parent / "nested"
    nested.mkdir(parents=True)
    (parent / "parent.py").touch()
    (nested / "nested.py").touch()
    registry = ProjectRegistry([
        ProjectDefinition("parent", "Parent", {"primary": parent}),
        ProjectDefinition("nested", "Nested", {"primary": nested}),
    ])

    def indexable(relative):
        candidate = parent / relative
        return is_indexable_path(relative) and registry.is_owned_by(
            candidate, project_id="parent", root_id="primary"
        )

    queue = IndexQueue(parent, lambda *args: True, indexable)
    queue.reconcile()
    assert set(queue.pending) == {str(parent / "parent.py")}


def test_watchdog_supervisor_adds_and_removes_roots_without_restart(tmp_path, monkeypatch):
    import watchdog_indexer

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    current = ProjectRegistry([ProjectDefinition("first", "First", {"primary": first})])
    monkeypatch.setattr(watchdog_indexer, "get_project_registry", lambda _settings: current)
    supervisor = watchdog_indexer.WatchdogSupervisor(watch_paths=[first, second])
    try:
        supervisor.reconcile_registry()
        assert ("first", "primary") in supervisor.indexers
        assert any(key[0].startswith("ws:") for key in supervisor.indexers)
        current = ProjectRegistry([
            ProjectDefinition("first", "First", {"primary": first}),
            ProjectDefinition("second", "Second", {"primary": second}),
        ])
        supervisor.reconcile_registry()
        assert set(supervisor.indexers) == {("first", "primary"), ("second", "primary")}
        current = ProjectRegistry([ProjectDefinition("second", "Second", {"primary": second})])
        supervisor.reconcile_registry()
        assert ("second", "primary") in supervisor.indexers
        assert any(key[0].startswith("ws:") for key in supervisor.indexers)
    finally:
        supervisor.close()


def test_watchdog_is_opt_in_and_watches_only_root_with_safe_kernel_mask(tmp_path, monkeypatch):
    from watchdog.observers.api import ObservedWatch
    from watchdog.observers.inotify import InotifyEmitter
    from watchdog.observers.inotify_c import InotifyConstants
    from queue import Queue
    import watchdog_indexer

    root = tmp_path / "root"
    root.mkdir()
    registry = ProjectRegistry([ProjectDefinition("root", "Root", {"primary": root})])
    monkeypatch.setattr(watchdog_indexer, "get_project_registry", lambda _settings: registry)
    supervisor = watchdog_indexer.WatchdogSupervisor()
    try:
        supervisor.reconcile_registry()
        assert not supervisor.indexers
    finally:
        supervisor.close()


    supervisor = watchdog_indexer.WatchdogSupervisor(watch_paths=[root])
    try:
        supervisor.reconcile_registry()
        watch = supervisor.watches[("root", "primary")]
        assert not watch.is_recursive
        emitter = InotifyEmitter(Queue(), ObservedWatch(str(root), recursive=False),
                                  event_filter=watchdog_indexer.WATCH_EVENTS)
        mask = emitter.get_event_mask_from_filter()
        assert not mask & (InotifyConstants.IN_OPEN | InotifyConstants.IN_CLOSE_NOWRITE)
        assert mask & InotifyConstants.IN_CLOSE_WRITE
    finally:
        supervisor.close()


def test_watchdog_limits_number_of_explicit_roots(tmp_path, caplog):
    import watchdog_indexer

    roots = [tmp_path / str(index) for index in range(watchdog_indexer.MAX_WATCH_ROOTS + 1)]
    for root in roots:
        root.mkdir()
    supervisor = watchdog_indexer.WatchdogSupervisor(watch_paths=roots)
    try:
        assert len(supervisor.watch_paths) == watchdog_indexer.MAX_WATCH_ROOTS
        assert "remaining paths are not watched" in caplog.text
    finally:
        supervisor.close()


def test_watchdog_subdir_watches_schedules_only_named_subdirs(tmp_path, monkeypatch):
    from pathlib import Path
    import watchdog_indexer

    root = tmp_path / "proj"
    skills = root / "skills"
    other = root / "other"
    skills.mkdir(parents=True)
    other.mkdir()
    registry = ProjectRegistry([ProjectDefinition("proj", "Proj", {"primary": root})])
    monkeypatch.setattr(watchdog_indexer, "get_project_registry", lambda _settings: registry)

    supervisor = watchdog_indexer.WatchdogSupervisor(
        watch_paths=[root],
        subdir_watches=("skills",),
    )
    try:
        supervisor.reconcile_registry()
        handles = supervisor._subdir_watch_handles[("proj", "primary")]
        assert len(handles) == 1
        assert Path(handles[0].path).resolve() == skills.resolve()
        assert Path(supervisor.watches[("proj", "primary")].path).resolve() != root.resolve()
        assert not handles[0].is_recursive
    finally:
        supervisor.close()


def test_watchdog_run_drains_inflight_work_on_sigterm_and_restores_handlers(tmp_path, monkeypatch):
    import os
    import signal
    import threading
    import time
    import watchdog_indexer

    root = tmp_path / "proj"
    root.mkdir()
    registry = ProjectRegistry([ProjectDefinition("proj", "Proj", {"primary": root})])
    monkeypatch.setattr(watchdog_indexer, "get_project_registry", lambda _settings: registry)

    supervisor = watchdog_indexer.WatchdogSupervisor(reconcile_interval=30.0)
    monkeypatch.setattr(supervisor, "reconcile_registry", lambda: None)

    in_flight = threading.Event()
    drained = threading.Event()

    class FakeQueue:
        def process_next(self):
            if not in_flight.is_set():
                in_flight.set()
                time.sleep(0.08)
                drained.set()
            return False

        def close(self):
            return None

    supervisor.indexers[("proj", "primary")] = type("FakeIndexer", (), {"queue": FakeQueue()})()

    def send_sigterm():
        assert in_flight.wait(timeout=2.0)
        os.kill(os.getpid(), signal.SIGTERM)

    killer = threading.Thread(target=send_sigterm, daemon=True)
    orig_term = signal.getsignal(signal.SIGTERM)
    orig_int = signal.getsignal(signal.SIGINT)
    killer.start()
    supervisor.run()
    killer.join(timeout=2.0)

    assert drained.is_set()
    assert not supervisor._started
    assert signal.getsignal(signal.SIGTERM) == orig_term
    assert signal.getsignal(signal.SIGINT) == orig_int
