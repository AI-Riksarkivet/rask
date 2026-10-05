"""The two fields that steer the Ray lane's two passes are untrusted input, refused rather than coerced.

`ray_job_done` routes a delivery to pass 2 (measure the destination the job wrote) and `event_time` is pass 1's
instant, carried so the dataset's lineage document and the published COMPLETE name one eventTime (R26). Both ride a
trigger re-parsed through `parse_stage_trigger` like any bus arrival.
"""

from __future__ import annotations

from typing import Any

from medallion.services.trigger_guards import parse_stage_trigger


def _event(**data: Any) -> dict[str, Any]:
    return {"data": {"token": "tok-1", **data}}


def test_a_NON_BOOL_ray_job_done_is_refused_rather_than_coerced() -> None:
    """`extra="ignore"` tolerates unknown fields; it does not tolerate a known field of the wrong type.

    A string "false" coercing to True is the classic version of this bug, and it would silently route
    every trigger down the measure branch.
    """
    assert parse_stage_trigger(_event(ray_job_done="not-a-bool")) is None


def test_the_carried_instant_is_REFUSED_if_malformed() -> None:
    """The trigger is untrusted input like every other field. A garbage eventTime would produce a
    spec-invalid RunEvent, and the emit would fail AFTER the data landed — the worst moment."""
    assert parse_stage_trigger({"data": {"token": "t", "event_time": "not-a-timestamp"}}) is None
    ok = parse_stage_trigger({"data": {"token": "t", "event_time": "2026-08-15T12:00:00+00:00"}})
    assert ok is not None and ok.event_time == "2026-08-15T12:00:00+00:00"
