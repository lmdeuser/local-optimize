import json
import os
import urllib.request

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.network]


def test_explicit_network_liveness_contract():
    base_url = os.getenv("RAG_NETWORK_TEST_URL")
    if not base_url:
        pytest.skip("RAG_NETWORK_TEST_URL is not configured for explicit network testing")
    with urllib.request.urlopen(base_url.rstrip("/") + "/liveness", timeout=5) as response:
        assert response.status == 200
        assert json.loads(response.read().decode("utf-8")) == {"status": "success"}
