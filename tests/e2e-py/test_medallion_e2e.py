"""End-to-end test for the event-driven medallion cascade (lance-ray → 3 movers → lineage DAG).

ONE call to lance-ray's ``/produce`` must cascade the whole pipeline — raw → bronze → silver → gold —
purely through Dapr pub/sub, and the lineage graph must end up showing gold transitively derived from
raw. This is the regression guard for "the medallion services are wired and the triggers chain".

Run (port-forward lance-ray + lineage to distinct local ports first), or `make e2e-medallion`:

    kubectl port-forward svc/lance-ns-lance-ray 8002:8000 &
    kubectl port-forward svc/lance-ns-lineage   8000:8000 &
    LANCE_E2E_LANCERAY_URL=http://localhost:8002 LANCE_E2E_LINEAGE_URL=http://localhost:8000 \
    uv run pytest tests/e2e/test_medallion_e2e.py -v
"""

from __future__ import annotations

import os
import time

import pytest
import requests


LANCERAY = os.environ.get("LANCE_E2E_LANCERAY_URL", "")
LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "")
# /produce is guarded by require_dapr_token; when the deployed stack sets APP_API_TOKEN this must carry the
# shared secret (empty on a token-less dev stack, where the guard is a no-op). `make e2e-medallion` fills it.
DAPR_TOKEN = os.environ.get("LANCE_E2E_DAPR_TOKEN", "")

# Governed lineage READS use the app-token SERVICE door as `service-web` (a warehouse reader — the same
# read-only identity the web BFF uses). Auth-off → OIDC off → authenticate() pass-through (harmless);
# auth-on → this is what lets the reads through instead of a 401.
_LINEAGE_HEADERS = {"dapr-api-token": DAPR_TOKEN, "x-lance-service-identity": "service-web"} if DAPR_TOKEN else {}

pytestmark = [pytest.mark.e2e, pytest.mark.medallion]


@pytest.fixture(scope="module")
def urls() -> tuple[str, str]:
    if not (LANCERAY and LINEAGE):
        pytest.skip("set LANCE_E2E_LANCERAY_URL and LANCE_E2E_LINEAGE_URL (see module docstring)")
    for name, url in (("lance-ray", LANCERAY), ("lineage", LINEAGE)):
        try:
            requests.get(f"{url.rstrip('/')}/livez", timeout=5).raise_for_status()
        except Exception:
            pytest.skip(f"{name} not reachable at {url}")
    return LANCERAY.rstrip("/"), LINEAGE.rstrip("/")


def _run_count(lineage: str) -> int:
    """How many runs the lineage graph has recorded — the freshness baseline for the cascade."""
    resp = requests.get(f"{lineage}/runs?limit=1000", headers=_LINEAGE_HEADERS, timeout=8)
    resp.raise_for_status()
    return len(resp.json().get("runs", []))


def test_produce_cascades_raw_to_gold(urls: tuple[str, str]) -> None:
    lance_ray, lineage = urls

    # Snapshot the run count FIRST. gold's upstream set may already exist from earlier produces, so
    # set-membership alone can't prove THIS trigger did anything — the graph would look identical if the
    # cascade silently no-op'd. A fresh produce mints a new run per stage (producer + 3 movers = +4), so a
    # strictly rising run count is the real "the cascade fired just now" signal.
    before = _run_count(lineage)

    # ACT — one trigger at the head of the pipeline (carrying the app-token when the stack enforces it).
    headers = {"dapr-api-token": DAPR_TOKEN} if DAPR_TOKEN else {}
    produced = requests.post(f"{lance_ray}/produce", headers=headers, timeout=8)
    assert produced.status_code == 202 and produced.json()["status"] == "produced", produced.text

    # ASSERT — the cascade reached gold (its transitive upstream is the full chain) AND it did so from THIS
    # produce: all four stages emitted a fresh run, so the count grew by the producer + 3 movers.
    chain = {"raw_events", "bronze$events", "silver$features"}
    deadline = time.monotonic() + 60.0
    upstream: list[str] = []
    while time.monotonic() < deadline:
        resp = requests.get(f"{lineage}/datasets/gold$catalog/upstream", headers=_LINEAGE_HEADERS, timeout=8)
        if resp.status_code == 200:
            upstream = [ref["name"] for ref in resp.json().get("related", [])]
            if chain <= set(upstream) and _run_count(lineage) >= before + 4:
                return
        time.sleep(3)
    pytest.fail(f"gold$catalog cascade did not complete within 60s (upstream={upstream}, runs {before}->{_run_count(lineage)}, expected >= {before + 4})")
