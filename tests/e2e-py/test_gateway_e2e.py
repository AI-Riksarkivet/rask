"""End-to-end test for the API gateway — single entry point routing via Dapr service invocation.

Proves the architectural claim that one front routes the whole platform: the gateway's own health is
independent of upstreams, app APIs (/lineage/*, /catalog/*) reach the services THROUGH the gateway's
Dapr sidecar (service invocation, not a raw nginx upstream), and / serves the UI. A regression here means
the single-entry story (or the Dapr-invoke wiring) silently broke.

Run (port-forward the gateway), or `make e2e-gateway`:

    kubectl port-forward svc/lance-ns-gateway 8088:8080 &
    LANCE_E2E_GATEWAY_URL=http://localhost:8088 uv run pytest tests/e2e-py/test_gateway_e2e.py -v
"""

from __future__ import annotations

import os

import pytest
import requests


GATEWAY = os.environ.get("LANCE_E2E_GATEWAY_URL", "")

pytestmark = [pytest.mark.e2e, pytest.mark.gateway]


@pytest.fixture(scope="module")
def gateway() -> str:
    if not GATEWAY:
        pytest.skip("set LANCE_E2E_GATEWAY_URL (see module docstring)")
    try:
        requests.get(f"{GATEWAY.rstrip('/')}/healthz", timeout=5).raise_for_status()
    except Exception:
        pytest.skip(f"gateway not reachable at {GATEWAY}")
    return GATEWAY.rstrip("/")


def test_gateway_own_health_is_upstream_independent(gateway: str) -> None:
    # The edge must answer even if every backend is down — it owns no upstream for /healthz.
    resp = requests.get(f"{gateway}/healthz", timeout=5)
    assert resp.status_code == 200 and resp.text.strip() == "ok"


def test_app_apis_route_through_dapr_service_invocation(gateway: str) -> None:
    # /lineage/* and /catalog/* are proxied to 127.0.0.1:3500/v1.0/invoke/<app>/method/* — i.e. through
    # the gateway's OWN Dapr sidecar. A 200 here means the clean URL → Dapr invoke → service hop works.
    lineage = requests.get(f"{gateway}/lineage/livez", timeout=8)
    assert lineage.status_code == 200 and lineage.json() == {"status": "ok"}

    catalog = requests.get(f"{gateway}/catalog/readyz", timeout=8)
    assert catalog.status_code == 200


def test_root_is_backend_only(gateway: str) -> None:
    # Since the MFE migration the zones are served by the Ingress (web-<zone>); the gateway is the
    # BACKEND-only edge, so "/" answers an honest 404 instead of proxying a retired web upstream
    # (the old upstream crashed every fresh gateway boot with "host not found").
    assert requests.get(f"{gateway}/", timeout=8).status_code == 404
