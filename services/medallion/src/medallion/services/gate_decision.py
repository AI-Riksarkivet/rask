"""Which door a finished stage takes. Pure, so every ordering case is reachable in a test.

This was an `if/elif` chain inside `run_stage`, wrapped in a Lance write, a lineage emit and an HTTP
publish — and one ordering bug lived in it for as long as `cascadeViaPublish` has been on: the publish
branch came first, so the band `elif` beneath it never ran. Measured 2026-08-23 against the live
estate with `MEDALLION_PROMOTION_REVIEW_BAND=0` — the value documented as "asks about every change" —
a full cascade raised no review at all. Nothing was red, because the chain was syntactically fine and
the branch was simply unreachable.

Extracted rather than reordered in place: the ordering IS the policy, and a policy that can only be
exercised by standing up a mover, a catalog and an object store is a policy nobody re-checks.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum


class GateOutcome(StrEnum):
    """What happens to a stage's output once it is written."""

    BLOCK = "block"  # a failed assertion: corrupt, and no approval makes it right
    HOLD = "hold"  # a band breach: unusual rather than broken, so a person is asked
    PUBLISH = "publish"  # let the catalog's gate rule, and its tag move is the trigger
    TRIGGER = "trigger"  # fire the next-stage topic directly
    NOTHING = "nothing"  # terminal (gold has no downstream) — nothing to fire, nothing to refuse


def gate_decision(
    *,
    failed_assertions: Sequence[str],
    band_reasons: Sequence[str],
    cascade_via_publish: bool,
    has_target: bool,
    has_pub_topic: bool,
) -> GateOutcome:
    """The one place the promotion order is decided.

    **A block outranks a hold.** A failed assertion is a verdict — the data is wrong whether or not a
    reviewer exists — while a band breach is a question. Letting the question win would park a corrupt
    batch on an approval that should never be offered for it.

    **A band HOLD outranks a trigger, but not a publish — and that is a conflict, not a preference.**
    Where a topic drives the cascade, withholding the trigger genuinely withholds the promotion, so the
    hold acts. Where PUBLISHING drives it, the publish IS the promotion: the catalog runs the assertions
    and its tag move wakes the next stage. Holding first would make the band work at the cost of the
    catalog's verdict never reaching the review — and a hold that cannot name its assertions cannot tell
    a corrupt finding from a reviewable one, which is a property `test_cascade_via_publish.py` defends.

    Both cannot hold at once without a GATE-ONLY publish the catalog does not offer: assertions
    evaluated without moving the tag. Until that exists the band is subordinate on the publish path, and
    the consequence is stated here rather than hidden — **a publish-driven estate cannot hold an
    unusual-but-valid promotion.** That is a catalog change, not an ordering one.

    **Otherwise the stage promotes** by whichever mechanism drives this estate, and a stage with
    neither a publish target nor a topic is terminal rather than broken.
    """
    if failed_assertions:
        return GateOutcome.BLOCK
    # PUBLISH outranks a band HOLD, and that is a conflict rather than a preference — see the module
    # docstring. Holding first makes the band work but stops the catalog's verdict ever reaching the
    # review, which `test_a_refused_publish_stops_the_cascade_and_names_its_assertions` defends: a hold
    # that cannot name its assertions cannot tell a corrupt finding from a reviewable one.
    if cascade_via_publish and has_target:
        return GateOutcome.PUBLISH
    if band_reasons:
        return GateOutcome.HOLD
    if has_pub_topic:
        return GateOutcome.TRIGGER
    return GateOutcome.NOTHING
