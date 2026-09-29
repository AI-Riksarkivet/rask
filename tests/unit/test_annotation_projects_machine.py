"""The annotation-project domain core — entities, both state machines, the publish contract.

Named by `OPEN-WORK.md#design--annotation-projects` §S1. These tests deliberately need **no store, no
corpus mount, no OpenFGA and no running service**: the domain core is store-free, which is what makes
slices S1/S3 buildable ahead of the actor plane.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict, cast

import pytest

from annotator.projects import (
    IllegalTransition,
    ProjectState,
    TaskState,
    project_transition,
)


# --------------------------------------------------------------------------------------------------
# Project machine (§5.1)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "event", "expected"),
    [
        (ProjectState.DRAFT, "open", ProjectState.LABELING),
        (ProjectState.LABELING, "freeze", ProjectState.FROZEN),
        (ProjectState.PUBLISHED, "archive", ProjectState.ARCHIVED),
    ],
)
def test_project_legal_edges(state: ProjectState, event: str, expected: ProjectState) -> None:
    assert project_transition(state, event)[0] == expected


@pytest.mark.parametrize(
    "state",
    [ProjectState.FROZEN],
)
def test_send_into_a_closed_project_is_illegal(state: ProjectState) -> None:
    """§5.1: sending items into a frozen/publishing/published/archived project is rejected."""
    with pytest.raises(IllegalTransition):
        project_transition(state, "send")


def test_project_edges_carry_the_permission_that_gates_them() -> None:
    assert project_transition(ProjectState.DRAFT, "open")[1] == "can_manage"
    assert project_transition(ProjectState.FROZEN, "publish")[1] == "can_publish"
    # System-caused edges have no principal, so no permission to check.
    assert project_transition(ProjectState.PUBLISHING, "publish_succeeded")[1] is None


# --------------------------------------------------------------------------------------------------
# The identity-bound rules (§5.2), as the ONE function both sides of the sidecar apply
# --------------------------------------------------------------------------------------------------


class _IdentityCall(TypedDict):
    """The keyword shape of `identity_violation`, so the per-case dict is checked against it.

    Each case below states only the rows it is about; the call merges them over an all-`None` base.
    Naming the merged shape keeps the call honest — a parameter added to, or retyped on,
    `identity_violation` fails the typecheck here instead of being unpacked as `object`.
    """

    event: str
    subject: str | None
    assignee: str | None
    submitted_by: str | None
    subject_can_manage: NotRequired[bool]


@pytest.mark.parametrize(
    ("case", "kwargs", "expected"),
    [
        # A task released in between is not held by anyone, so nothing here objects — the transition
        # table still decides whether `submit` is legal from the state it landed in.
        pytest.param(
            "a task free by the time the turn runs",
            {"event": "submit", "subject": "gina", "assignee": None},
            None,
            id="a task free by the time the turn runs-kwargs3-None",
        ),
        # Nobody has submitted yet: `submitted_by` is None and so is a system caller's subject.
        # Comparing them as equal would ban the review of an unsubmitted task, which is nonsense.
        pytest.param(
            "an unsubmitted task, system caller",
            {"event": "accept", "subject": None, "submitted_by": None},
            None,
            id="an unsubmitted task, system caller-kwargs8-None",
        ),
        pytest.param(
            "release of a task nobody holds",
            {"event": "release", "subject": "henry", "assignee": None},
            None,
            id="release of a task nobody holds-kwargs12-None",
        ),
    ],
)
def test_the_identity_bound_rules_are_a_pure_function_of_the_tasks_own_rows(case: str, kwargs: dict[str, object], expected: str | None) -> None:
    """The truth table both call sites read from — the HTTP layer against its pre-turn snapshot, the
    actor against the rows of its own turn. Pinned HERE, with no store and no OpenFGA, because that
    is the property that lets the actor apply it at all."""
    from annotator.projects.machines import identity_violation

    violation = identity_violation(**cast("_IdentityCall", {"subject": None, "assignee": None, "submitted_by": None, **kwargs}))

    if expected is None:
        assert violation is None, f"{case}: refused with {violation!r}"
    else:
        assert violation is not None and expected in violation, case


# --------------------------------------------------------------------------------------------------
# Legal-event derivation — what A1's "the UI renders the transitions the backend supplies" reads
# --------------------------------------------------------------------------------------------------


def test_legal_project_events_include_send_only_where_send_is_legal() -> None:
    from annotator.projects.machines import legal_project_events

    labeling = {e["event"] for e in legal_project_events(ProjectState.LABELING)}
    frozen = {e["event"] for e in legal_project_events(ProjectState.FROZEN)}
    assert "send" in labeling
    assert "send" not in frozen


def test_legal_task_events_for_the_working_loop() -> None:
    from annotator.projects.machines import legal_task_events

    claimed = {(e["event"], e["permission"]) for e in legal_task_events(TaskState.CLAIMED)}
    assert claimed == {
        ("save_draft", "can_annotate"),
        ("submit", "can_annotate"),
        ("release", "can_annotate"),
        ("skip", "can_annotate"),
    }
    in_review = {e["event"] for e in legal_task_events(TaskState.IN_REVIEW)}
    assert in_review == {"accept", "fix_and_accept", "request_changes"}
