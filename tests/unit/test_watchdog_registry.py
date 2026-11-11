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
    supervisor = watchdog_indexer.WatchdogSupervisor()
    try:
        supervisor.reconcile_registry()
        assert set(supervisor.indexers) == {("first", "primary")}
        current = ProjectRegistry([
            ProjectDefinition("first", "First", {"primary": first}),
            ProjectDefinition("second", "Second", {"primary": second}),
        ])
        supervisor.reconcile_registry()
        assert set(supervisor.indexers) == {("first", "primary"), ("second", "primary")}
        current = ProjectRegistry([ProjectDefinition("second", "Second", {"primary": second})])
        supervisor.reconcile_registry()
        assert set(supervisor.indexers) == {("second", "primary")}
    finally:
        supervisor.close()
