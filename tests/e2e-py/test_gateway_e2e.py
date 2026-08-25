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
    """Skip when NOBODY ASKED; fail when somebody asked and the estate is not there.

    Both outcomes used to be a skip, and that is why this suite could not fail. Unset variable →
    3 skips → exit 0, which is right: an offline run should not demand a deployed gateway. But an
    unreachable gateway with the variable EXPLICITLY set was also 3 skips and exit 0 — so `make
    e2e-gateway` against a dead estate reported the same success as against a healthy one, and the
    routing proof could only ever pass. The stale `/lineage/livez` and `/catalog/readyz` assertions
    this file carried (the gateway serves `/api/lineage` and `/api/catalog`, with no `/api` catch-all)
    survived precisely there: an assertion inside a suite that cannot run is invisible twice over.

    Setting the variable is a request for the drive. A request that cannot be served is a failure.
    """
    if not GATEWAY:
        pytest.skip("set LANCE_E2E_GATEWAY_URL (see module docstring)")
    base = GATEWAY.rstrip("/")
    try:
        requests.get(f"{base}/healthz", timeout=5).raise_for_status()
    except Exception as exc:
        pytest.fail(
            f"LANCE_E2E_GATEWAY_URL is set to {GATEWAY} but the gateway did not answer /healthz: {exc}. "
            "This is a FAILURE rather than a skip on purpose — asking for the drive and getting nothing "
            "is not the same as not asking, and reporting it as a skip made this proof unfailable."
        )
    return base


def test_gateway_own_health_is_upstream_independent(gateway: str) -> None:
    # The edge must answer even if every backend is down — it owns no upstream for /healthz.
    resp = requests.get(f"{gateway}/healthz", timeout=5)
    # JSON, not the bare `ok` this asserted until 2026-08-25. The gateway answers with the shared
    # `Liveness` model from `service_kit.probes`, exactly as every other service does — so the contract
    # MOVED (a541d35f) and this now asserts where it moved to. Asserting the PAYLOAD rather than only
    # the status keeps the point of the test: a 200 with an empty or HTML body is still a broken edge.
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}, resp.text


def test_app_apis_route_through_dapr_service_invocation(gateway: str) -> None:
    # `/api/lineage/*` and `/api/catalog/*` are proxied to 127.0.0.1:3500/v1.0/invoke/<app>/method/* —
    # i.e. through the gateway's OWN Dapr sidecar. A 200 here means the clean URL → Dapr invoke →
    # service hop works.
    #
    # THE PREFIX IS `/api/`, AND THIS FILE ASSERTED IT WITHOUT ONE. `gateway/__init__.py:144-145`
    # registers `("/api/catalog", ...)` and `("/api/lineage", ...)`, and the gateway has NO `/api`
    # catch-all — an unmatched path 404s. So both requests below were aimed at routes the gateway does
    # not serve and would have failed against any live estate. Nothing noticed because the whole file
    # skips (see the module docstring): a stale assertion inside a suite that never runs is invisible
    # twice over.
    lineage = requests.get(f"{gateway}/api/lineage/livez", timeout=8)
    assert lineage.status_code == 200 and lineage.json() == {"status": "ok"}

    catalog = requests.get(f"{gateway}/api/catalog/readyz", timeout=8)
    assert catalog.status_code == 200


def test_root_is_backend_only(gateway: str) -> None:
    # Since the MFE migration the zones are served by the Ingress (web-<zone>); the gateway is the
    # BACKEND-only edge, so "/" answers an honest 404 instead of proxying a retired web upstream
    # (the old upstream crashed every fresh gateway boot with "host not found").
    assert requests.get(f"{gateway}/", timeout=8).status_code == 404
