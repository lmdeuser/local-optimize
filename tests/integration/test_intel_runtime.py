"""Optional real Intel GPU check; no model loading and no database access."""

import numpy as np
import pytest

from rag_engine import XpuReranker
from tests.similarity_reference import ReferenceReranker

pytestmark = pytest.mark.integration


def test_intel_gpu_cosine_matches_reference():
    import torch

    if not torch.xpu.is_available():
        pytest.skip("Intel XPU device is not exposed to this process")
    rng = np.random.default_rng(29)
    query = rng.normal(size=384).astype(np.float32)
    candidates = rng.normal(size=(80, 384)).astype(np.float32)
    candidates[0] = query
    candidates[1] = 0
    scores = XpuReranker().re_rank(query, candidates)
    reference = ReferenceReranker().re_rank(query, candidates)
    np.testing.assert_allclose(scores, reference, rtol=1e-5, atol=1e-6)
    assert scores.argmax() == 0
    assert scores[1] == 0
