"""The Ray lane asked about EVERY promotion, because it could never see a predecessor.

`promotion_band.review_reasons` treats `previous_row_count=None` as FIRST_PROMOTION and asks. That is
the right default for an unknown history — "a dataset we cannot read the history of gets a person's
attention instead of a silent promote" — but on the Ray lane it was not an unknown history, it was a
structural blind spot: the job writes OUT-OF-PROCESS, so by the time the mover measures in pass 2 the
predecessor is already overwritten and `WriteResult.previous_row_count` is None on every single run.

A review that fires every time is the same as no review. The signal it exists to carry — THIS promotion
is unusual — cannot be read out of a stream where every promotion is flagged, so the band was
effectively off for the entire Ray lane while looking configured.

The fix is the one transform.py's own comment asked for: pass 1 measures the destination in the last
moment the predecessor exists and hands the count forward on the trigger, exactly as it already hands
forward `event_time` (R26).

NOT `version - 1` at measure time, and that is the trap this pins shut. The writer commits data and
the lineage index SEPARATELY, so the reported version is N+1 and `version - 1` is the commit that
already holds the new rows — a structurally zero delta. It was silently wrong in production: 8 -> 200
and then 200 -> 1000 both published without ever asking.
"""

from __future__ import annotations

import pytest
from medallion.services.promotion_band import FIRST_PROMOTION, ROW_DELTA, resolve_previous_row_count, review_reasons
from medallion.services.trigger_guards import StageTrigger, parse_stage_trigger


BAND = 0.25


def _band(*, row_count: int, from_writer: int | None, from_trigger: int | None) -> list[str]:
    """The REAL resolution transform.py performs — the shipped function, not a copy of its logic.

    Reimplementing the conditional here would let this suite stay green while production regressed,
    which is the specific way a test about a two-source fallback fails to be worth anything.
    """
    previous = resolve_previous_row_count(observed=from_writer, carried=from_trigger)
    return review_reasons(row_count=row_count, previous_row_count=previous, band=BAND)


def test_the_trigger_can_carry_a_predecessor_count() -> None:
    """The field itself. Without it the Ray lane has nowhere to put pass 1's measurement."""
    assert "pre_row_count" in StageTrigger.model_fields
    assert StageTrigger().pre_row_count is None, "absent must mean unknown, not zero"


def test_an_unremarkable_ray_promotion_no_longer_ASKS() -> None:
    """The defect, stated as the behaviour it produced: 100 -> 104 rows is a 4% move on a 25% band.

    Before the carried count this asked, because the writer saw no predecessor. Asking about a 4%
    change on every run is what made the band unreadable.
    """
    assert _band(row_count=104, from_writer=None, from_trigger=100) == []


def test_a_genuinely_unusual_ray_promotion_still_asks() -> None:
    """The other half — the fix must not silence the band, only stop it firing on everything."""
    assert _band(row_count=400, from_writer=None, from_trigger=100) == [ROW_DELTA]


def test_the_WRITER_wins_over_the_carried_count() -> None:
    """In-process writes observe the predecessor directly; the trigger's is pass 1's older reading.

    Ordering matters when they disagree: the writer measured the version it actually replaced.
    """
    assert _band(row_count=104, from_writer=100, from_trigger=1) == [], "the stale trigger value won"
    assert _band(row_count=104, from_writer=1, from_trigger=100) == [ROW_DELTA], "the writer's value was ignored"


def test_both_absent_is_still_a_FIRST_PROMOTION() -> None:
    """An unreadable destination must keep asking — the fix removes a blind spot, not the safe default."""
    assert _band(row_count=104, from_writer=None, from_trigger=None) == [FIRST_PROMOTION]


def test_a_zero_predecessor_is_a_first_promotion_not_a_division() -> None:
    """An empty predecessor gives nothing to compare against; dividing by it manufactures infinity."""
    assert _band(row_count=104, from_writer=None, from_trigger=0) == [FIRST_PROMOTION]


def test_a_trigger_from_an_OLDER_build_still_validates() -> None:
    """Additive-only evolution (DATA-CONTRACT §7.4): a payload predating the field must not DROP.

    The rollout case is real — triggers queued before this field existed are replayed by the new
    consumer — and a trigger that fails to parse is dropped silently.
    """
    trigger = parse_stage_trigger({"data": {"token": "t1", "dataset": "bronze$events", "namespace": "bronze"}})
    assert trigger is not None
    assert trigger.pre_row_count is None


def test_a_forged_count_cannot_promote_something_corrupt() -> None:
    """`pre_row_count` is a CLAIM, and the blast radius is bounded to asking or not asking.

    Worth pinning because it arrives off a topic anything in the mesh can publish to: the worst a
    forged value does is suppress a question. It cannot wave through a failed assertion, which is a
    separate and stricter gate — `gate_decision` puts BLOCK above HOLD unconditionally.
    """
    from medallion.services.gate_decision import GateOutcome, gate_decision

    # A forger sets pre_row_count so the band sees nothing unusual...
    assert _band(row_count=104, from_writer=None, from_trigger=100) == []
    # ...and the assertion still blocks, because the band never gated that.
    assert gate_decision(failed_assertions=["not_null"], band_reasons=[], cascade_via_publish=True, has_target=True, has_pub_topic=True) is GateOutcome.BLOCK


@pytest.mark.parametrize("carried", [100, 0, None])
def test_the_dispatch_path_passes_the_count_through(carried: int | None) -> None:
    """The wire hop: whatever pass 1 injects must survive `model_dump` -> re-publish -> re-parse.

    `publish_stage_ready` re-publishes `dict(spec.trigger)`, so a field that does not round-trip
    through the model is silently dropped between the passes and the fix would do nothing.
    """
    dumped = StageTrigger(token="t1", pre_row_count=carried).model_dump()
    if carried is not None:
        assert dumped["pre_row_count"] == carried
    reparsed = parse_stage_trigger({"data": {**dumped, "ray_job_done": True}})
    assert reparsed is not None
    assert reparsed.pre_row_count == carried
    assert reparsed.ray_job_done is True
