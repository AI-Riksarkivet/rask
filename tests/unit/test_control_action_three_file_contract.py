"""A named control action is a THREE-file change, and nothing checked that all three landed.

`.claude/skills/rask-notifications` spells the contract out and says why each file is load-bearing:

  1. `service_kit/control_events.py` — add the member to `ControlAction`, "or the envelope will not
     validate".
  2. `notifications/api/control_events.py` — add it to `NAMED_ACTIONS`, "or the lane files it IGNORED".
  3. `notifications/models.py` — add the matching `NotificationReason`, "because `as_delivery`
     constructs `NotificationReason(event.action)` and would otherwise **raise on every delivery**".

THE THREE DIRECTIONS ARE NOT EQUALLY DANGEROUS, and measuring that is what this file is for.

* A missing `NotificationReason` is **already fail-fast, and not by `as_delivery`**. `inbox.py:46`
  builds `_CONTROL_REASONS = frozenset(NotificationReason(a) for a in NAMED_ACTIONS)` at MODULE scope,
  so the service refuses to import at all. Verified by mutation: adding an action to `NAMED_ACTIONS`
  with no matching reason fails at COLLECTION with `ValueError: '...' is not a valid
  NotificationReason` (exit 2), long before any delivery. The skill's warning describes the runtime
  path and is right about the blast radius — on 2026-08-16 rows carrying three new reasons landed, the
  deployment rolled back, and the older enum turned them into `ValidationError` → `InboxUnreadable` →
  a **503 for the subject's entire inbox**, because list validation is all-or-nothing — but that is a
  ROLLBACK hazard (old code, new data), which no gate in this repo can catch. Forward, the import
  guard already holds. The assertion below is kept for the named message, not because it closes a hole.
* A missing `NAMED_ACTIONS` entry fails **SILENTLY**, and nothing else in the estate catches it. The
  lane files the event IGNORED with a SUCCESS ack: no retry, no error log, and the producer's own
  tests pass because the event was emitted perfectly and simply reached nobody. That is the one this
  file actually adds, and it is the estate's most expensive failure mode.

This checks the three declarations agree. It deliberately does NOT check the runtime path (`as_delivery`
building a reason from an action) — that belongs with the notifications service's own suite, and a gate
that reached across into it would be asserting someone else's contract from here.

`ControlAction` is deliberately the SUPERSET: an action may be emitted for console invalidation without
naming a person, and `_UNTARGETED_ACTIONS` records each such member with the reason it targets nobody —
so the set is stated rather than inferred from whatever happens to be missing.
"""

from __future__ import annotations

import pytest

from notifications.api.control_events import NAMED_ACTIONS
from notifications.models import NotificationReason
from service_kit.control_events import ControlAction


def _control_actions() -> set[str]:
    """Every member of the `ControlAction` literal."""
    from typing import get_args

    return set(get_args(ControlAction))


#: Actions that legitimately name NO PERSON, and are therefore absent from `NAMED_ACTIONS` on purpose.
#:
#: They share one rationale, so they are listed rather than described by a prefix rule: every one is an
#: OBJECT-LIFECYCLE event on a governed object — something was created, dropped, renamed, protected,
#: bound, purged. It names what changed, not who it changed things for. `rask-notifications` rules that
#: out explicitly under "What is deliberately NOT notified": "Actions naming no party — delivering every
#: catalog mutation recreates the estate-wide feed this plane exists to replace."
#:
#: The eight that ARE targeted are the ones where a PERSON's standing changed: the grant pair (someone
#: gained or lost access), the task quintet (someone was handed work or had it taken away), and the
#: promotion review (someone is being asked to decide). That is the line — a control event is targeted
#: when it changes what a specific person may do or must do, not when it changes an object.
#:
#: Enumerated rather than pattern-matched deliberately. A prefix rule (`table_*` is untargeted) would
#: pass a NEW `table_shared_with_user` without anyone noticing, which is the failure this gate exists to
#: prevent — and matching a name shape instead of the fact is the defect class this audit keeps finding.
_UNTARGETED_ACTIONS: frozenset[str] = frozenset(
    {
        # A gate declaration changes an OBJECT's configuration, not a person's standing — the same
        # shape as `transform_*` below. Nobody's access moved, so there is nobody to tell.
        "gate_deleted",
        "gate_set",
        "namespace_created",
        "namespace_dropped",
        "namespace_protected",
        "namespace_purged",
        "namespace_undropped",
        "namespace_unprotected",
        "policy_deleted",
        "policy_set",
        "project_created",
        "project_deleted",
        "table_created",
        "table_declared",
        "table_deregistered",
        "table_dropped",
        "table_protected",
        "table_published",
        "table_purged",
        "table_registered",
        "table_renamed",
        "table_undropped",
        "table_unprotected",
        "transform_deleted",
        "transform_set",
        "warehouse_activated",
        "warehouse_bound",
        "warehouse_created",
        "warehouse_deactivated",
        "warehouse_deleted",
    }
)


def test_every_named_action_is_a_real_control_action() -> None:
    """`NAMED_ACTIONS` naming something `ControlAction` does not means the lane waits for an envelope
    that can never validate — a targeting rule for an event that cannot exist."""
    actions = _control_actions()
    assert actions, "ControlAction resolved to nothing — the Literal moved and this gate is vacuous"

    orphans = sorted(NAMED_ACTIONS - actions)
    assert not orphans, f"NAMED_ACTIONS names actions ControlAction does not define: {orphans}. The envelope would fail validation before the lane ever saw it."


def test_every_named_action_has_a_notification_reason() -> None:
    """Already guaranteed at import — kept for the message, not the coverage.

    `inbox.py:46` builds `_CONTROL_REASONS` from `NAMED_ACTIONS` at module scope, so a missing reason
    stops the service importing rather than reaching a delivery. This restates it as a named assertion
    so the failure says WHICH contract broke instead of surfacing as a collection error.
    """
    reasons = {reason.value for reason in NotificationReason}
    missing = sorted(NAMED_ACTIONS - reasons)
    assert not missing, (
        f"these targeted actions have no NotificationReason: {missing}. `as_delivery` builds the "
        "reason from the action, so the first delivery raises — and because InboxRows validation is "
        "all-or-nothing, that is a 503 for the subject's ENTIRE inbox, not one dropped row."
    )


def test_every_targeted_control_action_is_declared_in_named_actions() -> None:
    """THE ONE THIS FILE EXISTS FOR — it fails silently and nothing else catches it.

    An action `ControlAction` defines but `NAMED_ACTIONS` omits is filed IGNORED with a SUCCESS ack.
    Nothing retries, nothing logs an error, and the producer's own tests pass — the event was emitted
    correctly and simply reached nobody.
    """
    unlisted = sorted(_control_actions() - set(NAMED_ACTIONS) - _UNTARGETED_ACTIONS)
    assert not unlisted, (
        f"these ControlActions are not in NAMED_ACTIONS: {unlisted}. The lane files them IGNORED with "
        "a SUCCESS ack, so they reach nobody and nothing reports it. Add them to NAMED_ACTIONS "
        "(and to NotificationReason), or add them to _UNTARGETED_ACTIONS to record that the action "
        "changes an object rather than a person's standing."
    )


@pytest.mark.parametrize("action", sorted(_UNTARGETED_ACTIONS))
def test_every_untargeted_exemption_still_names_a_real_action(action: str) -> None:
    assert action in _control_actions(), f"{action!r} is exempted as untargeted but ControlAction no longer defines it — delete the entry"


def test_the_three_declarations_are_all_non_empty() -> None:
    """Non-vacuity: an import that resolved to an empty set would satisfy every assertion above."""
    assert len(_control_actions()) >= 8, "ControlAction has fewer members than the estate ships"
    assert len(NAMED_ACTIONS) >= 8, "NAMED_ACTIONS is smaller than the actions the estate targets"
    assert len(list(NotificationReason)) >= 8, "NotificationReason lost members"
