import pytest

from rag_benchmark import is_correct_abstention
from config import settings

pytestmark = pytest.mark.unit


def test_missing_summary_score_does_not_hide_accepted_irrelevant_hit():
    assert not is_correct_abstention({
        "status": "success", "results": [{"final_score": settings.relevance_floor + 0.01}]
    })


@pytest.mark.parametrize("status", ["error", "unavailable"])
def test_failure_is_not_a_correct_abstention(status):
    assert not is_correct_abstention({"status": status, "results": []})


def test_only_below_floor_candidates_are_an_abstention():
    assert is_correct_abstention({
        "status": "success", "results": [{"final_score": settings.relevance_floor - 0.01}]
    })
