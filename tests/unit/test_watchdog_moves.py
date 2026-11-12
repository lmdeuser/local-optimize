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
