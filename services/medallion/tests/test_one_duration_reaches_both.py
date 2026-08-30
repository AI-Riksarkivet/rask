"""The graph and the metric disagreed about how long a Ray stage took — by orders of magnitude.

`docs/architecture/batch-processing-invariants.md` B10: "Monotonic clocks; the same number lands in the lineage facet." The
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
from pathlib import Path

from medallion.services import transform


_MODULE = Path(inspect.getfile(transform))

#: The duration used to be derived and spent inside one 900-line `handle_stage`. MED-004 split that
#: handler into seams, so the number now crosses a boundary: `_build_stage_event` DERIVES it and
#: returns it beside the run event, `handle_stage` carries it, and `_report_success` SPENDS it on the
#: metric. That boundary is exactly where the two could drift apart again, so these read the whole
#: module rather than one function — a search scoped to the deriver would pass while the metric read
#: a different number.
_BUILD = inspect.getsource(transform._build_stage_event)
_REPORT = inspect.getsource(transform._report_success)
_HANDLER = inspect.getsource(transform.handle_stage)


class TestTheDurationIsResolvedBeforeItIsEmitted:
    def test_the_run_event_does_not_take_the_wake_up_duration(self) -> None:
        assert "duration_seconds=elapsed_seconds" not in _MODULE.read_text(), (
            "the lineage facet is built from this handler's own wall time. On the Ray lane pass 2 is "
            "the measure-and-emit wake-up, so the graph records seconds for a stage that ran hours"
        )

    def test_the_run_event_takes_the_resolved_stage_duration(self) -> None:
        """Scoped to the `build_run_event` call, not the whole function — the metric call carries the
        same keyword, so a whole-body search passes while the facet stays wrong."""
        _, _, after = _BUILD.partition("run_event = build_run_event(")
        call, _, _ = after.partition("\n    )")
        assert "duration_seconds=stage_seconds" in call, f"the emitted duration is not the resolved one: {call[-400:]}"

    def test_the_resolution_happens_before_the_emit(self) -> None:
        """Ordering IS the fix: the value existed already and was computed after the event that
        needed it. A later re-derivation would leave the two able to drift apart again."""
        assert _BUILD.index("stage_seconds =") < _BUILD.index("duration_seconds=stage_seconds")


class TestTheRayWatchersSpanWins:
    def test_a_completed_ray_job_uses_the_watchers_measurement(self) -> None:
        assert "trigger.ray_duration_seconds" in _BUILD
        assert "trigger.ray_job_done" in _BUILD, (
            "the preference must be gated on the job actually having completed — a duration carried on a dispatch trigger measures nothing"
        )

    def test_the_in_process_lane_still_uses_its_own_clock(self) -> None:
        """The non-Ray path has no watcher, so `elapsed_seconds` IS the stage's real span there. The
        fix must not replace a correct number with an absent one."""
        assert "else elapsed_seconds" in _BUILD


class TestOneNumberNotTwo:
    def test_the_metric_and_the_facet_read_the_same_variable(self) -> None:
        """Asserted structurally rather than by driving both paths, because the property is that
        there is ONE value — two expressions that agree today are what produced this defect."""
        assert _MODULE.read_text().count("stage_seconds =") == 1, "the duration is derived in more than one place"
        assert "duration_seconds=stage_seconds" in _BUILD

    def test_the_metric_spends_the_number_the_facet_was_given(self) -> None:
        """The seam MED-004 introduced: the deriver hands the number back and the handler carries it
        to the metric. A `_report_success` that re-measured — or took a different argument — would
        recreate the exact defect this file exists for, one function boundary further along."""
        assert "duration_seconds=stage_seconds" in _REPORT, "the metric no longer spends the resolved duration"
        assert "stage_seconds, run_event = _build_stage_event(" in _HANDLER, "the handler no longer takes the duration from the deriver"
        assert "stage_seconds=stage_seconds" in _HANDLER, "the handler no longer hands the resolved duration to the metric"
