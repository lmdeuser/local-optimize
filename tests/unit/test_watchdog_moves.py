from pathlib import Path

import pytest

pytest.importorskip("watchdog")

from config import settings
from project_context import ProjectContext, ProjectDefinition, ProjectRegistry, ReadScope, WriteVisibility
from watchdog_indexer import BASE_DIR, DebouncedIndexer

pytestmark = pytest.mark.unit


class FakeClient:
    def __init__(self, move_result=None, delete_result=None):
        self.calls = []
        self.move_result = move_result or {"status": "success", "moved_count": 1}
        self.delete_result = delete_result or {"status": "success", "deleted_count": 1}

    def move_document(self, source, destination, *, context):
        self.calls.append(("move", source, destination, context.project_id))
        return self.move_result

    def delete_document(self, path, *, context):
        self.calls.append(("delete", path, context.project_id))
        return self.delete_result

    def index_code(self, path, *, context):
        self.calls.append(("index_code", path, context.project_id))
        return {"status": "success"}

    def index_document(self, path, *, context):
        self.calls.append(("index_document", path, context.project_id))
        return {"status": "success"}


def make_indexer(tmp_path, client):
    context = ProjectContext(
        "project-a", "Project A", "root-a", str(tmp_path), (str(tmp_path),),
        WriteVisibility.SHARED, ReadScope.SHARED, "watchdog", "service",
    )
    registry = ProjectRegistry([
        ProjectDefinition("project-a", "Project A", {"root-a": tmp_path})
    ])
    return DebouncedIndexer(client, context, registry)


def test_valid_move_uses_single_server_side_operation(tmp_path):
    source = tmp_path / "old.py"
    destination = tmp_path / "new.py"
    destination.write_text("print('fixture')", encoding="utf-8")
    client = FakeClient()
    indexer = make_indexer(tmp_path, client)

    indexer._process_now("move", str(source), str(destination))

    assert client.calls == [("move", str(source), str(destination), "project-a")]


def test_move_to_ignored_destination_only_deletes_scoped_source(tmp_path):
    source = tmp_path / "old.py"
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    destination = hidden / "new.py"
    destination.write_text("print('fixture')", encoding="utf-8")
    client = FakeClient()
    indexer = make_indexer(tmp_path, client)

    indexer._process_now("move", str(source), str(destination))

    assert client.calls == [("delete", str(source), "project-a")]


def test_failed_atomic_move_never_falls_back_to_delete(tmp_path):
    source = tmp_path / "old.py"
    destination = tmp_path / "new.py"
    destination.write_text("print('fixture')", encoding="utf-8")
    client = FakeClient(move_result={"status": "error", "moved_count": 0})
    indexer = make_indexer(tmp_path, client)

    indexer._process_now("move", str(source), str(destination))

    assert [call[0] for call in client.calls] == ["move"]


def test_delayed_delete_indexes_a_recreated_file(tmp_path):
    path = tmp_path / "recreated.py"
    path.write_text("value = 42\n")
    client = FakeClient()
    indexer = make_indexer(tmp_path, client)
    assert indexer._process_now("delete", str(path)) is True
    assert [call[0] for call in client.calls] == ["index_code"]


def test_fallback_does_not_depend_on_a_developer_home():
    source = (Path(BASE_DIR) / "watchdog_indexer.py").read_text(encoding="utf-8")

    assert 'Path("/home/' not in source
    assert BASE_DIR == settings.project_root



def test_partial_index_result_is_not_acknowledged(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("value = 1\n")

    class PartialClient(FakeClient):
        def index_code(self, path, *, context):
            return {"status": "success", "index_mode": "partial", "completed": False}

    indexer = make_indexer(tmp_path, PartialClient())

    assert indexer._process_now("modify", str(path)) is False



def test_delete_after_recursive_directory_removal_is_processed(tmp_path):
    import shutil

    nested = tmp_path / "docs" / "subdir"
    nested.mkdir(parents=True)
    deleted = nested / "note.md"
    deleted.write_text("fixture", encoding="utf-8")
    client = FakeClient()
    indexer = make_indexer(tmp_path, client)
    shutil.rmtree(tmp_path / "docs")

    assert indexer._process_now("delete", str(deleted)) is True
    assert client.calls == [("delete", str(deleted), "project-a")]


def test_move_not_applied_falls_back_to_modify_index(tmp_path):
    old_path = tmp_path / "old.py"
    new_path = tmp_path / "new.py"
    new_path.write_text("print('moved')\n", encoding="utf-8")
    client = FakeClient(move_result={"status": "not_found", "moved_count": 0})
    indexer = make_indexer(tmp_path, client)

    assert indexer._process_now("move", str(old_path), str(new_path)) is True
    assert client.calls == [
        ("move", str(old_path), str(new_path), "project-a"),
        ("index_code", str(new_path), "project-a"),
    ]

    conflict_client = FakeClient(
        move_result={"status": "error", "message": "HTTP 409"}
    )
    conflict_indexer = make_indexer(tmp_path, conflict_client)
    assert conflict_indexer._process_now("move", str(old_path), str(new_path)) is True
    assert conflict_client.calls == [
        ("move", str(old_path), str(new_path), "project-a"),
        ("index_code", str(new_path), "project-a"),
    ]


def test_move_from_unallowed_source_into_indexable_destination_indexes_destination(tmp_path):
    root_a = tmp_path / "proj"
    outside_dir = tmp_path / "outside"
    root_a.mkdir()
    outside_dir.mkdir()
    (root_a / ".venv").mkdir()
    arrived = root_a / "arrived.py"
    arrived.write_text("print('arrived')\n", encoding="utf-8")

    client = FakeClient()
    indexer = make_indexer(root_a, client)

    assert indexer._process_now("move", str(outside_dir / "staged.py"), str(arrived)) is True
    assert client.calls == [("index_code", str(arrived), "project-a")]
    assert not any(call[0] == "delete" for call in client.calls)

    queued = indexer.trigger("move", str(root_a / ".venv" / "x.py"), dest_path=str(arrived))
    assert queued is True
    assert set(indexer.queue.pending) == {str(arrived)}
    assert indexer.queue.pending[str(arrived)][1] == "modify"
