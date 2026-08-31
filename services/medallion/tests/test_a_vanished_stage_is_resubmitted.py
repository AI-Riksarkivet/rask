"""A stage whose job the head LOST is retried; a training run in the same state is not.

Owner ruling 2026-08-31, after the live probe that proved a head restart drops every job record
(`jobs: 0` on the new head, the watch's polls turning to a steady 404). Reporting `job_vanished` in
two minutes instead of hanging for twenty-four hours made the failure VISIBLE, but nothing recovered
the work — the tier behind it still waited for a `publish_stage_ready` that only the success branch
reaches.

Resubmitting is safe here because the submission id is deterministic in `(stage, token, from->to,
code)` and `ray_kit.submit_or_reattach` already takes `on_terminal_failure="resubmit"` for the STAGE
contract: the same id either creates a fresh job (the record is genuinely gone) or re-attaches to the
one that turned out to be alive. So a spurious 404 costs a re-attach, not a duplicate job.

The asymmetry with training is deliberate and is the reason this is not applied to both watches.
`submit_train_job` uses `on_terminal_failure="report"` because training compute is expensive and a
failed run stays terminal until a human resubmits with a fresh token. An automatic retry of a
four-hour GPU run is exactly what that policy exists to prevent.
"""

from __future__ import annotations

from typing import Any, cast

from medallion.workflow import MAX_RESUBMITS

from .test_stage_workflow import _Ctx, _drive, _spec


def test_a_vanished_stage_job_is_resubmitted_rather_than_abandoned() -> None:
    """Seen RUNNING, then the head forgets it: submit again instead of giving up."""
    # RUNNING, then the record is gone. The resubmitted job then runs to completion.
    ctx = _Ctx({"submit_stage": ["sub-1", "sub-2"], "poll_stage": ["RUNNING", None, "RUNNING", "SUCCEEDED"]})
    out = _drive(ctx, cast("Any", _spec(max_polls=2880)))

    assert out["verdict"] == "succeeded", "the retried job landed, so the cascade must continue"
    assert ctx.actions.count("call_activity(submit_stage)") == 2, "the vanished job was never resubmitted"


def test_the_resubmit_is_bounded() -> None:
    """A head that keeps losing jobs must not be retried forever."""
    ctx = _Ctx({"submit_stage": [f"sub-{n}" for n in range(20)], "poll_stage": ["RUNNING", None] * 20})
    out = _drive(ctx, cast("Any", _spec(max_polls=2880)))

    assert out["verdict"] == "abandoned", "it must eventually stop and report"
    submits = ctx.actions.count("call_activity(submit_stage)")
    assert submits == MAX_RESUBMITS + 1, f"expected the first submit plus {MAX_RESUBMITS} retries, got {submits}"


def test_a_healthy_run_is_never_resubmitted() -> None:
    """The common path must be untouched — exactly one submit."""
    ctx = _Ctx({"submit_stage": ["sub-1"], "poll_stage": ["RUNNING", "SUCCEEDED"]})
    out = _drive(ctx, cast("Any", _spec(max_polls=10)))

    assert out["verdict"] == "succeeded"
    assert ctx.actions.count("call_activity(submit_stage)") == 1


def test_the_training_watch_does_NOT_resubmit() -> None:
    """Expensive compute stays terminal until a human decides — the `report` contract.

    Pinned rather than left implicit: the two watches sit in one file and share a shape, so applying
    the stage's retry to both is the natural mistake.
    """
    from .test_train_workflow import _Ctx as _TCtx
    from .test_train_workflow import _drive as _tdrive
    from .test_train_workflow import _spec as _tspec

    ctx = _TCtx({"poll_train": ["RUNNING", None, None, None, None]})
    out = _tdrive(ctx, cast("Any", _tspec(max_polls=2880)))

    assert out["verdict"] == "abandoned"
    assert "call_activity(submit_train)" not in ctx.actions
