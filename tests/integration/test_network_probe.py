import json
import os
import urllib.request

import pytest

from config import settings

pytestmark = [pytest.mark.integration, pytest.mark.network]

_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def test_explicit_network_liveness_contract():
    base_url = os.getenv("RAG_NETWORK_TEST_URL")
    if not base_url:
        pytest.skip("RAG_NETWORK_TEST_URL is not configured for explicit network testing")
    headers = {}
    token = settings.bearer_token.get_secret_value()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base_url.rstrip("/") + "/liveness",
        headers=headers,
    )
    with _DIRECT_OPENER.open(request, timeout=5) as response:
        assert response.status == 200
        assert json.loads(response.read().decode("utf-8")) == {"status": "success"}
