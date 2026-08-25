"""Clear the promotion hold a cascade drive is waiting on, so a suite proves the CASCADE.

WHY A SUITE NEEDS THIS. An estate may run human-in-the-loop promotion review
(``MEDALLION_QUALITY_REVIEW_ENABLED``). When it does, a **first** promotion holds every single time:
the review band compares a stage's row count against its predecessor's, a first promotion has no
predecessor, and ``FIRST_PROMOTION`` reads as a breach. The mover publishes, the gate holds, and a
``promotion_review`` workflow waits for a human who is not coming. The suite then times out and
reports "the cascade did not complete", which is the wrong sentence — the cascade completed and
governance stopped it.

Measured 2026-08-25: `medallion`, `media` and `governed_union` all failed this way at once, on an
estate where every hop had actually run and every Ray job had SUCCEEDED.

WHY APPROVE RATHER THAN DISABLE. Approving exercises the real governance loop — the hold, the
approver's identity, the decision door, the resumed cascade — instead of configuring it away. A drive
that turns review off proves less than one that satisfies it. ``LANCE_E2E_APPROVE_HOLDS=0`` is there
for the estate that runs review OFF (nothing to approve) or for a suite that wants to ASSERT the hold.

THE ID IS DERIVED, NOT DISCOVERED. ``promotions.instance_for`` is ``f"promotion-{token}"`` and the
producer exposes no list endpoint — deliberately, per its docstring: "it is the only handle either
side has: the mover publishes a hold and moves on, and the door receives an id from a URL". So a
caller that knows its own cascade token knows its own hold, and can clear no one else's.
"""

from __future__ import annotations

import os

import requests


#: Approve by default. Set to 0/false/no to leave holds standing — correct on an estate with review
#: OFF (there is nothing to approve) or in a suite whose subject IS the hold.
APPROVE_HOLDS = os.environ.get("LANCE_E2E_APPROVE_HOLDS", "1").strip().lower() not in {"0", "false", "no", ""}


def instance_for(token: str) -> str:
    """Mirror of ``medallion.api.promotions.instance_for`` — the id is derived from the run token."""
    return f"promotion-{token}"


def approve_if_held(producer: str, token: str, admin_token: str, *, timeout: float = 10.0) -> bool:
    """Approve the promotion this ``token`` is holding, if there is one. True when something was approved.

    Silent about everything else on purpose. A 404 means no hold — the ordinary case on an estate with
    review off, or on a second promotion that has a predecessor to compare against — and is not worth a
    line of output. Only a hold that EXISTS and could not be cleared is interesting, and the caller
    sees that as its own timeout with the hold still standing.

    ``admin_token`` must be the configured approver: the decision door gates on it, and a shared
    service credential deliberately cannot approve its own output.
    """
    if not (APPROVE_HOLDS and admin_token and token):
        return False

    base = producer.rstrip("/")
    instance = instance_for(token)
    headers = {"Authorization": f"Bearer {admin_token}"}
    try:
        held = requests.get(f"{base}/promotions/{instance}", headers=headers, timeout=timeout)
        if held.status_code != 200:
            return False
        decided = requests.post(
            f"{base}/promotions/{instance}/decision",
            json={"approved": True},
            headers=headers,
            timeout=timeout,
        )
    except (requests.ConnectionError, requests.Timeout):
        # A dropped port-forward packet is not a verdict. The caller is polling; it will come back.
        return False
    return decided.status_code == 202
