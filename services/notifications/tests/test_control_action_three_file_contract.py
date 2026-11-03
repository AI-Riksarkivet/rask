"""A control action this client targets is a THREE-file change, and only a check like this one sees all three.

The lakehouse publishes its control vocabulary as `service_kit.control_events.ControlAction`. Which of those actions
name a person to tell, and how each is filed, is this client's:

  1. `service_kit/control_events.py`: the member of `ControlAction`, or the envelope does not validate.
  2. `notifications/api/control_events.py`: the action in `NAMED_ACTIONS` (or `ERASING_ACTIONS`), or the lane files it
     IGNORED.
  3. `notifications/models.py`: the matching `NotificationReason`, because `as_delivery` builds
     `NotificationReason(event.action)` and raises without one.

THE DIRECTIONS ARE NOT EQUALLY DANGEROUS, and that is why this file checks only one of them.

* A missing `NotificationReason` already fails fast, and not in `as_delivery`: `api/inbox.py` builds
  `_CONTROL_REASONS = frozenset(NotificationReason(a) for a in NAMED_ACTIONS)` at MODULE scope, so the service refuses
  to import at all. Verified by mutation: an action added to `NAMED_ACTIONS` with no matching reason fails at
  COLLECTION with `ValueError: '...' is not a valid NotificationReason` (exit 2), long before any delivery. A rollback is
  the hazard that remains (old code, new data): on 2026-08-16 rows carrying three new reasons landed, the deployment
  rolled back, and the older enum turned them into `ValidationError` → `InboxUnreadable` → a **503 for the subject's
  entire inbox**, because list validation is all-or-nothing. No gate in this repository can catch that.
* A missing `NAMED_ACTIONS` entry fails SILENTLY: the lane files the event IGNORED with a SUCCESS ack, so nothing
  retries, nothing logs an error, and the producer's own tests pass because the event was emitted correctly and reached
  nobody. That is the direction this file closes.

The runtime path, `as_delivery` building a reason from an action, is driven by `test_control_events.py`.

`ControlAction` is deliberately the SUPERSET: an action may be emitted for a console's invalidation without naming a
person, and `_UNTARGETED_ACTIONS` records each such member with the reason it targets nobody, so the set is stated
rather than inferred from whatever happens to be missing. An action in `ERASING_ACTIONS` names a person and tells them
nothing: the lane erases what it holds for them (`principal_deleted`, CTL-022).
"""

from __future__ import annotations

from notifications.api.control_events import ERASING_ACTIONS, NAMED_ACTIONS
from service_kit.control_events import ControlAction


def _control_actions() -> set[str]:
    """Every member of the `ControlAction` literal."""
    from typing import get_args

    return set(get_args(ControlAction))


#: Actions that legitimately name NO PERSON, and are therefore absent from `NAMED_ACTIONS` on purpose.
#:
#: They share one rationale, so they are listed rather than described by a prefix rule: every one is an
#: OBJECT-LIFECYCLE event on a governed object — something was created, dropped, renamed, protected,
#: bound, purged, published, or had a ref or a gate move. It names what changed, not who it changed things for, and
#: delivering every catalog mutation would recreate the estate-wide feed a targeted inbox exists to replace.
#:
#: The three that ARE targeted are the ones where a PERSON's standing changed: the grant pair (someone gained or lost
#: access) and the erasure release notice (someone loses pinned work at a deadline). That is the line — a control event
#: is targeted when it changes what a specific person may do or must do, not when it changes an object.
#:
#: Enumerated rather than pattern-matched deliberately. A prefix rule (`table_*` is untargeted) would
#: pass a NEW `table_shared_with_user` without anyone noticing, which is the failure this gate exists to
#: prevent, and matching a name shape instead of the fact is the defect class this check is here to catch.
_UNTARGETED_ACTIONS: frozenset[str] = frozenset(
    {
        # A project's role minted or ended ([[LH-557]]): the role is an object; what a person gains or loses by it is
        # announced as the grant pair, naming them.
        "role_created",
        "role_deleted",
        # The ref plane ([[LH-056]]): a branch or tag moved, which is an OBJECT changing, not a
        # person's standing. Same family as the namespace lifecycle below.
        "table_branch_created",
        "table_branch_deleted",
        "table_tag_created",
        "table_tag_updated",
        "table_tag_deleted",
        # A gate declaration changes an OBJECT's configuration, not a person's standing. Nobody's access moved,
        # so there is nobody to tell.
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
        # The tag gate's hold lifecycle changes a table's publication, not a person's standing.
        "promotion_expired",
        "promotion_held",
        "promotion_rejected",
        "project_created",
        "project_deleted",
        "project_protected",
        "project_unprotected",
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
        "warehouse_activated",
        "warehouse_bound",
        "warehouse_created",
        # The exact inverse of `warehouse_bound` above, and classified with it: detaching a namespace
        # from its warehouse changes an OBJECT's routing, not a person's standing. Nobody's access
        # moved — every grant on the namespace and its tables survives the unbind — so there is nobody
        # the lane could target.
        "warehouse_unbound",
        "warehouse_deactivated",
        "warehouse_deleted",
        "warehouse_protected",
        "warehouse_unprotected",
    }
)


def test_every_targeted_control_action_is_declared_in_named_actions() -> None:
    """The silent direction: an action `ControlAction` defines but `NAMED_ACTIONS` omits is filed IGNORED with a SUCCESS
    ack. Nothing retries, nothing logs an error, and the producer's own tests pass — the event was emitted correctly
    and simply reached nobody.
    """
    unlisted = sorted(_control_actions() - set(NAMED_ACTIONS) - ERASING_ACTIONS - _UNTARGETED_ACTIONS)
    assert not unlisted, (
        f"these ControlActions are not in NAMED_ACTIONS: {unlisted}. The lane files them IGNORED with "
        "a SUCCESS ack, so they reach nobody and nothing reports it. Add them to NAMED_ACTIONS "
        "(and to NotificationReason), or add them to _UNTARGETED_ACTIONS to record that the action "
        "changes an object rather than a person's standing."
    )
