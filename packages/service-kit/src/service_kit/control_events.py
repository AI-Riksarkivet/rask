"""The catalog control-plane change-event — a typed governance/metadata mutation notice.

Distinct from the OpenLineage **data** events (`catalog/core/lineage_emit.py`, which describe table
*writes*): this is the **control-plane** stream — a grant changed, a warehouse was deactivated, a policy
was set, a namespace/table was created/dropped/renamed. Those mutations already land in the durable audit
trail (#41, GreptimeDB); this is the real-time **subscribable** layer on top, so internal consumers (cache
invalidation, an in-estate reaction worker) and the admin console get a live feed (Polaris/Lakekeeper ship
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
#: wire values. Grouped by object: grants, warehouses, policies, namespaces, tables. `table_renamed` matters
#: most to the UI — it invalidates every open view of that table.
ControlAction = Literal[
    "grant_added",
    "grant_revoked",
    "warehouse_created",
    "warehouse_activated",
    "warehouse_deactivated",
    "warehouse_bound",
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
]

#: The kind of governed object the action targets — drives which console view invalidates.
ControlObjectType = Literal["grant", "warehouse", "policy", "namespace", "table"]


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
