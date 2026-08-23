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
    pytest.fail(
        f"{gold} cascade did not complete within 60s (upstream={upstream}, runs {before}->{_run_count(lineage)}, "
        f"expected >= {before + 3}){refusal}{_projectless_diagnosis(lineage, upstream)}"
    )


def _projectless_diagnosis(lineage: str, upstream: list[str]) -> str:
    """Name the INVOCATION fault when the shape of the failure is that fault's signature.

    Same reason `refusal` exists one block up: a live drive that cannot tell "it did not happen" from
    "you may not look" sends the next reader after the wrong bug. This is the third such case, and the
    only one that is not about the estate at all — it is about how the test was invoked.

    With `medallion.cascadeViaPublish` on, the cascade is driven by `publication_trigger`, which ALWAYS
    carries a project because "the mover cannot resolve its tiers without it". So a PROJECTLESS produce
    against a publish-driven estate publishes silver and gold never fires — and the bare run-count
    message reports that as a broken cascade, which is what it did on the drive that produced
    `runs 131->133, expected >= 134` against a cascade working perfectly for a tenant.

    The signature is specific enough to name: no `LANCE_E2E_PROJECT`, and SILVER reached bronze while
    gold reached nothing. A genuinely broken cascade does not stop cleanly at exactly that boundary,
    and a tenant drive cannot produce it at all. Anything else gets no hint rather than a guess —
    a wrong diagnosis is worse than none, because it is the one the next reader will follow.
    """
    if PROJECT or upstream:
        return ""
    try:
        silver = _qualified("silver", "features")
        resp = requests.get(f"{lineage}/datasets/{silver}/upstream", headers=_LINEAGE_HEADERS, timeout=8)
        reached = resp.status_code == 200 and _qualified("bronze", "events") in [ref["name"] for ref in resp.json().get("related", [])]
    except requests.RequestException:
        return ""
    return projectless_hint(project=PROJECT, gold_upstream=upstream, silver_reached=reached)


def projectless_hint(*, project: str, gold_upstream: list[str], silver_reached: bool) -> str:
    """The decision, separated from the read that feeds it, so every branch is reachable in a test.

    Same reason `gate_decision` was extracted from `run_stage`: a diagnosis that can only be exercised
    by standing up a cluster and driving it wrong is a diagnosis nobody re-checks — and this one exists
    precisely for the person who is already having a bad time.
    """
    if project or gold_upstream or not silver_reached:
        return ""
    return (
        "\n  DIAGNOSIS: silver completed and gold never fired, with no LANCE_E2E_PROJECT set. On an estate "
        "with medallion.cascadeViaPublish ON, the cascade is driven by publication_trigger, which always "
        "carries a project — a projectless produce publishes silver and stops. Re-run with "
        "LANCE_E2E_PROJECT=<tenant>. This is an invocation fault, not a broken cascade."
    )


def test_the_projectless_signature_is_named() -> None:
    """Silver reached, gold empty, no project — the one shape that IS the invocation fault."""
    assert "LANCE_E2E_PROJECT" in projectless_hint(project="", gold_upstream=[], silver_reached=True)


def test_a_tenant_drive_gets_no_hint() -> None:
    """A drive that named a project cannot be suffering this fault, so guessing would mislead."""
    assert projectless_hint(project="acme", gold_upstream=[], silver_reached=True) == ""


def test_a_cascade_that_did_not_even_reach_silver_gets_no_hint() -> None:
    """Stopping BEFORE silver is a different failure; naming this one would send the reader wrong.

    A wrong diagnosis is worse than none, because it is the one the next reader will follow.
    """
    assert projectless_hint(project="", gold_upstream=[], silver_reached=False) == ""


def test_a_partially_reached_gold_gets_no_hint() -> None:
    """Any gold upstream at all means the cascade DID fire — a slow run, not a projectless one."""
    assert projectless_hint(project="", gold_upstream=["bronze$events"], silver_reached=True) == ""
