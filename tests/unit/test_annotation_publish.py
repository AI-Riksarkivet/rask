"""Publish → silver: which annotations land, and whose names travel with them.

The publish decision is pure, so these tests can be exhaustive about the two things that would be
expensive to discover in a cluster: work reaching silver that should not have, and provenance that
is wrong or missing once it is there.

The sharpest case is `skipped`. It is TERMINAL, so it satisfies the publish precondition and the
project actor happily allows the publish — but a skipped task can still hold a draft the annotator
saved before deciding the item did not belong. Terminal-therefore-publishable is the trap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from annotator.projects.models import Draft, Shape, Task, TaskState
from annotator.projects.publish import (
    AUTHORSHIP_FACET,
    PublishRefusal,
    attribution_for,
    authorship_facet,
    build_plan,
)


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _task(task_id: str, state: TaskState, **kw: Any) -> Task:
    base: dict[str, Any] = {
        "task_id": task_id,
        "project_id": "p1",
        "state": state,
        "source": {"kind": "chunks", "keys": [task_id]},
        "media": {"kind": "image", "image_url": f"s3://b/{task_id}.jpg"},
    }
    if state is TaskState.ACCEPTED:
        base.setdefault("submitted_by", "gina")
        base.setdefault("submitted_at", NOW)
        base.setdefault("reviewed_by", "carol")
        base.setdefault("reviewed_at", NOW)
        base.setdefault("review_action", "accepted")
    base.update(kw)
    return Task.model_validate(base)


def _draft(task_id: str, n: int, author: str = "gina") -> Draft:
    return Draft(
        task_id=task_id,
        project_id="p1",
        author=author,
        shapes=[Shape(shape_type="bbox", x=float(i), y=0.0, width=1.0, height=1.0) for i in range(n)],
        revision=1,
    )


# --------------------------------------------------------------------------------------------------
# What lands
# --------------------------------------------------------------------------------------------------


def test_only_accepted_tasks_contribute_rows() -> None:
    plan = build_plan("p1", [(_task("t0", TaskState.ACCEPTED), _draft("t0", 2))])
    assert plan.shape_count == 2
    assert plan.skipped_task_ids == []


@pytest.mark.parametrize(
    "state",
    [TaskState.SKIPPED, TaskState.UNASSIGNED, TaskState.CLAIMED, TaskState.IN_REVIEW, TaskState.CHANGES_REQUESTED],
)
def test_a_non_accepted_task_contributes_no_rows_even_when_it_has_a_draft(state: TaskState) -> None:
    """The trap: `skipped` is terminal, so the project actor ALLOWS the publish. Its draft must still
    not reach silver — the annotator drew those shapes and then decided the item did not belong."""
    plan = build_plan("p1", [(_task("t0", state), _draft("t0", 5))])

    assert plan.rows == [], f"a {state} task's draft reached silver"
    assert plan.attributions == [], f"a {state} task was credited as a contributor"
    assert plan.skipped_task_ids == ["t0"]


def test_a_skipped_task_is_recorded_not_silently_dropped() -> None:
    """A publish that quietly halves a dataset must be visible in the lineage, not inferred from a
    row count nobody compared against anything."""
    plan = build_plan(
        "p1",
        [
            (_task("t0", TaskState.ACCEPTED), _draft("t0", 1)),
            (_task("t1", TaskState.SKIPPED), _draft("t1", 9)),
        ],
    )
    facet = authorship_facet(plan, published_by="henry")
    assert facet["tasksPublished"] == 1
    assert facet["tasksSkipped"] == 1
    assert facet["shapes"] == 1


def test_an_accepted_task_with_no_draft_contributes_attribution_but_no_rows() -> None:
    """A reviewer accepting an empty item ("nothing to annotate here") is a real decision, and the
    person who made it belongs in the provenance."""
    plan = build_plan("p1", [(_task("t0", TaskState.ACCEPTED), None)])
    assert plan.rows == []
    assert [a.annotated_by for a in plan.attributions] == ["gina"]


# --------------------------------------------------------------------------------------------------
# Whose names travel with it
# --------------------------------------------------------------------------------------------------


def test_row_provenance_comes_from_the_task_not_the_payload() -> None:
    """The shape payload is client-supplied; `annotated_by` must not be. A client that sends its own
    `annotated_by` must not be able to overwrite the server's record of who did the work."""
    task = _task("t0", TaskState.ACCEPTED, submitted_by="gina", reviewed_by="carol")
    draft = _draft("t0", 1)
    draft.shapes[0].attributes = {"annotated_by": "mallory"}

    row = build_plan("p1", [(task, draft)]).rows[0]

    assert row["annotated_by"] == "gina", "a client-supplied field overwrote server-recorded provenance"
    assert row["reviewed_by"] == "carol"
    assert row["review_action"] == "accepted"


def test_the_drafter_is_carried_separately_from_the_submitter() -> None:
    """Under `fix_and_accept` the REVIEWER edits the shapes. Collapsing "who annotated" and "who last
    saved" into one field would credit the wrong person for the work — in either direction."""
    task = _task("t0", TaskState.ACCEPTED, submitted_by="gina", reviewed_by="carol", review_action="fix_and_accept")
    plan = build_plan("p1", [(task, _draft("t0", 1, author="carol"))])

    a = plan.attributions[0]
    assert a.annotated_by == "gina"
    assert a.drafted_by == "carol"
    assert a.review_action == "fix_and_accept"


def test_an_accepted_task_with_no_submitter_refuses_the_publish() -> None:
    """A broken audit trail must stop the publish. Anonymous provenance in silver is worse than no
    publish, because it is indistinguishable from real provenance once written."""
    task = _task("t0", TaskState.ACCEPTED, submitted_by=None)
    with pytest.raises(PublishRefusal, match="anonymous"):
        build_plan("p1", [(task, _draft("t0", 1))])


def test_attribution_survives_a_task_with_no_review() -> None:
    """A project can waive review. That is legal, so it is REPORTED rather than refused — but a
    governance reader must be able to see it without diffing two lists."""
    task = _task("t0", TaskState.ACCEPTED, reviewed_by=None, reviewed_at=None, review_action=None)
    plan = build_plan("p1", [(task, _draft("t0", 1))])

    assert attribution_for(task, _draft("t0", 1)).reviewed_by is None
    assert authorship_facet(plan, published_by="henry")["tasksWithoutReview"] == 1


# --------------------------------------------------------------------------------------------------
# The run facet
# --------------------------------------------------------------------------------------------------


def test_the_facet_names_distinct_contributors_in_a_stable_order() -> None:
    """The workflow can replay this activity after a crash. Two replays producing differently-ordered
    lists would read as two different provenances for one publish."""
    plan = build_plan(
        "p1",
        [
            (_task("t0", TaskState.ACCEPTED, submitted_by="zoe", reviewed_by="carol"), _draft("t0", 1)),
            (_task("t1", TaskState.ACCEPTED, submitted_by="gina", reviewed_by="carol"), _draft("t1", 1)),
            (_task("t2", TaskState.ACCEPTED, submitted_by="gina", reviewed_by="al"), _draft("t2", 1)),
        ],
    )

    facet = authorship_facet(plan, published_by="henry")

    assert facet["annotators"] == ["gina", "zoe"], "contributors must be distinct and sorted"
    assert facet["reviewers"] == ["al", "carol"]
    assert facet["publishedBy"] == "henry"
    assert facet["reviewActions"] == {"accepted": 3}


def test_the_facet_is_spec_legal() -> None:
    """A custom facet still has to carry `_producer` and `_schemaURL`, or a strict OpenLineage
    consumer drops it — and provenance that silently vanishes at the far end is not provenance."""
    facet = authorship_facet(build_plan("p1", [(_task("t0", TaskState.ACCEPTED), _draft("t0", 1))]), published_by="henry")
    assert facet["_producer"] and facet["_schemaURL"]
    assert AUTHORSHIP_FACET  # the key the emitter mounts it under


def test_the_rows_and_the_facet_cannot_disagree() -> None:
    """Both are projected from one plan in one pass — the property that makes "the graph says X, the
    data says Y" impossible rather than merely unlikely."""
    plan = build_plan(
        "p1",
        [
            (_task("t0", TaskState.ACCEPTED, submitted_by="gina"), _draft("t0", 3)),
            (_task("t1", TaskState.ACCEPTED, submitted_by="zoe"), _draft("t1", 2)),
            (_task("t2", TaskState.SKIPPED), _draft("t2", 7)),
        ],
    )
    facet = authorship_facet(plan, published_by="henry")

    assert facet["shapes"] == len(plan.rows) == 5
    assert set(facet["annotators"]) == {r["annotated_by"] for r in plan.rows}
    assert facet["tasksPublished"] + facet["tasksSkipped"] == 3
