"""A job the dashboard once knew and no longer knows has VANISHED — it is not still starting up.

`poll_stage` answers `None` for a 404, and that is deliberate: a poll can land before the dashboard
has registered a just-submitted id, and treating that first 404 as failure would kill every run on a
slow submit. But the same `None` arrives for a completely different event — the Ray head restarted
and lost its job table (VERIFIED against the live cluster 2026-08-31: after a head rollout the new
head answered `jobs: 0` and the running watch's polls turned into a steady `404 Not Found`).

Conflating the two costs a full ceiling. With `MAX_POLLS` 2880 at a 30 s interval the watch keeps
asking a head that will never answer for TWENTY-FOUR HOURS, then reports `abandoned` — and because
`publish_stage_ready` runs only on the success branch, the cascade behind it stops with nothing
saying so for a day.

The distinguishing fact is already in hand: whether any poll ever came back with a real status. Once
one has, the id was registered, so a later 404 can only mean the record is gone.
"""

from __future__ import annotations

from typing import Any, cast

from .test_stage_workflow import _Ctx, _drive, _spec


def test_a_job_that_vanishes_after_being_seen_ends_the_watch_now_not_in_24_hours() -> None:
    """Seen RUNNING, then 404: the watch must END, not burn the rest of its ceiling."""
    ctx = _Ctx({"submit_stage": ["sub-1"], "poll_stage": ["RUNNING", None, None, None, None]})
    out = _drive(ctx, _spec(max_polls=2880))

    assert out["verdict"] == "abandoned", "a vanished job is not a success and not a job failure"
    # The whole point: it stopped on the poll AFTER the disappearance, not after 2880 of them.
    assert ctx.actions.count("call_activity(poll_stage)") == 2, (
        f"kept polling a job the dashboard has forgotten: {ctx.actions.count('call_activity(poll_stage)')} polls"
    )
    assert "call_activity(report_stage_outcome)" in ctx.actions


def test_a_404_before_the_job_was_ever_seen_is_still_survivable() -> None:
    """The submit race must keep working — this is the behaviour the early exit must not break."""
    ctx = _Ctx({"submit_stage": ["sub-1"], "poll_stage": [None, None, "RUNNING", "SUCCEEDED"]})
    out = _drive(ctx, cast("Any", _spec(max_polls=10)))

    assert out["verdict"] == "succeeded", "two not-yet-registered polls must not end the watch"
    assert ctx.actions.count("call_activity(poll_stage)") == 4


def test_a_vanished_training_job_ends_its_watch_too() -> None:
    """The train watch has the identical split, and the identical cost for getting it wrong.

    Longer runs make this WORSE, not better: a training watch is the one most likely to still be
    holding when a head restart takes the job table, and its ceiling is the same 24 hours.
    """
    from .test_train_workflow import _Ctx as _TCtx
    from .test_train_workflow import _drive as _tdrive
    from .test_train_workflow import _spec as _tspec

    ctx = _TCtx({"poll_train": ["RUNNING", None, None, None, None]})
    out = _tdrive(ctx, cast("Any", _tspec(max_polls=2880)))

    assert out["verdict"] == "abandoned"
    assert ctx.actions.count("call_activity(poll_train)") == 2, "kept polling a training job the head has forgotten"


def test_a_training_404_before_the_job_was_seen_is_still_survivable() -> None:
    from .test_train_workflow import _Ctx as _TCtx
    from .test_train_workflow import _drive as _tdrive
    from .test_train_workflow import _spec as _tspec

    ctx = _TCtx({"poll_train": [None, "RUNNING", "SUCCEEDED"]})
    out = _tdrive(ctx, cast("Any", _tspec(max_polls=10)))
    assert out["verdict"] == "succeeded"
