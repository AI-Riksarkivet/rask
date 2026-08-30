"""Promotion has exactly ONE enforcement point: the catalog's tag move.

`catalog/services/publication.py` states the rule it exists to protect -- "The ingest plane, a Ray
job, a backfill script and a person with catalog credentials must all publish the same way, or each
reimplements the contract and they drift." It is also the only door that can detect a concurrent
advance, because `UpdateTableTag` returns `ConcurrentModification` while a tag file carries no
format-level CAS.

`GateOutcome.TRIGGER` was a SECOND enforcement point: the mover fired the next stage's topic itself,
promoting without the catalog ruling. Two doors, one contract, and the flag deciding which one ran
(`MEDALLION_CASCADE_VIA_PUBLISH`, default False) meant the DEFAULT deployment used the door the
module says must not exist.

WHAT REPLACES IT IS NOT A SECOND FALLBACK. A stage that has a downstream topic but no publish target
cannot promote through the only sanctioned door -- that is a MISCONFIGURATION, and the estate's
lesson from 2026-08-24 is that this class must be loud. Three defects that day (`706c8ce3`,
`2e308f7f`, and a lane pointed at another tenant's namespace) had one symptom in common: silence.
So it returns `MISCONFIGURED`, which the caller records; it does not quietly do nothing and it does
not quietly fire a topic.

`NOTHING` keeps its meaning and its silence, because it is not an error: gold has no downstream, so
there is genuinely nothing to fire.

AND PUBLISHING NEEDS A CATALOG, which is the precondition the flag was silently carrying. Its settings
validator refused `MEDALLION_CASCADE_VIA_PUBLISH` without a reachable catalog, so while two doors
existed the publish branch could not be reached without one. Deleting the flag deleted that guard, and
`publish_stage_output` RAISES on an empty catalog URL -- so the ungoverned deployment the estate
supports answered every trigger RETRY, forever, on a redelivery that cannot set an env var. That is
`UNGOVERNED`: a whole deployment with no catalog, distinct from `MISCONFIGURED`, which is a GOVERNED
deployment carrying a stage that can never advance. Collapsing them would report a dev laptop and a
broken production stage as the same thing.
"""

from __future__ import annotations

import pytest

from medallion.services.gate_decision import GateOutcome, gate_decision


def _decide(**over: object) -> GateOutcome:
    base: dict[str, object] = {
        "failed_assertions": [],
        "band_reasons": [],
        "has_target": True,
        "has_catalog": True,
        "has_pub_topic": True,
    }
    base.update(over)
    return gate_decision(**base)  # type: ignore[arg-type]


def test_there_is_no_trigger_outcome_at_all() -> None:
    """The second door is gone from the vocabulary, not merely unreachable."""
    assert not hasattr(GateOutcome, "TRIGGER")
    assert {o.value for o in GateOutcome} == {"block", "hold", "publish", "ungoverned", "misconfigured", "nothing"}


def test_a_promotable_stage_always_publishes() -> None:
    """No flag decides which door runs. There is one door."""
    assert _decide() is GateOutcome.PUBLISH


def test_a_block_still_outranks_a_hold() -> None:
    """Unchanged ordering: a failed assertion is a verdict, a band breach is a question."""
    assert _decide(failed_assertions=["not_null"], band_reasons=["band"]) is GateOutcome.BLOCK


def test_a_hold_still_outranks_publishing() -> None:
    assert _decide(band_reasons=["row count moved"]) is GateOutcome.HOLD


def test_a_downstream_with_no_publish_target_is_LOUD() -> None:
    """The case that used to fire a topic. It cannot promote, so it must say so."""
    assert _decide(has_target=False) is GateOutcome.MISCONFIGURED


def test_terminal_is_still_silent_because_it_is_not_an_error() -> None:
    """Gold has no downstream. Nothing to fire is not the same as unable to fire."""
    assert _decide(has_target=False, has_pub_topic=False) is GateOutcome.NOTHING


@pytest.mark.parametrize("has_pub_topic", [True, False])
def test_a_block_wins_regardless_of_downstream_shape(has_pub_topic: bool) -> None:
    """Corrupt data never promotes and never mis-reports as a config problem."""
    assert _decide(failed_assertions=["x"], has_target=False, has_pub_topic=has_pub_topic) is GateOutcome.BLOCK


def test_publishing_requires_a_catalog_and_an_ungoverned_estate_ACKS() -> None:
    """The precondition the deleted flag was carrying, and the failure it caused when it went.

    A target is a table id plus a committed version; neither implies anything can move a tag.
    `publish_stage_output` raises on an empty catalog URL, so without this the mover returned RETRY on
    every trigger of an ungoverned deployment -- a mode the estate supports and pins elsewhere
    (`test_an_ungoverned_deployment_still_uses_its_configured_URI`). Redelivery cannot set an env var,
    so retrying is the one answer guaranteed never to make progress.
    """
    assert _decide(has_catalog=False) is GateOutcome.UNGOVERNED


def test_ungoverned_and_misconfigured_are_NOT_the_same_fact() -> None:
    """One is a supported deployment; the other is a stage that can never advance."""
    assert _decide(has_catalog=False, has_target=True) is GateOutcome.UNGOVERNED
    assert _decide(has_catalog=True, has_target=False) is GateOutcome.MISCONFIGURED


def test_a_terminal_stage_is_terminal_even_with_no_catalog() -> None:
    """Ordering: gold has no downstream, so a missing catalog is not a fact about it.

    Without this ordering every gold write on a catalog-less dev estate would report that its
    promotion will not fire, when there was never a promotion to fire.
    """
    assert _decide(has_catalog=False, has_pub_topic=False) is GateOutcome.NOTHING
