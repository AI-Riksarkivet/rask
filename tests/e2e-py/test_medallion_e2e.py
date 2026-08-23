"""End-to-end test for the event-driven medallion cascade (medallion-producer → 2 movers → lineage DAG).

ONE call to medallion-producer's ``/produce`` must cascade the whole pipeline — bronze → silver → gold (R23:
the producer ingests straight into bronze; raw is the external world) — purely through Dapr pub/sub,
and the lineage graph must end up showing gold transitively derived from bronze. This is the
regression guard for "the medallion services are wired and the triggers chain".

Run (port-forward medallion-producer + lineage to distinct local ports first), or `make e2e-medallion`:

    kubectl port-forward svc/lance-ns-medallion-producer 8002:8000 &
    kubectl port-forward svc/lance-ns-lineage   8000:8000 &
    LANCE_E2E_LANCERAY_URL=http://localhost:8002 LANCE_E2E_LINEAGE_URL=http://localhost:8000 \
    uv run pytest tests/e2e-py/test_medallion_e2e.py -v
"""

from __future__ import annotations

import os
import time

import pytest
import requests


LANCERAY = os.environ.get("LANCE_E2E_LANCERAY_URL", "")
LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "")
# /produce is guarded by require_dapr_token; when the deployed stack sets APP_API_TOKEN this must carry the
# shared secret (empty on a token-less dev stack, where the guard is a no-op). `make e2e-medallion` does NOT
# fill it — it requires the two URLs and forwards them, and the token stays the caller's to supply from the
# estate's secret store. This line used to claim the target filled it, which sent a reader looking for a
# mechanism that was never written.
DAPR_TOKEN = os.environ.get("LANCE_E2E_DAPR_TOKEN", "")
#: The tenant to drive, and on a publish-driven estate it is REQUIRED rather than optional.
#:
#: With `medallion.cascadeViaPublish` on, the cascade is triggered by `publication_trigger`, and that
#: publisher ALWAYS carries a project — `transform.py` says why: "the mover cannot resolve its tiers
#: without it". So a projectless produce publishes silver and gold never fires, and this test failed
#: with `runs 131->133, expected >= 134` against a cascade that was working perfectly for a tenant.
#: The shape asserted here has to be the shape the estate runs.
#:
#: Unset keeps the projectless drive, which is correct for an estate still on the legacy trigger.
PROJECT = os.environ.get("LANCE_E2E_PROJECT", "")


def _qualified(tier: str, table: str) -> str:
    """`bronze$events` on a shared estate, `acme-bronze$events` for a tenant — the names lineage records."""
    return f"{PROJECT}-{tier}${table}" if PROJECT else f"{tier}${table}"


# Governed lineage READS use the app-token SERVICE door as `service-web` (a warehouse reader — the same
# read-only identity the web BFF uses). Auth-off → OIDC off → authenticate() pass-through (harmless);
# auth-on → this is what lets the reads through instead of a 401.
_LINEAGE_HEADERS = {"dapr-api-token": DAPR_TOKEN, "x-lance-service-identity": "service-web"} if DAPR_TOKEN else {}

pytestmark = [pytest.mark.e2e, pytest.mark.medallion]


@pytest.fixture(scope="module")
def urls() -> tuple[str, str]:
    if not (LANCERAY and LINEAGE):
        pytest.skip("set LANCE_E2E_LANCERAY_URL and LANCE_E2E_LINEAGE_URL (see module docstring)")
    for name, url in (("medallion-producer", LANCERAY), ("lineage", LINEAGE)):
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


def test_produce_cascades_bronze_to_gold(urls: tuple[str, str]) -> None:
    lance_ray, lineage = urls

    # Snapshot the run count FIRST. gold's upstream set may already exist from earlier produces, so
    # set-membership alone can't prove THIS trigger did anything — the graph would look identical if the
    # cascade silently no-op'd. A fresh produce mints a new run per stage (producer + 2 movers = +3), so a
    # strictly rising run count is the real "the cascade fired just now" signal.
    before = _run_count(lineage)

    # ACT — one trigger at the head of the pipeline (carrying the app-token when the stack enforces it).
    headers = {"dapr-api-token": DAPR_TOKEN} if DAPR_TOKEN else {}
    params = {"project": PROJECT} if PROJECT else {}
    produced = requests.post(f"{lance_ray}/produce", headers=headers, params=params, timeout=8)
    assert produced.status_code == 202 and produced.json()["status"] == "produced", produced.text

    # ASSERT — the cascade reached gold (its transitive upstream is the full chain) AND it did so from THIS
    # produce: all three stages emitted a fresh run, so the count grew by the producer + 2 movers.
    gold = _qualified("gold", "catalog")
    chain = {_qualified("bronze", "events"), _qualified("silver", "features")}
    deadline = time.monotonic() + 60.0
    upstream: list[str] = []
    #: The last non-200 the read door gave, so a REFUSAL is not reported as an absent cascade. This test
    #: swallowed every non-200 and failed with `upstream=[]`, which reads as "the cascade never ran" —
    #: while the actual answer was `403 can_get_metadata required on table:acme-gold$catalog`, the
    #: lineage plane correctly refusing a reader with no grant on that tenant. A live drive that cannot
    #: tell "it did not happen" from "you may not look" sends the next reader after the wrong bug.
    refusal = ""
    while time.monotonic() < deadline:
        resp = requests.get(f"{lineage}/datasets/{gold}/upstream", headers=_LINEAGE_HEADERS, timeout=8)
        if resp.status_code == 200:
            upstream = [ref["name"] for ref in resp.json().get("related", [])]
            if chain <= set(upstream) and _run_count(lineage) >= before + 3:
                return
        else:
            refusal = f" [read door answered {resp.status_code}: {resp.text[:200]}]"
        time.sleep(3)
    pytest.fail(f"{gold} cascade did not complete within 60s (upstream={upstream}, runs {before}->{_run_count(lineage)}, expected >= {before + 3}){refusal}")
