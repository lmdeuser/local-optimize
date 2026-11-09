
import pytest

from source_evidence import prepare_evidence, read_snapshot

pytestmark = pytest.mark.unit


def hit_for(path):
    text, snapshot = read_snapshot(path)
    return {"content": text, "source_uri": str(path), "source_type": "code",
            "metadata": {"source_snapshot": snapshot}}


def test_changed_and_deleted_sources_are_removed_even_from_cached_hits(tmp_path):
    source = tmp_path / "code.py"
    source.write_text("value = 1\n")
    cached = hit_for(source)
    hits, count = prepare_evidence([cached], (tmp_path,))
    assert count == 0 and hits[0]["source_state"] == "current"
    source.write_text("value = 200\n")
    assert prepare_evidence([cached], (tmp_path,)) == ([], 1)
    source.unlink()
    assert prepare_evidence([cached], (tmp_path,)) == ([], 1)
    assert "source_state" not in cached


def test_other_roots_and_symlinks_cannot_supply_current_evidence(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("fixture")
    allowed = tmp_path / "project"
    allowed.mkdir()
    assert prepare_evidence([hit_for(outside)], (allowed,)) == ([], 1)
    link = allowed / "link.py"
    link.symlink_to(outside)
    linked_hit = dict(hit_for(outside), source_uri=str(link))
    assert prepare_evidence([linked_hit], (allowed,)) == ([], 1)
    with pytest.raises(OSError):
        read_snapshot(link)


def test_note_never_becomes_verified_code():
    note = {"source_uri": "agent_generated", "source_type": "llm_output",
            "validation_status": "unverified", "content": "candidate claim"}
    hits, count = prepare_evidence([note], ())
    assert count == 0
    assert hits[0]["source_kind"] == "agent_note"
    assert hits[0]["source_state"] == "untracked"
    assert hits[0]["validation_status"] == "unverified"


def test_engine_l1_hit_checks_snapshot_again(monkeypatch, tmp_path):
    import rag_engine
    from types import SimpleNamespace
    from tests.unit.test_search_scope_key import make_context

    path = tmp_path / "source.py"
    path.write_text("fixture")
    hit = dict(hit_for(path), chunk_id=1, final_score=0.9)
    engine = object.__new__(rag_engine.RagEngine)
    engine._project_cache_generation = lambda _: (1, True)
    calls = []
    def compute(*args, **kwargs):
        calls.append(1)
        return {"status": "success", "results": [hit]}
    engine._search_via_persistent_cache = compute
    monkeypatch.setattr(rag_engine, "get_project_registry", lambda _: SimpleNamespace(roots_for=lambda _: (tmp_path,)))
    context = make_context(tmp_path)
    assert engine.search(context, "fixture")["results"]
    path.write_text("changed fixture")
    result = engine.search(context, "fixture")
    assert result["results"] == []
    assert result["unusable_sources_omitted"] == 1
    assert result["cache"]["layer"] == "l1"
    assert len(calls) == 1


def test_evaluation_questions_are_not_their_own_evidence(tmp_path):
    hit = {"content": "benchmark question with no answer", "source_type": "document",
           "source_uri": str(tmp_path / "evaluation" / "cases.json")}
    assert prepare_evidence([hit], (tmp_path,)) == ([], 1)
