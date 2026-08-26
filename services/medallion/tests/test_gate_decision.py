"""Which door a finished stage takes — extracted from the IO shell so every case is reachable.

The gate lived as an `if/elif` chain inside `run_stage`, wrapped in a Lance write, a lineage emit and
an HTTP publish, and one ordering bug hid in it for as long as `cascadeViaPublish` has been on: the
publish branch is first, so the band `elif` below it NEVER RAN. Measured 2026-08-23 on the live
estate — `MEDALLION_PROMOTION_REVIEW_BAND=0`, the value documented as "asks about every change", and
a full cascade raised no review at all.

Ordering is the whole content of this function, so it is stated once, here, and tested exhaustively:

* a FAILED ASSERTION blocks, and outranks everything — corrupt data is wrong whether or not anyone is
  available to ask, and a question must never displace a verdict;
* a BAND BREACH holds, and must be decided BEFORE the promotion rather than after it. Under
  `cascadeViaPublish` the publish IS the promotion: a breach noticed once the tag has moved cannot
  un-move it, so a band that runs after the publish is not a late check, it is no check;
* otherwise the stage promotes — through the catalog's gate when publishing drives the cascade, or by
  firing the next-stage topic when a trigger does.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from medallion.services.gate_decision import GateOutcome, gate_decision, promotion_status_for, refusal_message


def _decide(
    *,
    failed_assertions: Sequence[str] = (),
    band_reasons: Sequence[str] = (),
    has_target: bool = True,
    has_catalog: bool = True,
    has_pub_topic: bool = False,
) -> GateOutcome:
    """A clean publishing stage, with one aspect varied per test."""
    return gate_decision(
        failed_assertions=failed_assertions,
        band_reasons=band_reasons,
        has_target=has_target,
        has_catalog=has_catalog,
        has_pub_topic=has_pub_topic,
    )


def test_a_clean_stage_publishes_when_publishing_drives_the_cascade() -> None:
    assert _decide() is GateOutcome.PUBLISH


def test_a_clean_stage_still_publishes_when_a_topic_also_exists() -> None:
    """There is ONE door. A downstream topic does not offer a second way to promote.

    This asserted TRIGGER while `GateOutcome.TRIGGER` existed -- the mover firing the next stage's
    topic itself, promoting without the catalog ruling. That was the second enforcement point
    `publication.py` exists to prevent, and it was the DEFAULT path because
    MEDALLION_CASCADE_VIA_PUBLISH defaulted False. The stage still promotes; it promotes through the
    catalog.
    """
    assert _decide(has_pub_topic=True) is GateOutcome.PUBLISH


def test_a_failed_assertion_blocks() -> None:
    assert _decide(failed_assertions=("key_non_null",)) is GateOutcome.BLOCK


def test_a_band_breach_holds_on_a_trigger_driven_cascade() -> None:
    assert _decide(band_reasons=("row_delta",), has_pub_topic=True) is GateOutcome.HOLD


def test_a_band_breach_holds_even_when_publishing_drives_the_cascade() -> None:
    """The case this file was written for, and it moved twice before settling.

    Under `cascade_via_publish` the publish IS the promotion: the catalog runs the assertions and its
    tag move wakes the next stage. That made two wanted properties look mutually exclusive — holding
    first let the band act but left the review unable to name the assertions it was reviewing, while
    publishing first kept those names and had already promoted. For a while this test asserted PUBLISH
    and recorded the cost.

    `publication.gate` removed the tradeoff rather than picking a side: the caller asks the catalog
    what it WOULD say, tag untouched, and reaches this function already holding `failed_assertions`.
    So a breach can hold, and a corrupt batch still blocks by name.
    """
    assert _decide(band_reasons=("row_delta",), has_target=True) is GateOutcome.HOLD


def test_a_block_outranks_a_hold() -> None:
    """Corrupt data is a verdict; an unusual delta is a question. A question must not displace it."""
    assert _decide(failed_assertions=("key_non_null",), band_reasons=("row_delta",)) is GateOutcome.BLOCK


def test_a_hold_outranks_a_trigger() -> None:
    """Where the band CAN act: a topic-driven cascade fires the next stage itself, so withholding the
    trigger genuinely withholds the promotion."""
    assert _decide(band_reasons=("first_promotion",), has_pub_topic=True) is GateOutcome.HOLD


def test_no_target_and_no_topic_is_terminal() -> None:
    """Gold has no next stage; there is nothing to fire and nothing to refuse."""
    assert _decide(has_target=False, has_pub_topic=False) is GateOutcome.NOTHING


def test_publishing_requires_a_catalog() -> None:
    """A target is a table id and a committed version. Neither implies anything can move a tag.

    This precondition used to be carried by `MEDALLION_CASCADE_VIA_PUBLISH`, whose settings validator
    refused the flag without a reachable catalog — so while two doors existed the publish branch could
    not be reached without one. Deleting the flag deleted the guard, and because
    `publish_stage_output` RAISES on an empty catalog URL, every ungoverned mover answered its trigger
    RETRY, forever, on a redelivery that cannot set an env var. Found by running the suite, not by
    reading the diff.
    """
    assert _decide(has_catalog=False, has_pub_topic=True) is GateOutcome.UNGOVERNED


def test_an_ungoverned_terminal_stage_is_still_just_terminal() -> None:
    """Gold has no downstream, so a missing catalog is not a fact about it.

    Ordering, not a special case: `NOTHING` is returned before either no-door branch. Otherwise every
    gold write on a dev laptop would warn that its promotion will not fire, when there was never a
    promotion to fire.
    """
    assert _decide(has_catalog=False, has_pub_topic=False) is GateOutcome.NOTHING


def test_a_catalog_and_a_downstream_but_no_target_is_MISCONFIGURED() -> None:
    """The chart cannot render this, which is exactly why it must be loud rather than terminal.

    Distinct from UNGOVERNED on purpose. That is a whole deployment with no catalog — a supported
    mode that acks. This is a governed deployment carrying a stage that can never advance, and
    collapsing the two would report a dev laptop and a broken production stage identically.
    """
    assert _decide(has_target=False, has_pub_topic=True) is GateOutcome.MISCONFIGURED


@pytest.mark.parametrize("outcome", list(GateOutcome))
def test_every_outcome_is_reachable(outcome: GateOutcome) -> None:
    """A branch no input can produce is dead code wearing a name."""
    reachable = {
        _decide(failed_assertions=("x",)),
        _decide(band_reasons=("x",), has_pub_topic=True),
        _decide(),
        _decide(has_pub_topic=True),
        _decide(has_target=False),
        _decide(has_target=False, has_pub_topic=True),
        _decide(has_catalog=False, has_pub_topic=True),
    }
    assert outcome in reachable


# --- what a refused promotion TELLS a person -------------------------------------------------
#
# `transform.py` collapsed four distinct outcomes onto one boolean (`quality_blocked`) and then
# emitted ONE hardcoded sentence for all of them: "quality gate HELD the promotion into <x>".
#
# That is wrong for three of the four, and the BLOCK case is actively harmful: `GateOutcome.BLOCK`
# is defined as "a failed assertion: corrupt, and no approval makes it right", yet it reported as
# HELD — which tells an operator to go find a validator to approve something no approval can fix.
# MEASURED on the live compute board 2026-08-26, where every refusal read "quality gate HELD".
#
# The verdict is also the thing the UI needs: the run board renders a hold identically to a
# failure, because the only signal crossing the wire is eventType=FAIL.


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        # The two genuine GATE VERDICTS get a promotion status, and they are opposites: one asks a
        # person, the other says no answer exists.
        (GateOutcome.HOLD, "HELD"),
        (GateOutcome.BLOCK, "BLOCKED"),
        # The catalog refused a publish the gate itself allowed — a verdict, but the catalog's.
        (GateOutcome.PUBLISH, "REFUSED"),
        # Not a verdict at all: a stage with a downstream and no publish target is a DEPLOYMENT
        # fault. Giving it a promotion status would put an operator's misconfiguration in the same
        # column as a data-quality decision, and no validator can act on it.
        (GateOutcome.MISCONFIGURED, None),
        # Neither of these refuses anything, so neither has a verdict to report.
        (GateOutcome.UNGOVERNED, None),
        (GateOutcome.NOTHING, None),
    ],
)
def test_each_outcome_reports_its_own_promotion_verdict(outcome: GateOutcome, expected: str | None) -> None:
    assert promotion_status_for(outcome) == expected


def test_a_block_never_claims_a_person_can_approve_it() -> None:
    """The sharp end: BLOCK and HOLD must not read alike, because the actions they invite differ."""
    blocked = refusal_message(GateOutcome.BLOCK, "silver$features")
    held = refusal_message(GateOutcome.HOLD, "silver$features")

    assert "HELD" not in blocked, f"a corrupt batch was reported as held for approval: {blocked!r}"
    assert "BLOCKED" in blocked
    assert "HELD" in held
    assert "silver$features" in blocked and "silver$features" in held
    assert blocked != held


def test_every_refusing_outcome_has_a_message_that_names_its_dataset() -> None:
    """No outcome may fall through to a generic sentence — that is the bug, one layer down."""
    for outcome in (GateOutcome.BLOCK, GateOutcome.HOLD, GateOutcome.PUBLISH, GateOutcome.MISCONFIGURED):
        message = refusal_message(outcome, "silver$features")
        assert "silver$features" in message, f"{outcome} produced a message that does not name the target: {message!r}"
        assert message.strip(), f"{outcome} produced an empty message"
