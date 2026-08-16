"""The catalog control-plane change-event — a typed governance/metadata mutation notice.

Distinct from the OpenLineage **data** events (`catalog/core/lineage_emit.py`, which describe table
*writes*): this is the **control-plane** stream — a grant changed, a warehouse was deactivated, a policy
was set, a namespace/table was created/dropped/renamed. Those mutations already land in the durable audit
trail (#41, GreptimeDB); this is the real-time **subscribable** layer on top, so internal consumers (cache
invalidation, an in-estate reaction worker) and the admin console get a live feed (comparable catalogs ship
the same).

Shared here (`service_kit`) so producers (the catalog) and consumers import ONE model. The event is a
plain pointer payload (claim-check invariant — no data), published onto the existing Dapr/NATS bus by
`catalog/core/control_emit.py`; Dapr wraps it in a CloudEvent envelope at the sidecar (the same envelope the
lineage subscriber already speaks). An event is a **refresh hint**, never authoritative data: a consumer
re-reads state through the normal FGA-governed path, so a dropped/duplicated/late event only costs a
redundant re-read. `event_id` is the client-side dedupe key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


#: The Dapr pub/sub topic for control-plane events. Versioned (a breaking schema change → a new `.vN`),
#: mirroring `lineage.events.v1`. The catalog subscribes to this WITHOUT a queueGroupName, so every replica
#: receives every event (broadcast → each replica's ring buffer stays complete).
CONTROL_TOPIC = "catalog.control.v1"

#: The mutation that occurred. A `Literal` union (house style — no runtime enum) whose members double as the
#: wire values. Grouped by object: projects, grants, warehouses, policies, namespaces, tables.
#: `table_renamed` matters most to the UI — it invalidates every open view of that table; `project_deleted`
#: matters most to the shell, which must drop a tenant that no longer exists from every picker.
ControlAction = Literal[
    "grant_added",
    "grant_revoked",
    "project_created",
    "project_deleted",
    "warehouse_created",
    "warehouse_activated",
    "warehouse_deactivated",
    "warehouse_bound",
    "warehouse_deleted",
    "policy_set",
    "policy_deleted",
    "namespace_created",
    "namespace_dropped",
    "table_created",
    "table_dropped",
    "table_renamed",
    "table_registered",
    "table_deregistered",
    "table_declared",
    # #73 deletion protection on the table/namespace rungs — the same Decision-5 flag warehouses
    # carry, emitted so the console can show WHO armed or disarmed the safety on an object.
    "table_protected",
    "table_unprotected",
    "namespace_protected",
    "namespace_unprotected",
    # #75 the drop→undrop path: a recoverable drop and its recovery are both governance events.
    "table_undropped",
    # #96 the recoverable CASCADE: recovering a whole subtree is one governance event on its root.
    "namespace_undropped",
    # diff2 F10 item 6 — the EXPIRY PURGE has its own verbs. It reused `table_dropped` /
    # `namespace_dropped` with `extra.reason="trash_expired"`, which made an automated reclamation
    # indistinguishable from a person deleting the table unless a consumer thought to read `extra` —
    # and the console's feed does not. Two different facts were arriving under one name: "someone
    # decided to remove this" and "the grace period ran out and the sweep collected it". The second
    # has no actor (the emitter stamps `None`), is not appealable, and is the LAST event an object
    # ever produces, so conflating them mis-reports both the cause and the finality.
    #
    # Additive: `extra.reason` stays, so a consumer keying on it keeps working.
    "table_purged",
    "namespace_purged",
    # § D2 D-R2: the tag is the truth, this event is only the NOTIFICATION. A publication moves the
    # `published` ref, so it belongs to the same family as the other ref-plane mutations here — and a
    # consumer that misses it loses nothing, because the tag still answers "what is ready?".
    # `extra` carries {from_version, to_version}: the RANGE (D-R3) a consumer turns straight into a
    # row delta via `_row_created_at_version`, holding no bookmark of its own.
    "table_published",
    # ANNOTATION WORK, and the first control actions a service other than the catalog/maintenance pair
    # publishes. They belong in this vocabulary rather than a stream of their own for the reason the
    # grant actions do: both NAME a person and hand them (or take from them) something they must act on,
    # which is exactly what the notifications plane's `NAMED_ACTIONS` targets. `task_unassigned` is the
    # sharper half — an annotator holding a draft against a task that is no longer theirs discovers it by
    # losing the work, the same way a revoked grant is discovered by a 403 mid-task.
    "task_assigned",
    "task_unassigned",
    # THE OTHER DEPARTURE EDGES. `TASK_EDGES` has twelve transitions that take a task out of somebody's
    # hands; `release` was simply the first one wired, not a special case. These two cover the rest of
    # the HTTP-reachable ones, and each is a DISTINCT action rather than a reused `task_unassigned`
    # because the notifications panel RENDERS the reason as the row's label — telling somebody their
    # reviewed work was "unassigned" is a worse answer than the silence it replaces.
    #
    # `task_changes_requested` carries both review-side returns (`request_changes` from a reviewer,
    # `reopen` from a manager): from the submitter's side they are the same fact — work they had
    # finished is theirs again. `task_dropped` is the sharpest of all, because the item is DISCARDED:
    # the task actor keeps their draft, the index entry `saga.collect` enumerates is gone, and nothing
    # will ever look for that work again.
    "task_changes_requested",
    "task_dropped",
    # THE ONE NO PERSON CAUSES. A self-claimed task carries a lease (30 min by default, renewed on
    # every save); when it lapses the task returns to the pool and the holder is left with a draft
    # against work that is no longer theirs. An ASSIGNED task never expires, so this only ever names
    # somebody who took the work off the pool themselves — exactly the person with no reason to
    # suspect they lost it.
    #
    # It is fired by the task actor's own reminder, so its `actor` is `system:annotator` rather than a
    # principal. That is honest rather than limiting: the lane targets on `extra.subject` and never
    # reads `actor` at all.
    "task_lease_expired",
]

#: The kind of governed object the action targets — drives which console view invalidates. `project` is the
#: top of rask's hierarchy (project > warehouse > namespace > table) and became a first-class control object
#: when tenants got their own registry record (`open_hierarchy_lifecycle.md` Decision 1).
#: `annotation_task` is deliberately NOT `table`: a task is a unit of work inside an annotation project,
#: not a governed lakehouse object, and conflating them would send the console to invalidate a table view
#: for an assignment that changed no data.
ControlObjectType = Literal["project", "grant", "warehouse", "policy", "namespace", "table", "annotation_task"]


class CatalogControlEvent(BaseModel):
    """One control-plane mutation notice. Published AFTER the backend/FGA mutation succeeds; the `actor` is
    the VERIFIED principal from the OIDC token in scope (never self-asserted — the pub/sub topic is an
    internal catalog-only channel, so the subscriber trusts the catalog's stamp, exactly like lineage's
    trusted `author`)."""

    #: Client-side dedupe key (a redelivery carries the same id).
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    #: When the mutation happened (UTC).
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    action: ControlAction
    object_type: ControlObjectType
    #: The mutated object's canonical id (e.g. `warehouse:acme`, `table:db1$t`, `namespace:db1`, or the
    #: `table:<id>` a grant/policy is scoped to). What a consumer keys its invalidation off.
    object_id: str
    #: The verified principal that made the change (the OIDC sub, e.g. `user:alice`), or `None` on a
    #: service/unauthenticated mutation (auth-off dev).
    actor: str | None = None
    #: Action-specific detail — kept small (claim-check: pointers, never data). E.g. a grant's
    #: `{relation, subject}`, a rename's `{from, to}`, a warehouse bind's `{namespace}`.
    extra: dict[str, Any] = Field(default_factory=dict)
