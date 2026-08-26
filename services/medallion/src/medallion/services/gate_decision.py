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
    UNGOVERNED = "ungoverned"  # no catalog at all: nothing may advance a tag, and nothing will
    MISCONFIGURED = "misconfigured"  # a catalog and a downstream, but no target to publish
    NOTHING = "nothing"  # terminal (gold has no downstream) — nothing to fire, nothing to refuse


def gate_decision(
    *,
    failed_assertions: Sequence[str],
    band_reasons: Sequence[str],
    has_target: bool,
    has_catalog: bool,
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

    **Otherwise the stage promotes through the catalog, which is the only door.** `publication.py`
    states the rule: every writer must publish the same way or each reimplements the contract and
    they drift, and it is the only place a concurrent advance is detectable, because
    `UpdateTableTag` returns `ConcurrentModification` while a tag file has no format-level CAS.

    **There used to be a second door.** `TRIGGER` fired the next stage's topic from the mover,
    promoting without the catalog ruling, and a flag (`MEDALLION_CASCADE_VIA_PUBLISH`, default
    False) chose between them — so the DEFAULT deployment used the door that module says must not
    exist. Both the outcome and the flag are gone.

    **PUBLISHING NEEDS A CATALOG, and that precondition has to be stated here now.** It used to be
    carried by `MEDALLION_CASCADE_VIA_PUBLISH`, whose settings validator refused the flag without a
    reachable catalog — so while two doors existed, the publish branch could never be reached without
    one. Deleting the flag deleted that guard with it, and `has_target` does not replace it:
    a target is a table id and a committed version, neither of which implies anything can move a tag.
    `publish_stage_output` RAISES on an empty catalog URL, so without this the ungoverned deployment
    the estate supports answered every trigger RETRY, forever, on a redelivery that cannot set an env
    var.

    **Two ways to have no door, and they are different facts.** `UNGOVERNED` is a whole deployment
    with no catalog: the write already warned (`medallion_stage_output_UNGOVERNED`, with a span
    attribute to alert on), the bytes landed, and nothing will promote because nothing CAN. It is a
    supported mode, so it ACKS. `MISCONFIGURED` is a governed deployment that has a catalog and a
    downstream topic and no target to publish — which the chart cannot render, so it means someone
    built a stage that can never advance. Collapsing them would report a dev laptop and a broken
    production stage as the same thing.

    `NOTHING` keeps its silence because it is not an error — gold has no downstream, so there is
    genuinely nothing to fire, catalog or not.
    """
    if failed_assertions:
        return GateOutcome.BLOCK
    if band_reasons:
        return GateOutcome.HOLD
    if has_target and has_catalog:
        return GateOutcome.PUBLISH
    # Terminal BEFORE the two no-door cases: gold has no downstream, so neither a missing catalog nor
    # a missing target is a fact about it. Reporting a terminal stage as ungoverned would warn on
    # every gold write in a dev estate.
    if not has_pub_topic:
        return GateOutcome.NOTHING
    if not has_catalog:
        return GateOutcome.UNGOVERNED
    return GateOutcome.MISCONFIGURED


#: The promotion verdict each outcome represents, for the `lance.promotion_status` run facet.
#:
#: Only outcomes that actually REFUSED a promotion appear. `MISCONFIGURED` is deliberately absent: a
#: stage with a downstream and no publish target is a DEPLOYMENT fault, and giving it a promotion
#: status would file an operator's mistake in the same column as a data-quality decision, where no
#: validator can act on it. `UNGOVERNED` and `NOTHING` refuse nothing, so they have no verdict.
#:
#: `PUBLISH` maps to REFUSED rather than BLOCKED because the refuser is different and so is the
#: remedy: the gate allowed the promotion and the CATALOG's own gate declined it.
_PROMOTION_STATUS: dict[GateOutcome, str] = {
    GateOutcome.HOLD: "HELD",
    GateOutcome.BLOCK: "BLOCKED",
    GateOutcome.PUBLISH: "REFUSED",
}


def promotion_status_for(outcome: GateOutcome | None) -> str | None:
    """The verdict to stamp on the run facet, or ``None`` when the outcome is not a verdict.

    The run board renders a HOLD identically to a hard failure today, because the only thing crossing
    the wire is ``eventType=FAIL``. This is the one bit that tells them apart — and it distinguishes
    "a person may approve this" from "no approval can fix this", which are opposite instructions.
    """
    if outcome is None:
        return None
    return _PROMOTION_STATUS.get(outcome)


def refusal_message(outcome: GateOutcome | None, to_dataset: str) -> str:
    """What a person is told when a promotion did not happen — named by its ACTUAL cause.

    Every refusal used to emit one hardcoded sentence, "quality gate HELD the promotion into <x>",
    whatever the outcome. Three of the four were wrong and one was harmful: `BLOCK` is defined as
    "corrupt, and no approval makes it right", and reporting it as HELD sends an operator looking for
    a validator who cannot help. Measured on the live compute board 2026-08-26, where every refusal —
    including a catalog 404 — read as a hold.
    """
    match outcome:
        case GateOutcome.HOLD:
            return f"quality gate HELD the promotion into {to_dataset} — a validator must approve it; downstream was not triggered"
        case GateOutcome.BLOCK:
            return f"quality gate BLOCKED the promotion into {to_dataset} — a failed assertion, which no approval can waive; downstream was not triggered"
        case GateOutcome.PUBLISH:
            return f"the catalog REFUSED the promotion into {to_dataset} — downstream was not triggered"
        case GateOutcome.MISCONFIGURED:
            return f"the stage has a downstream but no publish target, so the promotion into {to_dataset} could not be attempted"
        case _:
            return f"the promotion into {to_dataset} did not happen — downstream was not triggered"
