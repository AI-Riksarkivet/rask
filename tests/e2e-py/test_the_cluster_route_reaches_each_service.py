"""Each public `/api/*` prefix reaches its service through the cluster's Gateway, and the service refuses an anonymous caller.

No rask process sits between the edge and a service (ADR 0156, ADR 0165): the Gateway's HTTPRoutes map each prefix onto a
Service, rewriting the prefix where the service serves another path, and each service authenticates every request
itself. One anonymous request per prefix proves both halves at once: an RFC 9457 `unauthenticatederror` comes only from
a rask service's own door (the Gateway answers an unrouted path with its own 404), so the route reached the service, the
rewrite landed on a route it serves, and the door refused.

Run against a deployed estate, or `make e2e-edge`:

    RASK_E2E_EDGE_URL=http://<gateway-address> uv run pytest tests/e2e-py/test_the_cluster_route_reaches_each_service.py -v
"""

from __future__ import annotations

import os

import pytest
import requests


EDGE = os.environ.get("RASK_E2E_EDGE_URL", "").rstrip("/")

pytestmark = [pytest.mark.e2e, pytest.mark.edge]

#: One route per public prefix that every caller must authenticate for, with the method that reaches it.
_PROBES = [
    ("GET", "/api/catalog/management/v1/me"),
    ("GET", "/api/lineage/events?limit=1"),
    ("GET", "/api/explorer/datasets"),
    ("GET", "/api/explorer/search?q=edge"),
    ("POST", "/api/explorer/annotations/tags"),
    ("GET", "/api/flows/catalog"),
    ("GET", "/api/notifications/inbox"),
    ("GET", "/api/ray/jobs"),
    ("GET", "/api/serve/applications/"),
]


@pytest.fixture(scope="module")
def edge() -> str:
    if not EDGE:
        pytest.skip("set RASK_E2E_EDGE_URL (see module docstring)")
    return EDGE


@pytest.mark.parametrize(("method", "path"), _PROBES)
def test_an_anonymous_request_reaches_the_service_and_is_refused_there(edge: str, method: str, path: str) -> None:
    response = requests.request(method, f"{edge}{path}", json={} if method == "POST" else None, timeout=15)

    assert response.status_code == 401, (response.status_code, response.text[:300])
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "https://lance.org/problems/unauthenticatederror"
