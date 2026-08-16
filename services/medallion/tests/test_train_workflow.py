"""The TRAIN watcher — the lane that had none.

THE GAP. `stage_run` watches a stage job to a terminal state; the train lane had no watcher at all.
`handle_train_trigger` submits and acks (D2, deliberately — training is hours of GPU and holding a
Dapr ack across it is the race A13 deleted), and the JOB emits its own OpenLineage lifecycle. That
covers a job that runs and fails: it reports its own FAIL, carrying `lance.originator` since
2026-08-16.

It does not cover a job that dies BEFORE it emits anything — a bad image, an OOM during runtime-env
setup, a `python /home/ray/jobs/<x>.py` whose entrypoint the image lacks (`exit 2`). Ray knows the
job FAILED; nobody else does; and the person who asked for it is told nothing, ever. That is the case
Ray's own `metadata` was stamped for: `rask.originator` survives the process and comes back on
`GET /api/jobs/<id>`.

WHY THE RUN ID IS THE JOB'S OWN, and it is the load-bearing decision here. The watcher emits with
`run_id_for(f"train-{token}")` — byte-identical to what `scripts/ray_train_job.py` uses. So when the
job DID emit its own FAIL, the watcher's event merges onto that same run rather than forking a second
failure for one training run; when the job died silently, the watcher's event is the only record and
it lands under the id every other surface already links to. Emitting under a fresh id would produce
two runs for one job and make the graph answer "what happened to this model" with a duplicate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from medallion.workflow import TrainJobSpec, train_run


class _Action:
    def __init__(self, kind: str, name: str = "", result: Any = None) -> None:
        self.kind, self.name, self.result = kind, name, result
        self.raises = False


class _Ctx:
    """Replay-faithful stand-in — records what was yielded, answers with scripted results."""

    def __init__(self, activity_results: dict[str, list[Any]] | None = None) -> None:
        self.actions: list[str] = []
        self._results = {k: list(v) for k, v in (activity_results or {}).items()}
        self.is_replaying = False
        self.raise_on: str | None = None
        self.instance_id = "wf-train-test"
        self.current_utc_datetime = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
        self.continued: Any = None
        #: What each activity was called WITH — the reporting payload is the assertion that matters.
        self.inputs: dict[str, list[Any]] = {}

    def call_activity(self, activity: Any, *, input: Any = None, retry_policy: Any = None) -> _Action:  # noqa: A002
        name = getattr(activity, "__name__", str(activity))
        self.actions.append(f"call_activity({name})")
        self.inputs.setdefault(name, []).append(input)
        queue = self._results.get(name)
        value = queue.pop(0) if queue else None
        action = _Action("activity", name, value)
        action.raises = name == self.raise_on
        return action

    def create_timer(self, delay: timedelta) -> _Action:
        self.actions.append(f"create_timer({int(delay.total_seconds())}s)")
        return _Action("timer")

    def continue_as_new(self, new_input: Any, *, save_events: bool = False) -> None:
        self.actions.append("continue_as_new")
        self.continued = new_input


def _drive(ctx: _Ctx, payload: dict[str, Any]) -> dict[str, Any]:
    """Run to completion ACROSS `continue_as_new` turns, feeding the carried input back in.

    The hand-off is the whole risk of the one-poll-per-turn shape: a turn starts with EMPTY history,
    so a poll count that did not ride along would restart at zero and the ceiling would never arrive.
    """
    current = payload
    result: dict[str, Any] = {}
    for _ in range(200):
        ctx.continued = None
        gen = train_run(cast("Any", ctx), current)
        try:
            action = gen.send(None)
            while True:
                if action.raises:
                    action = gen.throw(RuntimeError(f"{action.name} exhausted its retries"))
                    continue
                action = gen.send(action.result)
        except StopIteration as stop:
            result = stop.value or {}
        if ctx.continued is None:
            return result
        current = ctx.continued
    raise AssertionError("continue_as_new never converged")


def _spec(**over: Any) -> dict[str, Any]:
    base = TrainJobSpec(
        token="tok-train-1",
        model="churn",
        submission_id="ray-train-tok-train-1",
        originator="alice",
        project="acme",
        poll_interval_seconds=30,
        max_polls=5,
    )
    return base.model_copy(update=over).model_dump()


@pytest.mark.parametrize("terminal_bad", ["FAILED", "STOPPED"])
def test_a_train_job_that_DIES_reports_it(terminal_bad: str) -> None:
    """The whole point. Ray says the job is terminal-bad; the watcher records it, whether or not the
    job ever managed to emit anything itself."""
    ctx = _Ctx({"poll_train": [terminal_bad]})

    out = _drive(ctx, _spec())

    assert out["verdict"] == "failed"
    assert out["status"] == terminal_bad
    assert "call_activity(report_train_outcome)" in ctx.actions


def test_a_train_job_that_SUCCEEDS_reports_nothing() -> None:
    """The job emits its own COMPLETE. A second success event from here would fork a duplicate run for
    one training run, and the graph would answer 'what produced this model' twice."""
    ctx = _Ctx({"poll_train": ["SUCCEEDED"]})

    out = _drive(ctx, _spec())

    assert out["verdict"] == "succeeded"
    assert "call_activity(report_train_outcome)" not in ctx.actions


def test_the_watcher_NAMES_the_person_the_run_was_for() -> None:
    """Without this the watcher is pointless: a FAIL that names nobody is discarded by the plane's own
    rule, so the whole lane would be a producer whose output the consumer is designed to drop."""
    ctx = _Ctx({"poll_train": ["FAILED"]})

    _drive(ctx, _spec(originator="alice", project="acme"))

    reported = ctx.inputs["report_train_outcome"][0]
    assert reported["spec"]["originator"] == "alice"
    assert reported["spec"]["project"] == "acme"


def test_a_job_still_RUNNING_hands_the_watch_to_a_fresh_turn() -> None:
    """One poll per turn — the Monitor pattern. A turn carries the poll count forward, or the ceiling
    below can never be reached and the instance watches forever."""
    ctx = _Ctx({"poll_train": ["RUNNING", "RUNNING", "SUCCEEDED"]})

    out = _drive(ctx, _spec())

    assert ctx.actions.count("continue_as_new") == 2
    assert out["polls"] == 3, "the poll count did not survive the hand-off"


def test_the_watch_is_BOUNDED_and_says_so_rather_than_reporting_a_failure() -> None:
    """A job still running at the ceiling is NOT a failed job. Reporting it as one would send somebody
    hunting a training run that is alive and may yet land — the same wrong-state-reported defect this
    estate keeps producing. `abandoned` is its own verdict."""
    ctx = _Ctx({"poll_train": ["RUNNING"] * 40})

    out = _drive(ctx, _spec(max_polls=3))

    assert out["verdict"] == "abandoned"
    assert out["polls"] == 3


def test_a_LOST_watch_is_not_reported_as_a_dead_job() -> None:
    """An exhausted poll means the dashboard was unreachable across the whole retry policy. The JOB may
    be training perfectly; reporting `failed` would be a lie about somebody's four-hour run."""
    ctx = _Ctx({"poll_train": ["RUNNING"]})
    ctx.raise_on = "poll_train"

    out = _drive(ctx, _spec(max_polls=3))

    assert out["verdict"] == "abandoned"


def test_the_workflow_body_yields_ONLY_ctx_actions() -> None:
    """Determinism (DWF-DET): every yield must be a ctx action, never a bare awaitable or an I/O call
    smuggled into the body. A replay re-executes this generator from the top."""
    ctx = _Ctx({"poll_train": ["FAILED"]})

    _drive(ctx, _spec())

    assert all(a.startswith(("call_activity(", "create_timer(", "continue_as_new")) for a in ctx.actions), ctx.actions


def test_every_train_activity_is_registered() -> None:
    """A workflow that calls an unregistered activity fails at RUNTIME, in the cluster, on a real
    training run — the least recoverable place to learn it."""
    from medallion.workflow import ACTIVITIES, WORKFLOWS

    names = {a.__name__ for a in ACTIVITIES}
    assert {"poll_train", "report_train_outcome"} <= names
    assert train_run in WORKFLOWS
