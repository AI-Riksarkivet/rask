"""The two state machines of `OPEN-WORK.md#design--annotation-projects` §5, as data.

The transition tables ARE the spec — `fire()` consults them rather than reimplementing them in
branches, so "everything not in the table is illegal" is structural. Each edge also carries the
`can_*` permission that gates it, which keeps the op→privilege mapping in one place and lets the API
layer ask the model rather than hardcode a ladder.

Authorization is NOT performed here: this module says *which* permission an event requires, and the
caller checks it against OpenFGA. That split is what lets the domain core be tested without a store,
a sidecar, or a running OpenFGA.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Final

from annotator.projects.models import ProjectState, TaskState


#: The wire form of a refused transition, and the reason it exists.
#:
#: Dapr gives an actor-side exception exactly ONE channel to this side of the sidecar: the actor
#: HTTP surface answers 500 with ``repr(ex)`` inside a JSON envelope
#: (``dapr/ext/fastapi/actor.py``) and the SDK nests that inside its own message. So whatever the
#: caller is to rebuild has to survive as text — and the text is JSON-escaped twice on the way.
#:
#: Reconstructing the domain fields by PARSING THE PROSE cost three things, all on live paths: the
#: estate's own refusal reasons contain an em dash and ``json.dumps`` renders it ``\u2014``, a
#: backslash in a subject was eaten by the hand-rolled un-escaper, and any failure whose text merely
#: mentioned the class was rewritten into a refused transition (a 409 for what is a 500 — and
#: ``saga._converge`` reads this exception type as "already converged", i.e. as SUCCESS).
#:
#: So the fields ride as a base64 payload instead. The alphabet (``A-Za-z0-9_-=``) is exactly the set
#: JSON never escapes, which is the whole point: no un-escaping step can exist to get it wrong.
_WIRE_MARKER: Final = "illegal-transition"
_WIRE_RE: Final = re.compile(rf"\[{_WIRE_MARKER}:([A-Za-z0-9_-]+=*)\]")


class IllegalTransition(ValueError):
    """Raised for an edge absent from the table — the closed-world guarantee of §5.

    ``str()`` is the human sentence a 409 carries. ``repr()`` is the WIRE form: the same sentence
    plus :meth:`wire_token`, because ``repr`` is what Dapr serialises and therefore the only place a
    structured payload can ride. Keeping the two apart is what lets the endpoints go on handing
    ``str(exc)`` to the client while the proxy rebuilds the exact fields.
    """

    def __init__(self, kind: str, state: str, event: str, *, message: str | None = None) -> None:
        super().__init__(message if message is not None else f"illegal {kind} transition: {event!r} is not permitted from {state!r}")
        # `str(...)`: `state` is handed in as an enum member at most call sites, and the wire payload
        # has to be JSON. The rendered sentence above still uses the value as it was passed, so the
        # text a locally-raised error carries is unchanged.
        self.kind = str(kind)
        self.state = str(state)
        self.event = str(event)

    def wire_token(self) -> str:
        """The structured payload, in a form two layers of JSON escaping cannot touch."""
        blob = json.dumps({"kind": self.kind, "state": self.state, "event": self.event, "message": str(self)}, ensure_ascii=False)
        return f"[{_WIRE_MARKER}:{base64.urlsafe_b64encode(blob.encode()).decode('ascii')}]"

    def __repr__(self) -> str:
        """What crosses the sidecar — the sentence AND the token, in one ordinary exception repr."""
        return f"{type(self).__name__}({str(self) + ' ' + self.wire_token()!r})"

    @classmethod
    def from_wire(cls, text: str) -> IllegalTransition | None:
        """Rebuild the error from `text`, or `None` when `text` does not carry one.

        `None` is a real answer and the caller must re-raise on it: a failure that is not a refused
        transition has to stay the failure it is. There is deliberately NO prose fallback — an actor
        that answers without a token is not running this code, and guessing at its sentence is the
        behaviour this replaces.
        """
        found = _WIRE_RE.search(text)
        if found is None:
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(found.group(1).encode("ascii")).decode())
        except ValueError:  # binascii.Error, UnicodeDecodeError and JSONDecodeError are all ValueError
            return None
        if not isinstance(payload, dict):
            return None
        message = payload.get("message")
        return cls(
            str(payload.get("kind", "")),
            str(payload.get("state", "")),
            str(payload.get("event", "")),
            message=str(message) if message else None,
        )


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
    # §5.2: `reopen` is a MANAGER action, not a reviewer one — it un-finishes work that was
    # already accepted, which is a scheduling decision rather than a review verdict.
    (TaskState.ACCEPTED, "reopen"): (TaskState.CHANGES_REQUESTED, "can_manage"),
    (TaskState.SKIPPED, "requeue"): (TaskState.UNASSIGNED, "can_manage"),
}

#: A reviewer may not accept their own submission (§5.2), checked against the task's `submitted_by`.
#: Naming the events here keeps the rule discoverable from the machine — and gives
#: :func:`identity_violation`, which both the API layer and the actor apply, one place to read it.
SELF_REVIEW_FORBIDDEN: Final[frozenset[str]] = frozenset({"accept", "fix_and_accept", "request_changes"})

#: §5.2 rule 2 — "only the lease holder writes … even a manager". `skip` belongs here with
#: `save_draft` and `submit`: it is a decision ABOUT the work someone is holding, and letting a
#: passer-by make it discards another annotator's claim (and, with `requeue_for_others`, their turn).
#: A manager who wants the task must `release` it and re-`assign`.
LEASE_HOLDER_ONLY: Final[frozenset[str]] = frozenset({"save_draft", "submit", "skip"})

#: §5.2 — `release` is "lease holder OR can_manage". It is the documented escape hatch for a task
#: pinned to someone unavailable, so it is deliberately NOT in `LEASE_HOLDER_ONLY`:
#: :func:`identity_violation` allows the holder, or anyone the caller states holds `can_manage`.
HOLDER_OR_MANAGER: Final[frozenset[str]] = frozenset({"release"})

#: §5.2 rule 5 — "nothing escapes a published project". Once a project reaches one of these, every
#: task transition is rejected: provenance is frozen with the artifact, and a task that moved after
#: the publish would describe a dataset that no longer matches it.
FROZEN_PROJECT_STATES: Final[frozenset[ProjectState]] = frozenset({ProjectState.PUBLISHING, ProjectState.PUBLISHED, ProjectState.ARCHIVED})


def identity_violation(
    *,
    event: str,
    subject: str | None,
    assignee: str | None,
    submitted_by: str | None,
    subject_can_manage: bool = False,
) -> str | None:
    """The three IDENTITY-BOUND rules of §5.2, as a pure function of the task's own identity rows.

    They are not edges and cannot live in `TASK_EDGES`: they depend on WHO is firing and on columns
    the table does not carry. They live here, next to the sets that name them, because both sides of
    the sidecar have to apply them — the HTTP layer against its pre-turn snapshot (that is where a
    clean 403 comes from) and the actor inside its own turn (that is where the answer is true). Two
    implementations would drift, and the one that drifted would be the one that decided.

    `subject_can_manage` is a VERIFIED INPUT, not a check performed here. `release` is "lease holder
    OR can_manage", and `can_manage` is an OpenFGA fact: the caller resolves it and states it, which
    is what keeps this module — and the actor that imports it — testable with no store, no sidecar
    and no running OpenFGA.

    Returns the reason the transition is refused, or `None` when nothing here objects.
    """
    if event in SELF_REVIEW_FORBIDDEN and submitted_by is not None and submitted_by == subject:
        return "a reviewer may not review their own submission"
    if event in LEASE_HOLDER_ONLY and assignee not in (None, subject):
        # §5.2 rule 2 — "even a manager". A manager who wants the task must `release` and re-`assign`.
        return f"the task is held by {assignee}"
    if event in HOLDER_OR_MANAGER and assignee not in (None, subject) and not subject_can_manage:
        return f"the task is held by {assignee} — releasing it needs can_manage"
    return None


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


def legal_project_events(state: ProjectState) -> list[dict[str, str]]:
    """The PRINCIPAL-fireable edges out of `state`, each with the permission that gates it.

    This is what the API returns so the UI renders the transitions the backend supplies rather than
    hardcoding a second copy of the table that drifts. System edges (permission `None` — fired by
    the saga, never a principal) are excluded: a rendered control for one is a button that can only
    403. `send` is included where legal — it is an edge in every sense but the state change.
    """
    events = [
        {"event": event, "to": str(target), "permission": permission}
        for (from_state, event), (target, permission) in PROJECT_EDGES.items()
        if from_state is state and permission is not None
    ]
    if state in PROJECT_SEND_STATES:
        events.append({"event": "send", "to": str(state), "permission": "can_send_items"})
    return sorted(events, key=lambda e: e["event"])


def legal_task_events(state: TaskState) -> list[dict[str, str]]:
    """The principal-fireable edges out of `state` — the task half of `legal_project_events`."""
    return sorted(
        (
            {"event": event, "to": str(target), "permission": permission}
            for (from_state, event), (target, permission) in TASK_EDGES.items()
            if from_state is state and permission is not None
        ),
        key=lambda e: e["event"],
    )


def submit_target(review_required: bool) -> TaskState:
    """`submit` lands in `in_review`, or straight in `accepted` when the project waives review."""
    return TaskState.IN_REVIEW if review_required else TaskState.ACCEPTED


def may_publish(task_states: list[TaskState]) -> bool:
    """The publish precondition, mechanical (§5.1): EVERY task terminal. One `in_review` blocks it."""
    return all(s in {TaskState.ACCEPTED, TaskState.SKIPPED} for s in task_states)
