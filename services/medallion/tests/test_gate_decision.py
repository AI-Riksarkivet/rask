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
from medallion.services.gate_decision import GateOutcome, gate_decision


def _decide(
    *,
    failed_assertions: Sequence[str] = (),
    band_reasons: Sequence[str] = (),
    cascade_via_publish: bool = True,
    has_target: bool = True,
    has_pub_topic: bool = False,
) -> GateOutcome:
    """A clean publishing stage, with one aspect varied per test."""
    return gate_decision(
        failed_assertions=failed_assertions,
        band_reasons=band_reasons,
        cascade_via_publish=cascade_via_publish,
        has_target=has_target,
        has_pub_topic=has_pub_topic,
    )


def test_a_clean_stage_publishes_when_publishing_drives_the_cascade() -> None:
    assert _decide() is GateOutcome.PUBLISH


def test_a_clean_stage_triggers_when_a_topic_drives_the_cascade() -> None:
    assert _decide(cascade_via_publish=False, has_pub_topic=True) is GateOutcome.TRIGGER


def test_a_failed_assertion_blocks() -> None:
    assert _decide(failed_assertions=("key_non_null",), cascade_via_publish=False) is GateOutcome.BLOCK


def test_a_band_breach_holds() -> None:
    assert _decide(band_reasons=("row_delta",)) is GateOutcome.HOLD


def test_the_band_is_reached_even_when_publishing_drives_the_cascade() -> None:
    """The regression this file exists for. With the band as an `elif` below the publish branch, this
    returned PUBLISH and the review machinery never ran."""
    assert _decide(band_reasons=("row_delta",), cascade_via_publish=True, has_target=True) is GateOutcome.HOLD


def test_a_block_outranks_a_hold() -> None:
    """Corrupt data is a verdict; an unusual delta is a question. A question must not displace it."""
    assert _decide(failed_assertions=("key_non_null",), band_reasons=("row_delta",)) is GateOutcome.BLOCK


def test_a_hold_outranks_promoting() -> None:
    """Stated separately from the case above because it is the ORDERING that matters: the hold has to
    be decided before the promotion, not discovered after the tag moved."""
    assert _decide(band_reasons=("first_promotion",), has_pub_topic=True) is GateOutcome.HOLD


def test_no_target_and_no_topic_is_terminal() -> None:
    """Gold has no next stage; there is nothing to fire and nothing to refuse."""
    assert _decide(cascade_via_publish=True, has_target=False, has_pub_topic=False) is GateOutcome.NOTHING


@pytest.mark.parametrize("outcome", list(GateOutcome))
def test_every_outcome_is_reachable(outcome: GateOutcome) -> None:
    """A branch no input can produce is dead code wearing a name."""
    reachable = {
        _decide(failed_assertions=("x",)),
        _decide(band_reasons=("x",)),
        _decide(),
        _decide(cascade_via_publish=False, has_pub_topic=True),
        _decide(cascade_via_publish=True, has_target=False),
    }
    assert outcome in reachable
