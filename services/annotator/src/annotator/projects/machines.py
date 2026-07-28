"""The two state machines of `docs/DESIGN-annotation-projects.md` §5, as data.

The transition tables ARE the spec — `fire()` consults them rather than reimplementing them in
branches, so "everything not in the table is illegal" is structural. Each edge also carries the
`can_*` permission that gates it, which keeps the op→privilege mapping in one place and lets the API
layer ask the model rather than hardcode a ladder.

Authorization is NOT performed here: this module says *which* permission an event requires, and the
caller checks it against OpenFGA. That split is what lets the domain core be tested without a store,
a sidecar, or a running OpenFGA.
"""

from __future__ import annotations

from typing import Final

from annotator.projects.models import ProjectState, TaskState


class IllegalTransition(ValueError):
    """Raised for an edge absent from the table — the closed-world guarantee of §5."""

    def __init__(self, kind: str, state: str, event: str) -> None:
        super().__init__(f"illegal {kind} transition: {event!r} is not permitted from {state!r}")
        self.kind = kind
        self.state = state
        self.event = event


#: (from, event) -> (to, required permission). `None` = caused by the system (a workflow or an actor
#: reminder), never by a principal, so there is no permission to check.
PROJECT_EDGES: Final[dict[tuple[ProjectState, str], tuple[ProjectState, str | None]]] = {
    (ProjectState.DRAFT, "open"): (ProjectState.LABELING, "can_manage"),
    (ProjectState.LABELING, "freeze"): (ProjectState.FROZEN, "can_manage"),
    (ProjectState.FROZEN, "open"): (ProjectState.LABELING, "can_manage"),
    (ProjectState.FROZEN, "publish"): (ProjectState.PUBLISHING, "can_publish"),
    (ProjectState.PUBLISH_FAILED, "publish"): (ProjectState.PUBLISHING, "can_publish"),
    (ProjectState.PUBLISHING, "publish_succeeded"): (ProjectState.PUBLISHED, None),
    (ProjectState.PUBLISHING, "publish_failed"): (ProjectState.PUBLISH_FAILED, None),
    (ProjectState.FROZEN, "archive"): (ProjectState.ARCHIVED, "can_manage"),
    (ProjectState.PUBLISHED, "archive"): (ProjectState.ARCHIVED, "can_manage"),
}

#: `send` leaves project state UNCHANGED and is legal only in draft/labeling. Sending into a frozen,
#: publishing, published or archived project is a 409 (§5.1) — modelled as absence, not a special case.
PROJECT_SEND_STATES: Final[frozenset[ProjectState]] = frozenset({ProjectState.DRAFT, ProjectState.LABELING})

TASK_EDGES: Final[dict[tuple[TaskState, str], tuple[TaskState, str | None]]] = {
    (TaskState.UNASSIGNED, "claim"): (TaskState.CLAIMED, "can_claim"),
    (TaskState.CHANGES_REQUESTED, "claim"): (TaskState.CLAIMED, "can_claim"),
    (TaskState.UNASSIGNED, "assign"): (TaskState.CLAIMED, "can_manage"),
    (TaskState.CHANGES_REQUESTED, "assign"): (TaskState.CLAIMED, "can_manage"),
    # save_draft is a self-edge: it renews the lease without changing state.
    (TaskState.CLAIMED, "save_draft"): (TaskState.CLAIMED, "can_annotate"),
    (TaskState.CLAIMED, "submit"): (TaskState.IN_REVIEW, "can_annotate"),
    (TaskState.CLAIMED, "release"): (TaskState.UNASSIGNED, "can_annotate"),
    (TaskState.CLAIMED, "lease_expired"): (TaskState.UNASSIGNED, None),
    (TaskState.CLAIMED, "skip"): (TaskState.SKIPPED, "can_annotate"),
    (TaskState.IN_REVIEW, "accept"): (TaskState.ACCEPTED, "can_review"),
    (TaskState.IN_REVIEW, "fix_and_accept"): (TaskState.ACCEPTED, "can_review"),
    (TaskState.IN_REVIEW, "request_changes"): (TaskState.CHANGES_REQUESTED, "can_review"),
    (TaskState.ACCEPTED, "reopen"): (TaskState.CHANGES_REQUESTED, "can_review"),
    (TaskState.SKIPPED, "requeue"): (TaskState.UNASSIGNED, "can_manage"),
}

#: A reviewer may not accept their own submission (§5.2). The API layer enforces it against the
#: task's `submitted_by`; naming the events here keeps the rule discoverable from the machine.
SELF_REVIEW_FORBIDDEN: Final[frozenset[str]] = frozenset({"accept", "fix_and_accept", "request_changes"})


def project_transition(state: ProjectState, event: str) -> tuple[ProjectState, str | None]:
    """Resolve a project edge, or raise `IllegalTransition`.

    `send` is special: legal in draft/labeling and a no-op on state, so it returns `state` unchanged.
    """
    if event == "send":
        if state not in PROJECT_SEND_STATES:
            raise IllegalTransition("project", state, event)
        return state, "can_send_items"
    try:
        return PROJECT_EDGES[(state, event)]
    except KeyError:
        raise IllegalTransition("project", state, event) from None


def task_transition(state: TaskState, event: str) -> tuple[TaskState, str | None]:
    """Resolve a task edge, or raise `IllegalTransition`."""
    try:
        return TASK_EDGES[(state, event)]
    except KeyError:
        raise IllegalTransition("task", state, event) from None


def submit_target(review_required: bool) -> TaskState:
    """`submit` lands in `in_review`, or straight in `accepted` when the project waives review."""
    return TaskState.IN_REVIEW if review_required else TaskState.ACCEPTED


def may_publish(task_states: list[TaskState]) -> bool:
    """The publish precondition, mechanical (§5.1): EVERY task terminal. One `in_review` blocks it."""
    return all(s in {TaskState.ACCEPTED, TaskState.SKIPPED} for s in task_states)
