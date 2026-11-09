"""Intel-only inference must fail closed before executing any CPU model."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

import rag_engine

pytestmark = pytest.mark.unit


def test_missing_xpu_never_constructs_a_cpu_model(monkeypatch, tmp_path):
    constructor = Mock()
    monkeypatch.setattr(rag_engine, "MODEL_PATH", str(tmp_path))
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=constructor))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        xpu=SimpleNamespace(is_available=lambda: False),
        cuda=SimpleNamespace(is_available=lambda: False),
    ))
    engine = object.__new__(rag_engine.RagEngine)
    with pytest.raises(RuntimeError, match="Intel XPU"):
        engine._load_model()
    constructor.assert_not_called()


def test_file_model_is_rejected_before_backend_loading(monkeypatch, tmp_path):
    model_file = tmp_path / "model.onnx"
    model_file.touch()
    monkeypatch.setattr(rag_engine, "MODEL_PATH", str(model_file))
    engine = object.__new__(rag_engine.RagEngine)
    with pytest.raises(RuntimeError, match="directory"):
        engine._load_model()


def test_xpu_reranker_refuses_cpu_only_runtime(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        xpu=SimpleNamespace(is_available=lambda: False),
    ))
    with pytest.raises(RuntimeError, match="Intel XPU"):
        rag_engine.XpuReranker()


def test_model_moved_to_cpu_is_rejected_before_encoding():
    encode = Mock(return_value=np.zeros(384))
    engine = object.__new__(rag_engine.RagEngine)
    engine._st_model = SimpleNamespace(device=SimpleNamespace(type="cpu"), encode=encode)
    with pytest.raises(RuntimeError, match="Intel XPU"):
        engine._encode_uncached("query: fixture")
    encode.assert_not_called()


def test_missing_gpu_scores_never_trigger_numpy_fallback(tmp_path, monkeypatch):
    from tests.unit.test_search_score_calibration import build_engine, make_context

    engine = build_engine([[1.0, 0.0]], tmp_path)
    engine.reranker = SimpleNamespace(re_rank=lambda *_args: None)
    norm = Mock(wraps=np.linalg.norm)
    monkeypatch.setattr(np.linalg, "norm", norm)
    result = engine._retrieve_candidates(make_context(tmp_path), "fixture")
    assert result["status"] == "unavailable"
    norm.assert_not_called()
