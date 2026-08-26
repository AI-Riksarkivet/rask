"""Three workflow-body defects that share one file and one action-order snapshot.

1. `StageJobSpec.lineage_json` is an uncapped `str`. A workflow input is re-persisted to the state
   store on EVERY checkpoint, and `stage_run` runs one poll per turn up to `MAX_POLLS` (2880 at the
   default interval) -- so an oversized blob is written 2880 times, not once. Nothing bounded it.

2. `train_run`'s abandoned exit yields NO activity. A lost or ceiling-hit train watch returned its
   outcome and told nobody: no metric, no lineage, no log an operator polls. Somebody's four-hour GPU
   run stops being watched and nothing anywhere says so.

3. `_watch_seconds` re-imports `datetime` inside the one workflow-scope helper whose sibling docstring
   forbids imports in the body -- and the module already imports it at line 51.

#2 CANNOT SHIP ALONE, and that is the trap this file exists to pin. `report_train_outcome` is a
SINGLE-ARMED failure reporter: its `reason` reads "ended <status>" and `_publish_train_fail` is
unconditional. Adding the yield without two-arming it would emit a lineage FAIL for a job that is
still running -- precisely what the abandoned branch's own comment says must not happen ("a training
job still running at the ceiling is alive and may yet land, and reporting it as dead sends somebody
hunting a healthy run"). `report_stage_outcome` is already two-armed; this brings its twin into line.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from medallion.workflow import MAX_LINEAGE_JSON_BYTES, StageJobSpec, TrainJobOutcome, TrainJobSpec, TrainReport, report_train_outcome


def test_an_oversized_lineage_blob_is_REFUSED_not_written_2880_times() -> None:
    """The spec is validated at the workflow's first line, so refusing here stops the run before any
    turn persists it -- rather than after the first of up to MAX_POLLS writes."""
    with pytest.raises(ValueError, match="lineage_json"):
        StageJobSpec.model_validate(
            {
                "from_uri": "s3://wh/bronze",
                "to_uri": "s3://wh/silver",
                "stage": "silver",
                "lineage_json": "x" * (MAX_LINEAGE_JSON_BYTES + 1),
            }
        )


def test_a_lineage_blob_INSIDE_the_bound_is_untouched() -> None:
    """The cap must not become a second failure mode for the ordinary case."""
    spec = StageJobSpec.model_validate({"from_uri": "s3://wh/bronze", "to_uri": "s3://wh/silver", "stage": "silver", "lineage_json": "x" * 1024})

    assert len(spec.lineage_json) == 1024


def _report(verdict: str, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    from medallion import workflow as workflow_mod

    published: list[str] = []
    recorded: list[str] = []
    monkeypatch.setattr(workflow_mod, "_publish_train_fail", lambda _spec, reason: published.append(reason))
    monkeypatch.setattr(workflow_mod, "record_train_outcome", recorded.append)

    payload = TrainReport(
        spec=TrainJobSpec(token="tok-1", model="churn", submission_id="ray-train-tok-1", originator="alice", project="acme"),
        outcome=TrainJobOutcome(submission_id="ray-train-tok-1", status="RUNNING", polls=5, verdict=verdict),
    )
    report_train_outcome(cast("Any", None), payload)
    return published, recorded


def test_an_ABANDONED_watch_is_RECORDED_but_not_reported_as_a_dead_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE TRAP. The job is still RUNNING; only the watch stopped. A lineage FAIL here would send
    somebody hunting a healthy four-hour run."""
    published, recorded = _report("abandoned", monkeypatch)

    assert recorded == ["abandoned"], "an abandoned watch must still be counted"
    assert published == [], f"an abandoned watch emitted a lineage FAIL for a job that is still running: {published}"


def test_a_FAILED_job_still_emits_its_lineage_FAIL(monkeypatch: pytest.MonkeyPatch) -> None:
    """The half that must not change: a job that actually died is the reason this reporter exists."""
    published, recorded = _report("failed", monkeypatch)

    assert recorded == ["failed"]
    assert len(published) == 1
    assert "ended" in published[0]


def test_the_watch_helper_does_NOT_re_import_datetime() -> None:
    """The module imports `datetime` at line 51. A local re-import inside the one workflow-scope helper
    reads as though the body were doing something the surrounding docstrings forbid."""
    import inspect

    from medallion.workflow import _watch_seconds

    source = inspect.getsource(_watch_seconds)

    assert "import datetime" not in source, "the workflow-scope helper still re-imports datetime"
