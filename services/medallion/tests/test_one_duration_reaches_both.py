"""The graph and the metric disagreed about how long a Ray stage took — by orders of magnitude.

`open_batch_process.md` B10: "Monotonic clocks; the same number lands in the lineage facet." The
clocks are monotonic. The same number does not land.

On the RAY lane the mover runs twice. Pass 1 submits and returns; the stage then runs on the cluster
for minutes-to-hours; the watcher polls it to a terminal state and re-publishes the trigger carrying
`ray_duration_seconds`, its own measured span. Pass 2 wakes up, measures, emits, and cascades.

`stage_seconds` correctly prefers the watcher's span — so the METRIC is right. The lineage run event
is built earlier in the handler and takes `elapsed_seconds`, which on pass 2 is this handler's own
wall time: the measure-and-emit wake-up. Seconds, for a stage that may have run for hours.

Two consequences, and the second is the expensive one. The graph under-reports every Ray stage, so
"how long does bronze→silver take" has two answers depending on which system you ask. And the answer
that looks authoritative — the one in the lineage record, which is the durable audit trail — is the
wrong one, while the metric that agrees with reality is the one people treat as approximate.

This is exactly the defect B10 was written about, on exactly the path B10 names, which is why the
invariant is stated as "the same number lands in the lineage facet" rather than "measure with a
monotonic clock". Measuring correctly and then emitting a different value is the failure.
"""

from __future__ import annotations

import inspect

from medallion.services import transform


class TestTheDurationIsResolvedBeforeItIsEmitted:
    def test_the_run_event_does_not_take_the_wake_up_duration(self) -> None:
        source = inspect.getsource(transform.handle_stage)
        assert "duration_seconds=elapsed_seconds" not in source, (
            "the lineage facet is built from this handler's own wall time. On the Ray lane pass 2 is "
            "the measure-and-emit wake-up, so the graph records seconds for a stage that ran hours"
        )

    def test_the_run_event_takes_the_resolved_stage_duration(self) -> None:
        """Scoped to the `build_run_event` call, not the whole function — the metric call carries the
        same keyword, so a whole-body search passes while the facet stays wrong."""
        source = inspect.getsource(transform.handle_stage)
        _, _, after = source.partition("run_event = build_run_event(")
        call, _, _ = after.partition("\n        )")
        assert "duration_seconds=stage_seconds" in call, f"the emitted duration is not the resolved one: {call[-400:]}"

    def test_the_resolution_happens_before_the_emit(self) -> None:
        """Ordering IS the fix: the value existed already and was computed after the event that
        needed it. A later re-derivation would leave the two able to drift apart again."""
        source = inspect.getsource(transform.handle_stage)
        assert source.index("stage_seconds =") < source.index("duration_seconds=stage_seconds")


class TestTheRayWatchersSpanWins:
    def test_a_completed_ray_job_uses_the_watchers_measurement(self) -> None:
        source = inspect.getsource(transform.handle_stage)
        assert "trigger.ray_duration_seconds" in source
        assert "trigger.ray_job_done" in source, (
            "the preference must be gated on the job actually having completed — a duration carried on a dispatch trigger measures nothing"
        )

    def test_the_in_process_lane_still_uses_its_own_clock(self) -> None:
        """The non-Ray path has no watcher, so `elapsed_seconds` IS the stage's real span there. The
        fix must not replace a correct number with an absent one."""
        source = inspect.getsource(transform.handle_stage)
        assert "else elapsed_seconds" in source


class TestOneNumberNotTwo:
    def test_the_metric_and_the_facet_read_the_same_variable(self) -> None:
        """Asserted structurally rather than by driving both paths, because the property is that
        there is ONE value — two expressions that agree today are what produced this defect."""
        source = inspect.getsource(transform.handle_stage)
        assert source.count("stage_seconds =") == 1, "the duration is derived in more than one place"
        assert "duration_seconds=stage_seconds" in source
        assert "duration_seconds=stage_seconds," in source or "duration_seconds=stage_seconds\n" in source
