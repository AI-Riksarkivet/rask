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

    **A HOLD outranks promoting, and that ordering only became available with a gate-only publish.**
    Under `cascade_via_publish` the publish IS the promotion — the catalog runs the assertions and its
    tag move wakes the next stage — so for a while these two properties could not both hold: deciding
    the review first made the band able to act but left it unable to name the assertions it was
    reviewing, and deciding after preserved those names but had already promoted.

    `publication.gate` separates the verdict from the act. The caller asks the catalog what it WOULD
    say, with the tag untouched, and arrives here already holding `failed_assertions`. So a corrupt
    finding blocks with its names intact, an unusual-but-valid promotion can be withheld for a person,
    and neither costs the other.

    **Otherwise the stage promotes** by whichever mechanism drives this estate, and a stage with
    neither a publish target nor a topic is terminal rather than broken.
    """
    if failed_assertions:
        return GateOutcome.BLOCK
    if band_reasons:
        return GateOutcome.HOLD
    if cascade_via_publish and has_target:
        return GateOutcome.PUBLISH
    if has_pub_topic:
        return GateOutcome.TRIGGER
    return GateOutcome.NOTHING
