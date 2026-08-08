"""The annotation-task actor — S6, the estate's first registered actor.

These tests drive the REAL actor class against a fake state manager and a recording reminder API, so
every behaviour below is provable without a Dapr sidecar, a placement service or a database. What
they cannot prove is the runtime guarantee itself — that Dapr routes one actor id to one pod and
serialises calls to it — which needs a live sidecar; that is the round-trip proof, not a unit test.

What they DO pin is everything the runtime cannot give us for free: that the transition table stays
the single source of truth, that the lease reminder is armed and disarmed on exactly the right edges,
and that a lapsed lease never costs an annotator their draft.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import pytest
from annotator.projects.actor import DRAFT_KEY, LEASE_REMINDER, TASK_KEY, AnnotationTaskActor
from annotator.projects.machines import IllegalTransition
from annotator.projects.models import Task, TaskState


class _FakeStateManager:
    """The actor's state partition, in a dict. `try_get_state` returns (found, value) like Dapr's."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.saves = 0

    async def try_get_state(self, key: str) -> tuple[bool, str | None]:
        return (key in self.store, self.store.get(key))

    async def set_state(self, key: str, value: str) -> None:
        self.store[key] = value

    async def save_state(self) -> None:
        self.saves += 1


class _Actor(AnnotationTaskActor):
    """The real actor with its Dapr plumbing replaced — state and reminders recorded in memory."""

    def __init__(self) -> None:  # noqa: D107 - deliberately bypasses Actor.__init__ (needs a runtime)
        # `sm` is the TYPED handle the assertions read; `_state_manager` is the same object cast into
        # the slot the actor reads. Casting only at the injection point keeps the double's own surface
        # honest — asserting through `_state_manager` would have to fight the base class's type.
        self.sm = _FakeStateManager()
        self._state_manager = cast(Any, self.sm)
        self.reminders: list[tuple[str, float]] = []
        self.unregistered: list[str] = []

    async def register_reminder(
        self,
        name: str,
        state: bytes,
        due_time: timedelta,
        period: timedelta | None = None,
        ttl: timedelta | None = None,
        failure_policy: Any = None,
    ) -> None:
        """Signature mirrors `Actor.register_reminder` exactly. A narrower override would type-error,
        and — worse — would stop compiling the day the actor starts passing `failure_policy`."""
        self.reminders.append((name, due_time.total_seconds()))

    async def unregister_reminder(self, name: str) -> None:
        self.unregistered.append(name)


def _task(**kw: Any) -> dict[str, Any]:
    base = Task(
        project_id="p1",
        source={"kind": "chunks", "keys": ["doc_1"]},  # type: ignore[arg-type]
        media={"kind": "image", "image_url": "s3://b/p.jpg"},  # type: ignore[arg-type]
    ).model_dump(mode="json")
    base.update(kw)
    return base


async def _state(actor: _Actor) -> dict[str, Any]:
    """`get()` is Optional by contract. Assert once here rather than suppressing at each call site —
    a None would then be a named failure ("the actor lost its state") instead of a TypeError."""
    task = await actor.get()
    assert task is not None, "the actor lost its state"
    return task


async def _seeded() -> _Actor:
    actor = _Actor()
    await actor.seed(_task())
    return actor


async def _claimed() -> _Actor:
    """A draft is writable only from CLAIMED — the actor enforces that itself, so a fixture that
    saves a draft has to be in the state the machine allows it from."""
    actor = await _seeded()
    await actor.fire({"event": "claim", "actor": "gina"})
    return actor


# --------------------------------------------------------------------------------------------------
# State and the machine
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_is_idempotent_so_a_redelivered_send_cannot_reset_a_claim() -> None:
    """Send arrives twice (JetStream redelivers). The second must not wipe an in-progress claim."""
    actor = await _seeded()
    await actor.fire({"event": "claim", "actor": "gina"})

    again = await actor.seed(_task())

    assert again["state"] == TaskState.CLAIMED
    assert again["assignee"] == "gina", "re-seeding reset a task someone was holding"


@pytest.mark.asyncio
async def test_state_round_trips_through_the_state_manager() -> None:
    actor = await _seeded()
    await actor.fire({"event": "claim", "actor": "gina"})

    stored = json.loads(actor.sm.store[TASK_KEY])
    assert stored["state"] == TaskState.CLAIMED
    assert (await _state(actor))["state"] == TaskState.CLAIMED
    assert actor.sm.saves >= 2  # seed + claim both persisted


@pytest.mark.asyncio
async def test_an_illegal_edge_raises_rather_than_being_reimplemented_here() -> None:
    """The actor consults TASK_EDGES; it does not restate the machine. Reviewing an unsubmitted task
    must fail the same way `task_transition` fails."""
    actor = await _seeded()
    with pytest.raises(IllegalTransition):
        await actor.fire({"event": "accept", "actor": "carol"})


@pytest.mark.asyncio
async def test_submit_honours_the_review_required_captured_on_the_TASK() -> None:
    """Captured at send time from the project — never read from the fire payload. A payload-supplied
    `review_required` would let the submitting annotator waive their own review and self-accept."""
    waived = _Actor()
    await waived.seed(_task(review_required=False))
    await waived.fire({"event": "claim", "actor": "gina"})
    out = await waived.fire({"event": "submit", "actor": "gina"})
    assert out["state"] == TaskState.ACCEPTED  # review waived on the task → straight to accepted

    required = await _seeded()  # the default is review_required=True
    await required.fire({"event": "claim", "actor": "gina"})
    out2 = await required.fire({"event": "submit", "actor": "gina"})
    assert out2["state"] == TaskState.IN_REVIEW
    assert out2["submitted_by"] == "gina"


@pytest.mark.asyncio
async def test_a_payload_cannot_override_the_tasks_review_requirement() -> None:
    """The self-accept hole, closed. The task says review IS required; a fire payload claiming
    otherwise must be ignored entirely."""
    actor = await _seeded()
    await actor.fire({"event": "claim", "actor": "gina"})

    out = await actor.fire({"event": "submit", "actor": "gina", "review_required": False})

    assert out["state"] == TaskState.IN_REVIEW, "a request payload waived the task's review requirement"


@pytest.mark.asyncio
async def test_every_transition_is_appended_to_the_audit_trail() -> None:
    actor = await _seeded()
    await actor.fire({"event": "claim", "actor": "gina"})
    await actor.fire({"event": "submit", "actor": "gina"})
    trail = (await _state(actor))["transitions"]
    assert [t["event"] for t in trail] == ["claim", "submit"]
    assert trail[0]["from"] == TaskState.UNASSIGNED and trail[0]["to"] == TaskState.CLAIMED


# --------------------------------------------------------------------------------------------------
# The lease reminder — armed and disarmed on exactly the right edges
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_arms_the_lease_reminder_with_the_project_lease() -> None:
    actor = await _seeded()
    await actor.fire({"event": "claim", "actor": "gina", "lease_seconds": 900})
    assert actor.reminders == [(LEASE_REMINDER, 900.0)]


@pytest.mark.asyncio
async def test_assign_pins_the_task_and_arms_nothing() -> None:
    """§4.2: `lease_expires_at is None` while CLAIMED means manager-pinned — it never expires."""
    actor = await _seeded()
    out = await actor.fire({"event": "assign", "actor": "henry"})
    assert out["state"] == TaskState.CLAIMED
    assert out["lease_expires_at"] is None
    assert actor.reminders == [], "a pinned task must not arm an expiry"


@pytest.mark.asyncio
async def test_saving_a_draft_renews_the_lease() -> None:
    actor = await _seeded()
    await actor.fire({"event": "claim", "actor": "gina", "lease_seconds": 60})
    await actor.fire({"event": "save_draft", "actor": "gina", "lease_seconds": 60})
    assert len(actor.reminders) == 2, "save_draft must re-arm, not let the lease run down"


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["submit", "release", "skip"])
async def test_leaving_claimed_disarms_the_reminder(event: str) -> None:
    """A reminder left armed would expire a task that already moved on — outliving what it guarded."""
    actor = await _seeded()
    await actor.fire({"event": "claim", "actor": "gina"})
    await actor.fire({"event": event, "actor": "gina"})
    assert LEASE_REMINDER in actor.unregistered


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["submit", "release", "skip"])
async def test_a_failed_store_leaves_the_lease_reminder_armed(event: str) -> None:
    """Disarm AFTER persist, never before. If the store raises, the persisted state is still CLAIMED
    and the reminder must still be armed — the reverse order strands a claimed task with no
    self-expiry, exactly what the lease reminder exists to prevent. (open_dapr.md §2.6.)"""
    actor = await _claimed()

    async def _boom() -> None:
        raise RuntimeError("state store down")

    actor.sm.save_state = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await actor.fire({"event": event, "actor": "gina"})

    assert LEASE_REMINDER not in actor.unregistered, "a failed store disarmed the safety net"


@pytest.mark.asyncio
async def test_an_expired_lease_returns_the_task_and_KEEPS_the_draft() -> None:
    """The whole point of a lease: the work is not lost, only the hold on it."""
    actor = await _seeded()
    await actor.fire({"event": "claim", "actor": "gina"})
    await actor.save_draft({"task_id": "t1", "project_id": "p1", "author": "gina", "shapes": [{"shape_type": "bbox", "x": 1, "y": 2, "width": 3, "height": 4}]})

    await actor.receive_reminder(LEASE_REMINDER, b"", timedelta(0), timedelta(0))

    task = await _state(actor)
    assert task["state"] == TaskState.UNASSIGNED
    assert task["assignee"] is None
    draft = await actor.get_draft()
    assert draft is not None and len(draft["shapes"]) == 1, "an expired lease destroyed the draft"


@pytest.mark.asyncio
async def test_a_stale_reminder_for_a_moved_on_task_is_a_no_op() -> None:
    """A reminder can outrace its unregister. Firing it must not drag a submitted task backwards."""
    actor = await _seeded()
    await actor.fire({"event": "claim", "actor": "gina"})
    await actor.fire({"event": "submit", "actor": "gina"})

    await actor.receive_reminder(LEASE_REMINDER, b"", timedelta(0), timedelta(0))

    assert (await _state(actor))["state"] == TaskState.IN_REVIEW


# --------------------------------------------------------------------------------------------------
# The draft — one document, etag-guarded
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_draft_is_one_write_for_the_whole_shape_set() -> None:
    actor = await _claimed()
    out = await actor.save_draft({"task_id": "t1", "project_id": "p1", "author": "gina", "shapes": [{"shape_type": "bbox"}, {"shape_type": "polygon"}]})
    assert out["revision"] == 1
    assert len(json.loads(actor.sm.store[DRAFT_KEY])["shapes"]) == 2


@pytest.mark.asyncio
async def test_a_stale_base_revision_is_refused_so_two_tabs_cannot_clobber() -> None:
    """`revision` is the etag. Saving against a revision that has moved on is the 409."""
    actor = await _claimed()
    await actor.save_draft({"task_id": "t1", "project_id": "p1", "author": "gina", "shapes": []})
    await actor.save_draft({"task_id": "t1", "project_id": "p1", "author": "gina", "shapes": [], "base_revision": 1})
    with pytest.raises(IllegalTransition):
        await actor.save_draft({"task_id": "t1", "project_id": "p1", "author": "gina", "shapes": [], "base_revision": 1})


# --------------------------------------------------------------------------------------------------
# Reporting to the project index
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_transition_reports_its_new_state_to_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """The project index is what makes publish decidable, so every transition must reach it."""
    reported: list[dict[str, Any]] = []

    class _ProjectProxy:
        # WIRE name, as the real ActorProxy dispatches — the typed adapter translates to this,
        # so a pythonic method here would prove a call path that does not exist in-cluster.
        async def TaskStateChanged(self, payload: dict[str, Any]) -> dict[str, Any]:  # noqa: N802 - Dapr wire name
            reported.append(payload)
            return {}

    import dapr.actor

    monkeypatch.setattr(dapr.actor.ActorProxy, "create", classmethod(lambda cls, *a, **k: _ProjectProxy()))
    actor = await _seeded()

    await actor.fire({"event": "claim", "actor": "gina"})
    await actor.fire({"event": "submit", "actor": "gina"})

    assert [r["state"] for r in reported] == [TaskState.CLAIMED, TaskState.IN_REVIEW]


@pytest.mark.asyncio
async def test_an_unreachable_project_does_not_fail_the_annotator_s_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    """A submit must not fail because the project actor is briefly unreachable. The cost is a stale
    index — which BLOCKS a publish rather than allowing one, and the workflow re-reads besides."""

    class _Broken:
        async def TaskStateChanged(self, payload: dict[str, Any]) -> dict[str, Any]:  # noqa: N802 - Dapr wire name
            raise RuntimeError("sidecar unreachable")

    import dapr.actor

    monkeypatch.setattr(dapr.actor.ActorProxy, "create", classmethod(lambda cls, *a, **k: _Broken()))
    actor = await _seeded()

    out = await actor.fire({"event": "claim", "actor": "gina"})

    assert out["state"] == TaskState.CLAIMED, "an unreachable project rolled back the annotator's claim"
    assert (await _state(actor))["state"] == TaskState.CLAIMED, "the task's own state was not persisted"


@pytest.mark.asyncio
async def test_shape_ids_are_minted_at_save_and_PERSISTED_so_a_republish_is_stable() -> None:
    """`Shape.shape_id` has a `default_factory`, so an id is minted whenever a shape is PARSED
    without one. The publish saga's replay-determinism therefore rests on the actor persisting the
    id at save time — otherwise every re-read of a draft would mint new ids and a retried publish
    would write a different `annotation_id` for the same annotation.

    That dependency is invisible from the saga, which is why it is pinned here: a change to
    `save_draft` that dropped shape ids would break a crash-recovery property two modules away.
    """
    actor = await _claimed()
    await actor.save_draft({"task_id": "t1", "project_id": "p1", "author": "gina", "shapes": [{"shape_type": "bbox"}, {"shape_type": "polygon"}]})

    first = await actor.get_draft()
    second = await actor.get_draft()

    assert first is not None and second is not None
    ids = [s["shape_id"] for s in first["shapes"]]
    assert all(ids), "a shape was stored without an id"
    assert ids == [s["shape_id"] for s in second["shapes"]], "re-reading a draft minted new shape ids"
    assert len(set(ids)) == 2, "two shapes collided on one id"


@pytest.mark.asyncio
async def test_assign_names_the_recipient_not_the_manager_who_fired_it() -> None:
    """§5.2's `assign` is the ONE manager-driven distribution mechanism in the plane. Setting
    `assignee = actor` made a manager able to assign only to themselves, quietly turning it into a
    self-claim — the "assign a user to a task" feature, present in the state machine and absent in
    behaviour."""
    actor = await _seeded()

    out = await actor.fire({"event": "assign", "actor": "henry", "assignee": "gina"})

    assert out["assignee"] == "gina", "the manager assigned the task to themselves"
    assert out["transitions"][-1]["by"] == "henry", "the manager is still the recorded actor"
    assert out["lease_expires_at"] is None, "a pinned task must not take a lease"


@pytest.mark.asyncio
async def test_assign_without_a_named_recipient_falls_back_to_the_actor() -> None:
    """A manager assigning with no name is taking it themselves — legal, and better than an empty
    assignee, which would be a CLAIMED task nobody holds."""
    actor = await _seeded()
    out = await actor.fire({"event": "assign", "actor": "henry"})
    assert out["assignee"] == "henry"


@pytest.mark.asyncio
async def test_request_changes_appends_the_reviewers_reason() -> None:
    """§5.2: `request_changes` appends a `ReviewNote`. Without it the annotator gets the task back
    with no statement of what to change, which makes the whole changes_requested loop useless."""
    actor = await _seeded()
    await actor.fire({"event": "claim", "actor": "gina"})
    await actor.fire({"event": "submit", "actor": "gina"})

    out = await actor.fire({"event": "request_changes", "actor": "carol", "message": "the second box is off by a line", "shape_ids": ["s1"]})

    assert out["state"] == TaskState.CHANGES_REQUESTED
    assert len(out["review_notes"]) == 1
    note = out["review_notes"][0]
    assert note["by"] == "carol" and note["message"] == "the second box is off by a line"
    assert note["shape_ids"] == ["s1"]


@pytest.mark.asyncio
async def test_review_notes_are_append_only_across_rounds() -> None:
    """Two rounds of review must leave two notes — the history of what was asked for is the record."""
    actor = await _seeded()
    for message in ("first pass", "second pass"):
        await actor.fire({"event": "claim", "actor": "gina"})
        await actor.fire({"event": "submit", "actor": "gina"})
        await actor.fire({"event": "request_changes", "actor": "carol", "message": message})

    notes = (await _state(actor))["review_notes"]
    assert [n["message"] for n in notes] == ["first pass", "second pass"]


@pytest.mark.asyncio
async def test_accepting_writes_no_review_note() -> None:
    """Only `request_changes` carries a reason. An accept with an empty note would be noise in an
    append-only structure."""
    actor = await _seeded()
    await actor.fire({"event": "claim", "actor": "gina"})
    await actor.fire({"event": "submit", "actor": "gina"})
    out = await actor.fire({"event": "accept", "actor": "carol"})
    assert out["review_notes"] == []


@pytest.mark.asyncio
async def test_two_tabs_cannot_clobber_by_OMITTING_base_revision() -> None:
    """The etag was opt-in: the conflict check was skipped entirely when `base_revision` was absent,
    so the promise "two tabs of the same annotator cannot lose each other's work" held only for
    clients that volunteered to be checked.

    Both tabs load revision 1. Tab A saves 30 shapes (revision 2). Tab B saves its stale 12 with no
    `base_revision` — that must be a conflict, not a silent 200 that discards A's work.
    """
    actor = await _claimed()
    await actor.save_draft({"task_id": "t1", "project_id": "p1", "author": "gina", "shapes": [{"shape_type": "bbox"}]})
    await actor.save_draft({"task_id": "t1", "project_id": "p1", "author": "gina", "base_revision": 1, "shapes": [{"shape_type": "bbox"}] * 30})

    with pytest.raises(IllegalTransition, match="without base_revision"):
        await actor.save_draft({"task_id": "t1", "project_id": "p1", "author": "gina", "shapes": [{"shape_type": "tag"}]})

    surviving = await actor.get_draft()
    assert surviving is not None and len(surviving["shapes"]) == 30, "the omitted-etag save clobbered 30 shapes"


@pytest.mark.asyncio
async def test_the_first_save_needs_no_base_revision() -> None:
    """There is nothing to be stale against before a draft exists, so requiring it would make the
    first save impossible rather than safe."""
    actor = await _claimed()
    out = await actor.save_draft({"task_id": "t1", "project_id": "p1", "author": "gina", "shapes": []})
    assert out["revision"] == 1


# --------------------------------------------------------------------------------------------------
# The lease duration comes from the PROJECT, captured at send — not from whoever clicks Claim
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_defaults_to_the_lease_captured_from_the_project() -> None:
    """`AnnotationProject.lease_seconds` is project config (§4.1). Until this test it was stored and
    never read: every claim ran on the CLIENT's default, so the setting was dead. The task captures
    it at send (like `review_required`) and the claim path reads the capture."""
    from datetime import UTC, datetime

    actor = _Actor()
    await actor.seed(_task(lease_seconds=600))

    out = await actor.fire({"event": "claim", "actor": "gina"})

    expires = datetime.fromisoformat(out["lease_expires_at"])
    seconds = (expires - datetime.now(UTC)).total_seconds()
    assert 540 <= seconds <= 600, f"claim ignored the captured lease ({seconds:.0f}s left, expected ~600)"
    assert actor.reminders[-1] == (LEASE_REMINDER, 600.0), "the expiry reminder must match the captured lease"


@pytest.mark.asyncio
async def test_an_explicit_lease_seconds_still_wins_over_the_capture() -> None:
    from datetime import UTC, datetime

    actor = _Actor()
    await actor.seed(_task(lease_seconds=600))

    out = await actor.fire({"event": "claim", "actor": "gina", "lease_seconds": 120})

    expires = datetime.fromisoformat(out["lease_expires_at"])
    seconds = (expires - datetime.now(UTC)).total_seconds()
    assert seconds <= 120.5


# --------------------------------------------------------------------------------------------------
# The task ONTOLOGY — the submit-time output contract, enforced in the actor for ANY caller
# --------------------------------------------------------------------------------------------------


async def _claimed_with_ontology(ontology: dict[str, Any], shapes: list[dict[str, Any]]) -> _Actor:
    actor = _Actor()
    await actor.seed(_task(ontology=ontology))
    await actor.fire({"event": "claim", "actor": "gina"})
    if shapes:
        await actor.save_draft({"task_id": "t1", "project_id": "p1", "author": "gina", "shapes": shapes})
    return actor


@pytest.mark.asyncio
async def test_a_submit_violating_the_ontology_is_refused_with_the_violation_named() -> None:
    # Tools are declared PER CLASS now, so this says what it always meant: "portrait" is a tag.
    ontology = {"kind": "classification", "classes": [{"name": "portrait", "tools": ["tag"]}]}
    actor = await _claimed_with_ontology(ontology, [{"shape_type": "bbox", "label": "portrait"}])

    with pytest.raises(IllegalTransition, match="portrait"):
        await actor.fire({"event": "submit", "actor": "gina"})
    assert (await _state(actor))["state"] == "claimed", "a refused submit must not move the task"


@pytest.mark.asyncio
async def test_a_conforming_submit_passes() -> None:
    ontology = {"classes": [{"name": "portrait", "tools": ["bbox"], "required": True}]}
    actor = await _claimed_with_ontology(ontology, [{"shape_type": "bbox", "label": "portrait"}])

    result = await actor.fire({"event": "submit", "actor": "gina"})
    assert result["state"] in {"in_review", "accepted"}


@pytest.mark.asyncio
async def test_a_draftless_submit_is_refused() -> None:
    """No draft = an empty shape set.

    This used to assert the `required_labels` message, and that reading was the bug the audit
    found: a required label was the ONLY rule an empty set could fail, so a task that declared none
    accepted a submission with nothing in it. The empty-set rule now catches this first — a strictly
    earlier refusal that no longer depends on the task having declared a required class."""
    ontology = {"classes": [{"name": "portrait", "required": True}]}
    actor = await _claimed_with_ontology(ontology, [])

    with pytest.raises(IllegalTransition, match="at least one annotation"):
        await actor.fire({"event": "submit", "actor": "gina"})


@pytest.mark.asyncio
async def test_an_ontology_that_CONSTRAINS_NOTHING_never_blocks_a_submit() -> None:
    """An ontology with no classes is the pre-ontology behaviour, exactly.

    This replaces `enforce=False`, and it is a strictly better shape: the flag was a document-wide
    kill switch that voided declarations someone had explicitly written, so a project could declare
    classes, tools and required attributes and have every one ignored. Here there is nothing to
    ignore — an empty ontology declares nothing, and everything declared is applied."""
    actor = await _claimed_with_ontology({}, [{"shape_type": "text", "label": ""}])
    assert (await actor.fire({"event": "submit", "actor": "gina"}))["state"] in {"in_review", "accepted"}


# --------------------------------------------------------------------------------------------------
# The audit's critical: the draft's state precondition must live in the ACTOR, not only in HTTP
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_draft_cannot_be_written_after_the_task_left_CLAIMED() -> None:
    """The TOCTOU the audit proved: the endpoint reads the state and invokes the actor as two
    steps, so a concurrent submit can land in between — the ontology validates the conforming
    draft, the task moves on, and the late write puts never-validated shapes into a reviewed task
    that publish then carries into silver. Inside the actor's own turn the state cannot move."""
    ontology = {"classes": [{"name": "cat", "tools": ["tag"], "required": True}]}
    actor = await _claimed_with_ontology(ontology, [{"shape_type": "tag", "label": "cat"}])
    assert (await actor.fire({"event": "submit", "actor": "gina"}))["state"] in {"in_review", "accepted"}

    with pytest.raises(IllegalTransition, match="save_draft"):
        await actor.save_draft(
            {
                "task_id": "t1",
                "project_id": "p1",
                "author": "gina",
                "shapes": [{"shape_type": "bbox"}, {"shape_type": "bbox"}, {"shape_type": "bbox"}],
                "base_revision": 1,
            }
        )
    assert [s["shape_type"] for s in json.loads(actor.sm.store[DRAFT_KEY])["shapes"]] == ["tag"]


@pytest.mark.asyncio
async def test_a_CONSTRAINED_ontology_refuses_a_draftless_submit_even_with_nothing_required() -> None:
    """The other half of the same critical: a required class was the ONLY rule an empty shape set
    could fail, and the create dialog filled that from an OPTIONAL textarea."""
    ontology = {"kind": "reading-order", "classes": [{"name": "line", "tools": ["bbox"]}]}
    actor = await _claimed_with_ontology(ontology, [])
    with pytest.raises(IllegalTransition, match="at least one annotation"):
        await actor.fire({"event": "submit", "actor": "gina"})


# --------------------------------------------------------------------------------------------------
# Relations: declared in the ontology, and STORABLE — a contract nobody can satisfy is not a contract
# --------------------------------------------------------------------------------------------------


KIE = {
    "kind": "document-question-answering",
    "classes": [{"name": "key", "tools": ["bbox"]}, {"name": "value", "tools": ["bbox"]}],
    "relations": [{"name": "answers", "from_classes": ["key"], "to_classes": ["value"], "required": True}],
}
KIE_SHAPES = [
    {"shape_id": "s1", "shape_type": "bbox", "label": "key"},
    {"shape_id": "s2", "shape_type": "bbox", "label": "value"},
]


@pytest.mark.asyncio
async def test_a_link_SURVIVES_the_draft_write() -> None:
    """The gap that made a required relation impossible to satisfy.

    `Draft` carried only `shapes`, and `save_draft` builds the model field by field — so a `links`
    key was dropped on the floor with no error. The submit check then read an always-empty link list,
    which meant an ontology declaring a REQUIRED relation refused every submission that could ever be
    made. Declared, enforced, and unsatisfiable: the worst of the three.
    """
    actor = _Actor()
    await actor.seed(_task(ontology=KIE))
    await actor.fire({"event": "claim", "actor": "gina"})
    await actor.save_draft(
        {
            "task_id": "t1",
            "project_id": "p1",
            "author": "gina",
            "shapes": KIE_SHAPES,
            "links": [{"name": "answers", "from_shape": "s1", "to_shape": "s2"}],
        }
    )

    stored = await actor.get_draft()
    assert stored is not None
    assert stored["links"] == [{"name": "answers", "from_shape": "s1", "to_shape": "s2"}], "the link was dropped by the draft write"


@pytest.mark.asyncio
async def test_a_KIE_submit_with_its_link_is_ACCEPTED() -> None:
    actor = _Actor()
    await actor.seed(_task(ontology=KIE))
    await actor.fire({"event": "claim", "actor": "gina"})
    await actor.save_draft(
        {
            "task_id": "t1",
            "project_id": "p1",
            "author": "gina",
            "shapes": KIE_SHAPES,
            "links": [{"name": "answers", "from_shape": "s1", "to_shape": "s2"}],
        }
    )

    result = await actor.fire({"event": "submit", "actor": "gina"})
    assert result["state"] in {"in_review", "accepted"}


@pytest.mark.asyncio
async def test_a_KIE_submit_WITHOUT_the_required_link_is_refused_by_name() -> None:
    """Still enforced — the fix is that it is now SATISFIABLE, not that it stopped being a rule."""
    actor = _Actor()
    await actor.seed(_task(ontology=KIE))
    await actor.fire({"event": "claim", "actor": "gina"})
    await actor.save_draft({"task_id": "t1", "project_id": "p1", "author": "gina", "shapes": KIE_SHAPES})

    with pytest.raises(IllegalTransition, match="answers"):
        await actor.fire({"event": "submit", "actor": "gina"})


@pytest.mark.asyncio
async def test_a_link_pointing_the_WRONG_WAY_is_refused() -> None:
    """`answers` is directed key→value. The endpoint classes are the closed set that makes a link
    checkable the same way a label is."""
    actor = _Actor()
    await actor.seed(_task(ontology=KIE))
    await actor.fire({"event": "claim", "actor": "gina"})
    await actor.save_draft(
        {
            "task_id": "t1",
            "project_id": "p1",
            "author": "gina",
            "shapes": KIE_SHAPES,
            "links": [{"name": "answers", "from_shape": "s2", "to_shape": "s1"}],
        }
    )

    with pytest.raises(IllegalTransition, match="source"):
        await actor.fire({"event": "submit", "actor": "gina"})
