"""Lineage asks for a delivery again for at least as long as the estate rides a secret-store outage ([[LH-605]]).

A signer keeps its key while its store does not answer for one login lease and one re-read of the key (ADR 0203).
Lineage reads a producer's public keys again after 60 s, so in that time it verifies no delivery: each is asked for
again, and parked on its last. So the lane's attempts and delay cover that outage, and the render refuses numbers that
do not.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from lakehouse.governed.signing_key import MAX_REFRESH_SECONDS
from tests.chart.chart_render import REPO, containers, env_of, render, render_chart_text, store_identities


_BASE = ("--set", "image.localImages=true")
_LEASE_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}


def test_the_default_lane_outlasts_the_lease_and_the_key_re_read() -> None:
    docs = render(*_BASE)
    (lineage,) = [container for name, process, container in containers(docs) if f"{name}/{process}" == "Deployment/rask-lineage/lineage"]
    told = env_of(lineage)
    # The lakehouse's own identities: the ones whose keys sign what lineage verifies.
    leases = [str(json.loads(role)["token_ttl"]) for key, role in store_identities(docs).items() if key.startswith("role-rask-")]

    ridden = (int(told["LINEAGE_DELIVERY_ATTEMPTS"]) - 1) * float(told["LINEAGE_DELIVERY_RETRY_SECONDS"])

    assert ridden >= max(float(lease[:-1]) * _LEASE_UNIT_SECONDS[lease[-1]] for lease in leases) + MAX_REFRESH_SECONDS + 60


#: What the render holds the lane to at the default lease of 15 minutes: the lease, the key's re-read and a minute.
_OUTAGE_SECONDS = 900 + int(MAX_REFRESH_SECONDS) + 60


@pytest.mark.parametrize(
    ("ridden", "refused"),
    [pytest.param(_OUTAGE_SECONDS, False, id="exactly the outage"), pytest.param(_OUTAGE_SECONDS - 1, True, id="a second short of it")],
)
def test_a_lane_is_refused_exactly_when_it_parks_before_the_outage_ends(ridden: int, refused: bool) -> None:
    lane = ("--set", "services.lineage.delivery.attempts=2", "--set", f"services.lineage.delivery.retrySeconds={ridden}")
    try:
        render_chart_text(REPO / "chart", *_BASE, *lane)
        answer = ""
    except subprocess.CalledProcessError as error:
        answer = error.stderr

    assert (f"rides out {ridden} s ((attempts - 1) x retrySeconds), less than the {_OUTAGE_SECONDS} s" in answer) is refused
