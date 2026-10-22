"""The catalog control-plane change-event — a typed governance/metadata mutation notice.

Distinct from the OpenLineage **data** events (`catalog/core/lineage_emit.py`, which describe table
*writes*): this is the **control-plane** stream — a grant changed, a warehouse was deactivated, a policy
was set, a namespace/table was created/dropped/renamed. Those mutations already land in the durable audit
trail (#41, GreptimeDB); this is the real-time **subscribable** layer on top, so internal consumers (cache
invalidation, an in-estate reaction worker) and the admin console get a live feed (comparable catalogs ship
the same).

Shared here (`service_kit`) so producers (the catalog) and consumers import ONE model. The event is a
plain pointer payload (claim-check invariant — no data), published onto the existing Dapr/NATS bus by
`service_kit.control_emit`; Dapr wraps it in a CloudEvent envelope at the sidecar (the same envelope the
lineage subscriber already speaks). An event is a **refresh hint**, never authoritative data: a consumer
re-reads state through the normal FGA-governed path, so a dropped/duplicated/late event only costs a
redundant re-read. `event_id` is the client-side dedupe key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, assert_never
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


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
    # A namespace DETACHED from its warehouse without dropping it — the repair for a binding that
    # outlived what it pointed at. It is its own action rather than a `warehouse_bound` with an empty
    # target because the binding cache is positive-and-forever: a replica that never hears the specific
    # word keeps routing a namespace at a warehouse nothing binds it to any more.
    "warehouse_unbound",
    "warehouse_deleted",
    "policy_set",
    "policy_deleted",
    # A project's quality-gate settings changed through the catalog door. Audited because it decides
    # whether a promotion waits for a human. Untargeted: it names no party, like `policy_set`.
    "gate_set",
    "gate_deleted",
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
    # `extra` carries {from_version, to_version}: the RANGE (D-R3) a consumer resolves by the change-feed
    # consumer rule (`catalog.services.changes`), holding no bookmark of its own.
    "table_published",
    # THE TAG GATE'S HOLD (ADR 0156 C1, [[LH-466]]): a publish the project's gate holds for a `can_promote`
    # holder, a rejection that restores main to the published version, and a hold nobody answered before its
    # deadline. An approval moves `published` and is announced as `table_published`. UNTARGETED: each changes a
    # table's publication, not a person's standing, and the watchers hear the tag move through lineage.
    "promotion_held",
    "promotion_rejected",
    "promotion_expired",
    # THE REF PLANE ([[LH-056]]), and UNTARGETED for the reason the rule states: a control event is
    # targeted when it changes what a specific person may do or must do, not when it changes an object.
    # Creating or deleting a branch, and creating, moving or deleting a tag, change an OBJECT -- nobody's
    # standing moves -- so they belong with the other object-lifecycle members and stay OUT of
    # notifications' `NAMED_ACTIONS`. `table_published` is the sibling that is NOT here, and the
    # difference is instructive: a publication stamps an `originator`, so it names somebody.
    "table_branch_created",
    "table_branch_deleted",
    "table_tag_created",
    "table_tag_updated",
    "table_tag_deleted",
    # An erasure's residual is pinned by a tag or a branch, and its release is scheduled (ADR 0138, [[LH-178]]).
    # TARGETED: one event per person it costs, the requester and each table owner in `extra.subject`, because the
    # release deletes the tags and recreates the branches at the deadline in `extra.due_at`, and a branch's own
    # commits are lost with it. Being told is the only window to copy work out; there is no door that defers it.
    "erasure_release_scheduled",
]

#: The kind of governed object the action targets — drives which console view invalidates. `project` is the
#: top of rask's hierarchy (project > warehouse > namespace > table) and became a first-class control object
#: when tenants got their own registry record (`open_hierarchy_lifecycle.md` Decision 1). Every member is a lakehouse
#: object: a tool that works on the lakehouse keeps its own objects and its own channel for them (ADR 0171).
ControlObjectType = Literal["project", "grant", "warehouse", "policy", "namespace", "table", "gate"]

#: The caps on the two cascade CLAIMS a publication echoes onto `table_published`'s `extra`: the batch
#: id and the person the batch is for. The catalog's publish door enforces them, and a door that takes
#: either from a caller declares the same cap, so a claim it accepts is never refused at that publish.
CASCADE_ID_MAX_LENGTH = 128
ORIGINATOR_MAX_LENGTH = 256


class CatalogControlEvent(BaseModel):
    """One control-plane mutation notice, published AFTER the mutation it announces succeeds.

    `actor` is the person the emitting service authenticated (`user:<principal>`), or a service principal or None when a
    service acts for itself. Several services publish on this topic (`control_signer_role` names the one that signs
    each action), so to a consumer the actor is a claim until the envelope's `rask_signature` proves who stamped it
    (`lineage_kit.signing.verify_control_signature`). The model ignores that member, so a signed and an unsigned
    envelope parse alike.
    """

    #: Client-side dedupe key (a redelivery carries the same id).
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    #: When the mutation happened (UTC).
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    action: ControlAction
    object_type: ControlObjectType
    #: The mutated object as OpenFGA holds it (e.g. `warehouse:acme`, `table:<uuid>`, `namespace:<uuid>`, or the
    #: `table:<uuid>` a grant/policy is scoped to). A table's or namespace's is its uuid, never its name ([[LH-524]]),
    #: so a consumer checking a person's access asks about this. What a consumer keys its invalidation off.
    object_id: str
    #: The table's or namespace's name when the event was emitted (`db1$t`), beside `object_id`'s uuid: what a person
    #: is shown, and what a consumer keyed by name (a namespace's warehouse binding) reads. `None` for any other object.
    object_name: str | None = None
    #: The uuid the catalog minted for the object when it is a table or a namespace (ADR 0173 §10), or for the table or
    #: namespace a grant or policy is scoped to: its identity across a rename, which a name is not.
    object_uuid: str | None = None
    #: The uuid of the namespace that table or namespace is in; `None` for a top-level namespace.
    namespace_uuid: str | None = None
    #: The verified principal that made the change, `user:` and its key (`user:dex~<user id>`, [[LH-063]]), or `None`
    #: on a service/unauthenticated mutation (auth-off dev).
    actor: str | None = None
    #: Action-specific detail — kept small (claim-check: pointers, never data). E.g. a grant's
    #: `{relation, subject}`, a rename's `{from, to}`, a warehouse bind's `{namespace}`. A `subject` is the OpenFGA
    #: user the change names (`fga.user_object`: the principal key percent-encoded, or a userset), which the
    #: notifications plane decodes back to the principal whose inbox it is.
    extra: dict[str, Any] = Field(default_factory=dict)


class ControlUuids(BaseModel):
    """A table's or namespace's own uuid and its namespace's: the pair a control event names its object by (ADR 0173 §10)."""

    model_config = ConfigDict(frozen=True)

    object_uuid: str | None = None
    namespace_uuid: str | None = None


#: A service whose own signing identity signs the control actions it emits ([[XC-078]]), and so a key of
#: `RASK_CONTROL_SIGNER_ROLES`, which the chart renders as each role's signing identities. Every action has one: the
#: lane carries only the lakehouse's own signed events (ADR 0171).
type ControlSigner = Literal["catalog", "maintenance"]


def control_signer_role(action: ControlAction) -> ControlSigner:
    """The role whose signature ``action`` needs before a door acts on it.

    The role is the service whose code emits the action, so a door verifies against that role's identities in
    `RASK_CONTROL_SIGNER_ROLES` and never against every signer the estate lists: a valid signature from another
    service is no authority over the action. Total under ty: an action added to `ControlAction` without a case
    here fails the `assert_never`.
    """
    match action:
        case (
            "grant_added"
            | "grant_revoked"
            | "project_created"
            | "project_deleted"
            | "warehouse_created"
            | "warehouse_activated"
            | "warehouse_deactivated"
            | "warehouse_bound"
            | "warehouse_unbound"
            | "warehouse_deleted"
            | "policy_set"
            | "policy_deleted"
            | "gate_set"
            | "gate_deleted"
            | "namespace_created"
            | "namespace_dropped"
            | "table_created"
            | "table_dropped"
            | "table_renamed"
            | "table_registered"
            | "table_deregistered"
            | "table_declared"
            | "table_protected"
            | "table_unprotected"
            | "namespace_protected"
            | "namespace_unprotected"
            | "table_undropped"
            | "namespace_undropped"
            | "table_published"
            | "promotion_held"
            | "promotion_rejected"
            | "promotion_expired"
            | "table_branch_created"
            | "table_branch_deleted"
            | "table_tag_created"
            | "table_tag_updated"
            | "table_tag_deleted"
            | "erasure_release_scheduled"
        ):
            return "catalog"
        case "table_purged" | "namespace_purged":
            # The expiry purge: maintenance's sweep emits both under `maintenance.controlEmit`.
            return "maintenance"
        case _:
            assert_never(action)
