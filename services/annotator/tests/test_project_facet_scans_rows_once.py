"""`project_facet` derives its per-task origins/datasets in a SINGLE pass over `plan.rows`.

open_python-audit `ANN-09`: the origins/datasets block built the distinct-task-id set with one pass
(`{r["task_id"] for r in plan.rows}`) and then, per distinct task, did a fresh `next(r for r in
plan.rows if r["task_id"] == task_id)` — a full rescan of the rows list per task, i.e.
O(distinct_tasks x rows) on the publish path. A single pass recording the first-seen row per task_id
produces the identical facet.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from annotator.projects.models import AnnotationProject, Draft, Shape, Task, TaskState
from annotator.projects.ontology import LabelClass, LabelOntology
from annotator.projects.publish import build_plan, project_facet


NOW = datetime(2026, 8, 29, tzinfo=UTC)


def _project() -> AnnotationProject:
    return AnnotationProject.model_validate(
        {
            "project_id": "p1",
            "tenant": "acme",
            "slug": "vasa",
            "ontology": LabelOntology(classes=[LabelClass(name="ship")]),
        }
    )


def _task(task_id: str) -> Task:
    return Task.model_validate(
        {
            "task_id": task_id,
            "project_id": "p1",
            "state": TaskState.ACCEPTED,
            "source": {"kind": "chunks", "keys": [f"key/{task_id}"], "where": "transcripts_v2"},
            "media": {"kind": "image", "image_url": f"s3://b/{task_id}.jpg"},
            "submitted_by": "gina",
            "submitted_at": NOW,
            "reviewed_by": "carol",
            "reviewed_at": NOW,
            "review_action": "accepted",
        }
    )


def _draft(task_id: str, n: int) -> Draft:
    return Draft(
        task_id=task_id,
        project_id="p1",
        author="gina",
        shapes=[Shape(shape_type="bbox", x=float(i), y=0.0, width=1.0, height=1.0, label="ship") for i in range(n)],
        revision=1,
    )


class _CountingRows(list[dict[str, Any]]):
    """A rows list that records how many times it is iterated, so a per-task rescan is visible."""

    iter_count = 0

    def __iter__(self) -> Any:
        type(self).iter_count += 1
        return super().__iter__()


def _scans_for(project: AnnotationProject, task_count: int) -> tuple[int, dict[str, Any]]:
    pairs = [(_task(f"t{i}"), _draft(f"t{i}", 2)) for i in range(task_count)]
    plan = build_plan(project, pairs, publish_id="pub1", published_at=NOW)
    _CountingRows.iter_count = 0
    plan.rows = _CountingRows(plan.rows)
    facet = project_facet(project, plan)
    return _CountingRows.iter_count, facet


def test_project_facet_scans_the_rows_a_constant_number_of_times() -> None:
    """The rows-scan count must not grow with the number of distinct tasks — the per-task rescan did."""
    project = _project()
    small, small_facet = _scans_for(project, 3)
    large, _ = _scans_for(project, 8)

    assert small_facet["sendOrigins"] == {"chunks": 3}, "fixture precondition: three distinct origins recorded"
    assert small == large, f"project_facet scaled its rows-scans with task count ({small} for 3 tasks, {large} for 8) — the O(tasks x rows) rescan is back"
