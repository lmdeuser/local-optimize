"""A model swap must not silently change what a score means.

Raw cosine is absolute *within* a model but not comparable across models: on this
corpus `all-MiniLM-L6-v2` puts unrelated text at ~0.35 while `multilingual-e5-small`
puts it at ~0.86. Feeding both into one calibrated threshold would mark the whole
store relevant under E5. Each model therefore carries measured anchors mapping it
onto one shared scale, plus the asymmetric prefixes it was trained with.
"""
import numpy as np
import pytest

import rag_engine


pytestmark = pytest.mark.unit


def test_every_registered_model_declares_a_full_profile():
    for name, profile in rag_engine.MODEL_PROFILES.items():
        assert set(profile) == {"query_prefix", "passage_prefix", "cos_lo", "cos_hi"}, name
        assert profile["cos_hi"] > profile["cos_lo"], name


def test_the_configured_model_is_registered():
    """An unregistered model falls back to identity, which no threshold fits."""
    assert rag_engine.EMBEDDING_MODEL_NAME in rag_engine.MODEL_PROFILES


def test_an_unknown_model_falls_back_to_the_identity_map():
    profile = rag_engine.get_model_profile("some-model-nobody-measured")

    assert profile["cos_lo"] == 0.0 and profile["cos_hi"] == 1.0
    assert profile["query_prefix"] == "" and profile["passage_prefix"] == ""


@pytest.mark.parametrize("model", sorted(rag_engine.MODEL_PROFILES))
def test_noise_and_true_match_land_on_the_same_scale_for_every_model(model, monkeypatch):
    """The anchors exist so these two land in the same place whichever model runs.

    Unrelated text must score below the relevance floor and a typical true match
    above it — that is what lets one calibrated cut survive a model swap.
    """
    monkeypatch.setattr(rag_engine, "EMBEDDING_MODEL_NAME", model)
    profile = rag_engine.MODEL_PROFILES[model]
    lo, hi = profile["cos_lo"], profile["cos_hi"]
    noise_cos = lo + 0.15 * (hi - lo)
    true_cos = lo + 0.55 * (hi - lo)

    ones = np.ones(1, dtype=np.float32)
    rrf = np.full(1, rag_engine.RRF_REFERENCE, dtype=np.float32)
    noise = rag_engine.compose_final_scores(
        np.array([noise_cos], dtype=np.float32), np.zeros(1, dtype=np.float32),
        ones, np.zeros(1, dtype=np.float32), ones,
    )[0]
    true = rag_engine.compose_final_scores(
        np.array([true_cos], dtype=np.float32), rrf,
        ones, np.zeros(1, dtype=np.float32), ones,
    )[0]

    assert noise < rag_engine.RELEVANCE_FLOOR, f"{model}: noise scored {noise}"
    assert true >= rag_engine.RELEVANCE_FLOOR, f"{model}: true match scored {true}"


def test_passage_and_query_sides_get_their_own_prefix(monkeypatch):
    monkeypatch.setattr(rag_engine, "EMBEDDING_MODEL_NAME", "multilingual-e5-small")
    seen = []

    engine = object.__new__(rag_engine.RagEngine)
    engine._st_model = type(
        "M", (), {"device": type("Device", (), {"type": "xpu"})(), "encode": lambda _self, text, **kw: seen.append(text) or np.zeros(4)}
    )()

    engine.encode("что такое кэш", kind="query")
    engine.encode("кэш это ...", kind="passage")

    assert seen[0].startswith("query: ")
    assert seen[1].startswith("passage: ")


def test_a_symmetric_model_adds_no_prefix(monkeypatch):
    monkeypatch.setattr(rag_engine, "EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")
    seen = []

    engine = object.__new__(rag_engine.RagEngine)
    engine._st_model = type(
        "M", (), {"device": type("Device", (), {"type": "xpu"})(), "encode": lambda _self, text, **kw: seen.append(text) or np.zeros(4)}
    )()

    engine.encode("plain text", kind="passage")

    assert seen == ["plain text"]
