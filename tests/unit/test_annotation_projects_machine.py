"""The annotation-project domain core — entities, both state machines, the publish contract.

Named by `OPEN-WORK.md#design--annotation-projects` §S1. These tests deliberately need **no store, no
corpus mount, no OpenFGA and no running service**: the domain core is store-free, which is what makes
slices S1/S3 buildable ahead of the actor plane.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from typing import NotRequired, TypedDict, cast

import pytest

from annotator.projects import (
    AnnotationProject,
    Draft,
    IllegalTransition,
    ProjectState,
    PublishRecord,
    Shape,
    Task,
    TaskState,
    may_publish,
    project_transition,
    submit_target,
    task_transition,
)
from annotator.projects.machines import SELF_REVIEW_FORBIDDEN


def _task(state: TaskState = TaskState.UNASSIGNED) -> Task:
    return Task(
        project_id="p1",
        state=state,
        source={"kind": "chunks", "keys": ["doc_1"]},  # type: ignore[arg-type]
        media={"kind": "image", "image_url": "s3://bucket/page.jpg"},  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------------------------------
# The decoupling guard — mechanical, not a promise (§3)
# --------------------------------------------------------------------------------------------------


def test_the_domain_core_never_reaches_the_lakehouse_plane() -> None:
    """CONTRACT: importing the domain core must NOT drag in the governed/lakehouse stack.

    An annotation project's state is the annotator's own until a publish. If this module could reach
    `lancekit`/the dataset registry, the project package would need a corpus mount to import — and
    S1/S3 would stop being buildable without one. The design asks for this guard by name.

    Runs in a SUBPROCESS on purpose. Checking `sys.modules` in-process is order-dependent nonsense:
    any earlier test in the session may already have imported the lakehouse plane, so the guard would
    fail on someone else's import (it did) or pass only because nothing happened to import it yet. A
    fresh interpreter is the only honest way to ask "what does importing THIS pull in".
    """
    probe = "import sys; import annotator.projects; print(','.join(sorted(m for m in sys.modules if 'lancekit' in m or m.endswith('.registry'))))"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    leaked = [m for m in result.stdout.strip().split(",") if m]
    assert leaked == [], f"the domain core pulled in the lakehouse plane: {leaked}"


# --------------------------------------------------------------------------------------------------
# Project machine (§5.1)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "event", "expected"),
    [
        (ProjectState.DRAFT, "open", ProjectState.LABELING),
        (ProjectState.LABELING, "freeze", ProjectState.FROZEN),
        (ProjectState.FROZEN, "open", ProjectState.LABELING),
        (ProjectState.FROZEN, "publish", ProjectState.PUBLISHING),
        (ProjectState.PUBLISHING, "publish_succeeded", ProjectState.PUBLISHED),
        (ProjectState.PUBLISHING, "publish_failed", ProjectState.PUBLISH_FAILED),
        (ProjectState.PUBLISHED, "archive", ProjectState.ARCHIVED),
    ],
)
def test_project_legal_edges(state: ProjectState, event: str, expected: ProjectState) -> None:
    assert project_transition(state, event)[0] == expected


def test_publish_failed_can_retry_rather_than_reset() -> None:
    """`publish_failed` is a resting state with a `publish` edge back — a retry, not a reopen."""
    assert project_transition(ProjectState.PUBLISH_FAILED, "publish")[0] == ProjectState.PUBLISHING


@pytest.mark.parametrize(
    "state",
    [ProjectState.FROZEN, ProjectState.PUBLISHING, ProjectState.PUBLISHED, ProjectState.ARCHIVED],
)
def test_send_into_a_closed_project_is_illegal(state: ProjectState) -> None:
    """§5.1: sending items into a frozen/publishing/published/archived project is rejected."""
    with pytest.raises(IllegalTransition):
        project_transition(state, "send")


def test_send_is_legal_while_labeling_and_leaves_state_unchanged() -> None:
    to, perm = project_transition(ProjectState.LABELING, "send")
    assert to == ProjectState.LABELING
    assert perm == "can_send_items"


def test_everything_not_in_the_table_is_illegal() -> None:
    """The closed-world guarantee: an unlisted edge raises rather than silently no-opping."""
    with pytest.raises(IllegalTransition):
        project_transition(ProjectState.ARCHIVED, "open")
    with pytest.raises(IllegalTransition):
        project_transition(ProjectState.DRAFT, "publish")


def test_project_edges_carry_the_permission_that_gates_them() -> None:
    assert project_transition(ProjectState.DRAFT, "open")[1] == "can_manage"
    assert project_transition(ProjectState.FROZEN, "publish")[1] == "can_publish"
    # System-caused edges have no principal, so no permission to check.
    assert project_transition(ProjectState.PUBLISHING, "publish_succeeded")[1] is None


# --------------------------------------------------------------------------------------------------
# Task machine (§5.2)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "event", "expected"),
    [
        (TaskState.UNASSIGNED, "claim", TaskState.CLAIMED),
        (TaskState.CHANGES_REQUESTED, "claim", TaskState.CLAIMED),
        (TaskState.CLAIMED, "submit", TaskState.IN_REVIEW),
        (TaskState.CLAIMED, "release", TaskState.UNASSIGNED),
        (TaskState.CLAIMED, "lease_expired", TaskState.UNASSIGNED),
        (TaskState.CLAIMED, "skip", TaskState.SKIPPED),
        (TaskState.IN_REVIEW, "accept", TaskState.ACCEPTED),
        (TaskState.IN_REVIEW, "request_changes", TaskState.CHANGES_REQUESTED),
        (TaskState.ACCEPTED, "reopen", TaskState.CHANGES_REQUESTED),
    ],
)
def test_task_legal_edges(state: TaskState, event: str, expected: TaskState) -> None:
    assert task_transition(state, event)[0] == expected


def test_save_draft_is_a_self_edge_so_a_lease_renewal_never_changes_state() -> None:
    assert task_transition(TaskState.CLAIMED, "save_draft")[0] == TaskState.CLAIMED


def test_lease_expiry_is_system_caused_and_has_no_permission() -> None:
    """A reminder fires this, not a principal — so there is nobody to authorize."""
    assert task_transition(TaskState.CLAIMED, "lease_expired")[1] is None


def test_review_actions_are_named_so_the_self_review_ban_is_discoverable() -> None:
    """A reviewer may not accept their own submission; the rule reads `submitted_by` against these."""
    assert {"accept", "fix_and_accept", "request_changes"} == SELF_REVIEW_FORBIDDEN


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
        # Lease holder only — the whole point of re-checking in-turn is the first two rows: the
        # HTTP layer saw `gina`, the lease moved, and the event arrives against `dave`'s claim.
        ("a stale submit after the lease moved", {"event": "submit", "subject": "gina", "assignee": "dave"}, "held by dave"),
        ("a stale skip after the lease moved", {"event": "skip", "subject": "gina", "assignee": "dave"}, "held by dave"),
        ("the holder submitting", {"event": "submit", "subject": "gina", "assignee": "gina"}, None),
        # A task released in between is not held by anyone, so nothing here objects — the transition
        # table still decides whether `submit` is legal from the state it landed in.
        ("a task free by the time the turn runs", {"event": "submit", "subject": "gina", "assignee": None}, None),
        ("can_manage does not unlock rule 2", {"event": "submit", "subject": "dave", "assignee": "gina", "subject_can_manage": True}, "held by gina"),
        # Self-review.
        ("reviewing one's own submission", {"event": "accept", "subject": "gina", "submitted_by": "gina"}, "own submission"),
        ("requesting changes on one's own", {"event": "request_changes", "subject": "gina", "submitted_by": "gina"}, "own submission"),
        ("reviewing someone else's", {"event": "accept", "subject": "dave", "submitted_by": "gina"}, None),
        # Nobody has submitted yet: `submitted_by` is None and so is a system caller's subject.
        # Comparing them as equal would ban the review of an unsubmitted task, which is nonsense.
        ("an unsubmitted task, system caller", {"event": "accept", "subject": None, "submitted_by": None}, None),
        # Release — holder OR can_manage, with can_manage stated by the caller, never checked here.
        ("release by a non-holder", {"event": "release", "subject": "henry", "assignee": "gina"}, "needs can_manage"),
        ("release by a manager", {"event": "release", "subject": "henry", "assignee": "gina", "subject_can_manage": True}, None),
        ("release by the holder", {"event": "release", "subject": "gina", "assignee": "gina"}, None),
        ("release of a task nobody holds", {"event": "release", "subject": "henry", "assignee": None}, None),
        # Edges no identity rule names — including the one system edge, whose subject is None and
        # whose reminder must never be refused for not being the lease holder.
        ("the lease reminder", {"event": "lease_expired", "subject": None, "assignee": "gina"}, None),
        ("a manager assigning", {"event": "assign", "subject": "mgr", "assignee": "gina"}, None),
        ("a claim", {"event": "claim", "subject": "gina", "assignee": None}, None),
        ("a manager reopening accepted work", {"event": "reopen", "subject": "mgr", "submitted_by": "gina"}, None),
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


def test_the_identity_rules_have_exactly_one_implementation() -> None:
    """`identity_violation` exists because the rule is now applied on BOTH sides of the sidecar, and
    two copies would drift — the one that drifted being the one that decided. The gate is on who
    APPLIES the rule: outside `machines.py` nothing may name the identity sets, re-write the holder
    comparison, or phrase either refusal.

    Set membership alone is NOT enough to catch a copy, which is how two survived the first version
    of this gate: a copy written for ONE fixed event never names a set. `tasks.py` refused a
    non-holder's draft save and import with a hand-rolled `assignee not in (None, subject)` on both
    draft-write routes while `identity_violation`'s docstring claimed one implementation. So the
    tells are the COMPARISONS as well — code shapes, not refusal wording, which prose collides with.

    `HOLDER_OR_MANAGER` is deliberately exempt from the membership tell: `tasks.py` reads it to
    decide whether to spend an OpenFGA `can_manage` Check, which is resolving an INPUT to the rule,
    not re-deciding it.
    """
    from pathlib import Path

    from annotator.projects import machines as machines_module

    tells = (
        " in SELF_REVIEW_FORBIDDEN",
        " in LEASE_HOLDER_ONLY",
        "not in (None, subject)",
        "submitted_by == subject",
        'submitted_by") == subject',
        "submitted_by'] == subject",
    )
    machines = Path(str(machines_module.__file__)).resolve()
    root = machines.parents[1]
    offenders = [
        f"{path.relative_to(root)}:{n}"
        for path in sorted(root.rglob("*.py"))
        if path != machines
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if any(tell in line for tell in tells)
    ]

    assert offenders == [], f"a second copy of the identity rules: {offenders}"


def test_submit_skips_review_only_when_the_project_waives_it() -> None:
    assert submit_target(review_required=True) == TaskState.IN_REVIEW
    assert submit_target(review_required=False) == TaskState.ACCEPTED


def test_a_claimed_task_cannot_be_reviewed_before_submission() -> None:
    with pytest.raises(IllegalTransition):
        task_transition(TaskState.CLAIMED, "accept")


# --------------------------------------------------------------------------------------------------
# The publish precondition (§5.1) and the publish record (§4.1)
# --------------------------------------------------------------------------------------------------


def test_one_task_in_review_blocks_the_publish() -> None:
    """The owner's "nothing lands before that", enforced rather than described."""
    assert may_publish([TaskState.ACCEPTED, TaskState.SKIPPED]) is True
    assert may_publish([TaskState.ACCEPTED, TaskState.IN_REVIEW]) is False
    assert may_publish([TaskState.CLAIMED]) is False
    assert may_publish([]) is True


def test_publish_payload_round_trips_through_the_pinned_schema() -> None:
    """S3: the publish record is the contract the workflow writes and the landing page reads back."""
    record = PublishRecord(
        table_id="acme$gold$labels_2026",
        namespace="gold",
        version=7,
        tag="v1",
        publish_id="pub_abc123",
        published_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        published_by="user:henry",
    )
    project = AnnotationProject(tenant="acme", slug="labels-2026", state=ProjectState.PUBLISHED, published=record)

    wire = project.model_dump_json()
    back = AnnotationProject.model_validate_json(wire)

    assert back.published is not None
    assert back.published.version == 7
    assert back.published.table_id == "acme$gold$labels_2026"
    assert back.published.published_at == record.published_at
    assert back.state is ProjectState.PUBLISHED
    assert back.fga_object == f"annotation_project:{project.project_id}"


# --------------------------------------------------------------------------------------------------
# Entities (§4)
# --------------------------------------------------------------------------------------------------


def test_the_authz_parent_is_the_tenant_not_the_child() -> None:
    """Create-on-parent: the door is `project:<tenant>`, because the child does not exist yet."""
    project = AnnotationProject(tenant="acme", slug="s")
    assert project.fga_parent == "project:acme"
    assert project.fga_object.startswith("annotation_project:")


def test_a_draft_holds_the_whole_shape_set_as_one_document() -> None:
    """§4.3: one document per (task, author) — not a row per shape. A save is one keyed write."""
    draft = Draft(
        task_id="t1",
        project_id="p1",
        author="user:dave",
        shapes=[
            Shape(shape_type="bbox", x=1, y=2, width=10, height=20, label="text_line"),
            Shape(shape_type="polygon", polygon=[0, 0, 1, 0, 1, 1], label="stamp"),
        ],
        revision=3,
    )
    back = Draft.model_validate_json(draft.model_dump_json())
    assert len(back.shapes) == 2
    assert back.revision == 3
    assert {s.shape_type for s in back.shapes} == {"bbox", "polygon"}
    # Every shape carries a stable identity, so a partial update can target one.
    assert all(s.shape_id for s in back.shapes)


def test_task_defaults_to_unassigned_with_no_assignee_and_no_lease() -> None:
    task = _task()
    assert task.state is TaskState.UNASSIGNED
    assert task.assignee is None
    assert task.lease_expires_at is None
    assert task.transitions == []


# --------------------------------------------------------------------------------------------------
# Legal-event derivation — what A1's "the UI renders the transitions the backend supplies" reads
# --------------------------------------------------------------------------------------------------


def test_legal_project_events_come_from_the_table_with_their_permissions() -> None:
    """FROZEN is the busy state: open, publish, archive — and NOT send. The permission travels with
    each event so the API can gate and the UI can explain, from ONE source."""
    from annotator.projects.machines import legal_project_events

    events = legal_project_events(ProjectState.FROZEN)

    assert {(e["event"], e["permission"]) for e in events} == {
        ("open", "can_manage"),
        ("publish", "can_publish"),
        ("archive", "can_manage"),
    }
    assert all(set(e) == {"event", "to", "permission"} for e in events)


def test_legal_project_events_include_send_only_where_send_is_legal() -> None:
    from annotator.projects.machines import legal_project_events

    labeling = {e["event"] for e in legal_project_events(ProjectState.LABELING)}
    frozen = {e["event"] for e in legal_project_events(ProjectState.FROZEN)}
    assert "send" in labeling
    assert "send" not in frozen


def test_legal_events_exclude_system_edges_so_no_ui_renders_a_button_for_them() -> None:
    """`publish_succeeded`/`publish_failed`/`lease_expired` are fired by the saga and the reminder —
    a rendered control for one would be a button that can only 403."""
    from annotator.projects.machines import legal_project_events, legal_task_events

    assert {e["event"] for e in legal_project_events(ProjectState.PUBLISHING)} == set()
    assert "lease_expired" not in {e["event"] for e in legal_task_events(TaskState.CLAIMED)}


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
