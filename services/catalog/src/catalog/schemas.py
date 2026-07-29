"""Catalog wire schemas — the request/response + value Pydantic models the API exposes.

Gathered here (mirroring ``services/lineage/schemas.py``) instead of scattered inline across the endpoint
modules, so the wire contract has one home and the endpoints stay routing-only. Grouped by concern. Class
names are the OpenAPI schema names, so they are preserved verbatim on any move.

Domain value objects that are NOT wire schemas stay where they belong: ``VendedCredentials`` in
``core/vending`` (a credential-vending value type it owns) and ``InputPin`` in ``core/lineage_emit`` (a
lineage-emit input reference).
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from catalog.core.vending import VendedCredentials
from catalog.services import models as registry


# --------------------------------------------------------------------------- #
# Access review / simulation / grant / graph (#51 / #68 / #72 / #81)
# --------------------------------------------------------------------------- #


class RelationGrants(BaseModel):
    """One ``can_*`` action and every user subject holding it (``"*"`` = a public wildcard grant)."""

    relation: str
    users: list[str]


class AccessListResponse(BaseModel):
    object: str
    grants: list[RelationGrants]


class AccessCheckRequest(BaseModel):
    """A simulated authorization question — does ``user`` hold ``relation`` on this object? The
    ``user`` may be a bare subject (``alice``, taken as ``user:alice``) or a fully-qualified userset
    (``role:project_admin``, ``team:acme#member``)."""

    user: str
    relation: str


class AccessCheckResponse(BaseModel):
    object: str
    user: str
    relation: str
    allowed: bool


class AccessGrantRequest(BaseModel):
    """Grant or revoke ONE base rung to a subject. ``user`` may be a bare id (``alice`` → ``user:alice``)
    or a fully-qualified userset (``role:project_admin#assignee``, ``team:acme#member``); ``relation`` must
    be a grantable base rung the compiled model defines on the type (owner/writer/reader/validator) — never
    a derived ``can_*`` action nor the structural ``parent`` edge."""

    user: str
    relation: str


class AccessGrantResponse(BaseModel):
    object: str
    user: str
    relation: str
    granted: bool  # True after a grant, False after a revoke — the resulting state of the tuple


class AccessTuple(BaseModel):
    """One raw relationship tuple, verbatim (``user`` is a full subject — ``user:<id>`` or a userset like
    ``team:acme#member``). The estate-admin tuple API's unit: the read page lists these, write/delete take
    one as the body and echo it back, and a check verdict carries the exact tuple it probed."""

    user: str
    relation: str
    object: str


class AccessTuplesPage(BaseModel):
    """One page of the raw tuple listing. ``continuation`` is the OpenFGA Read token for the next page —
    ``null`` on the last page."""

    tuples: list[AccessTuple]
    continuation: str | None


class AccessModelResponse(BaseModel):
    """The authorization model: the checked-in ``model.fga`` DSL text and the model id the catalog's
    checks are pinned to."""

    dsl: str
    authorization_model_id: str


class AccessCheckResult(BaseModel):
    """The estate-admin check verdict: ``checked`` echoes the exact resolved tuple that was probed (the
    ``user`` after bare-id → ``user:<id>`` resolution), so a verdict can never be misread against a
    different subject than the one OpenFGA saw."""

    allowed: bool
    checked: AccessTuple


class GraphNode(BaseModel):
    """One node in the authorization graph — an FGA object or subject. ``type`` is the FGA type
    (user/role/team/table/namespace/warehouse/project), ``label`` the id without its ``type:`` prefix."""

    id: str
    type: str
    label: str


class GraphEdge(BaseModel):
    """A relation edge: ``source`` holds ``relation`` on ``target`` (a grant), or an object's ``parent``
    edge pointing at its container (``target`` is the parent object)."""

    source: str
    target: str
    relation: str


class AccessGraphResponse(BaseModel):
    object: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]


# --------------------------------------------------------------------------- #
# On-demand maintenance — GC + compaction (#75 / #76)
# --------------------------------------------------------------------------- #


class GcRequest(BaseModel):
    """The GC bounds — a version is reclaimable only if it clears BOTH given bounds (older than
    ``retention_days`` AND outside the most-recent ``retain_versions``). At least one is required."""

    retention_days: int | None = Field(default=None, ge=1, le=3650)
    retain_versions: int | None = Field(default=None, ge=1, le=10000)

    @model_validator(mode="after")
    def _one_bound(self) -> Self:
        if self.retention_days is None and self.retain_versions is None:
            raise ValueError("provide retention_days and/or retain_versions")
        return self


class GcPreview(BaseModel):
    current_version: int
    total_versions: int
    eligible_versions: list[int]
    protected_tags: dict[str, int]
    retention_days: int | None
    retain_versions: int | None


class GcRunResult(BaseModel):
    ok: bool
    old_versions_removed: int
    bytes_removed: int


class CompactRequest(BaseModel):
    """Optional #76 target-size override for a one-off compaction (None → Lance's default fragment sizing)."""

    target_rows_per_fragment: int | None = Field(default=None, ge=1024, le=10_000_000)


class CompactResult(BaseModel):
    ok: bool
    fragments_removed: int
    fragments_added: int


# --------------------------------------------------------------------------- #
# Maintenance policy (#50 / #76)
# --------------------------------------------------------------------------- #


class PolicyRequest(BaseModel):
    """The maintenance policy for one table or namespace — every field optional-with-defaults.

    ``retention_days`` / ``retain_versions`` override the sweep's global old-version cleanup (Lance
    keeps tag-pinned versions regardless); ``compact_enabled=False`` opts the target out of maintenance
    entirely; ``compact_interval_hours`` bounds how often the sweep maintains it.
    """

    retention_days: int | None = Field(default=None, ge=1, le=3650)
    retain_versions: int | None = Field(default=None, ge=1, le=100_000)
    compact_enabled: bool = True
    compact_interval_hours: int | None = Field(default=None, ge=1, le=8760)
    # #76 compaction target-size tuning: the target rows per compacted fragment the sweep passes to
    # `compact_files` (Lance's `delta.targetFileSize` analog). None → Lance's default fragment sizing.
    target_rows_per_fragment: int | None = Field(default=None, ge=1024, le=10_000_000)

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        # Gate on what was PROVIDED, not on value-equality with defaults: an explicit
        # ``{"compact_enabled": true}`` is meaningful (a table-level re-enable under a disabled
        # namespace policy — the exact-table match shadows the namespace record), so only a body
        # that sets nothing at all is refused.
        if not self.model_fields_set:
            raise ValueError("an empty policy changes nothing — set a field or delete the policy")
        return self


class PolicyResponse(BaseModel):
    # The policy fields must mirror PolicyRequest — model_validate ignores extras, so a request field
    # missing here would be silently dropped from every response.
    kind: str
    id: str
    path: str
    # #84 project-level records match by bucket (their warehouse buckets, resolved at set time), not by a
    # single path (which is "" for them). None for table/namespace records.
    buckets: list[str] | None = None
    retention_days: int | None = None
    retain_versions: int | None = None
    compact_enabled: bool = True
    compact_interval_hours: int | None = None
    target_rows_per_fragment: int | None = None


class PolicyDeleteResponse(BaseModel):
    status: str
    kind: str
    id: str


# --------------------------------------------------------------------------- #
# Model registry — candidate→blessed promotion (#17)
# --------------------------------------------------------------------------- #


class PromoteRequest(BaseModel):
    """Bless a candidate model version (candidate→blessed). ``version`` is the model/Lance version to bless;
    ``min_metrics`` is the fail-closed gate — each named metric must be present AND >= its threshold."""

    version: int = Field(ge=1)
    min_metrics: dict[str, float] = Field(default_factory=dict)
    tag: str = Field(default=registry.BLESSED_TAG, min_length=1, max_length=64)


class PromoteResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    model: str
    blessed_version: int
    tag: str


class ModelArtifact(BaseModel):
    """One plain-path artifact object under the model's tree (the #17/#92 layout:
    ``models/<model>/<token>/…``). ``path`` is relative to the model's artifact root
    (``<token>/weights.json``); ``updated_at`` is the object's mtime as ISO-8601, ``null`` when the
    filesystem reports none."""

    path: str
    size_bytes: int
    updated_at: str | None


class ModelDescribeResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    model: str
    latest_version: int
    blessed_version: int | None
    candidate_metrics: dict[str, Any] | None
    blessed_metrics: dict[str, Any] | None
    # The registry rows point at these plain-path objects; [] when nothing is laid out (registry-only).
    artifacts: list[ModelArtifact] = Field(default_factory=list)


class ModelSummary(BaseModel):
    """One registry entry in the list view. Versions are ``None`` only for a registry directory that is
    not (yet) a readable Lance dataset — surfaced, not hidden, so an interrupted first publish is visible."""

    model_config = ConfigDict(protected_namespaces=())
    model: str
    latest_version: int | None
    blessed_version: int | None


class ModelsListResponse(BaseModel):
    models: list[ModelSummary]


# --------------------------------------------------------------------------- #
# Warehouse provisioning (catalog-parity control plane)
# --------------------------------------------------------------------------- #


class CreateWarehouseRequest(BaseModel):
    id: str
    project: str
    bucket: str | None = None  # defaults to the id (a warehouse = one bucket)
    # Serving designation (DECISIONS "Medallion tiers — hybrid physical layout"): "gold" marks this as the
    # project's gold SERVING warehouse — the silver→gold mover's tenant target root when the chart's
    # medallion.goldWarehouse is on. Absent (default) = a WORK warehouse. Only "gold" is accepted for now.
    serving: str | None = None


class WarehouseResponse(BaseModel):
    id: str
    bucket: str
    root_uri: str
    project: str
    status: str | None = None  # "active" / "deactivated" (P2.3 lifecycle); absent on pre-lifecycle records
    serving: str | None = None  # "gold" = the project's serving warehouse; absent = a work warehouse
    created_at: str | None = None


class CreateWarehouseNamespaceRequest(BaseModel):
    namespace: str  # a single TOP-LEVEL namespace name to create in + bind to this warehouse


# --------------------------------------------------------------------------- #
# Credential vending (Track B)
# --------------------------------------------------------------------------- #


class CredentialResponse(BaseModel):
    """The vending result. ``mode="direct"`` carries scoped ``credentials``; ``mode="server_mediated"``
    means no credential was issued (Mode B / unknown bucket) — the client uses the data endpoints.

    ``location`` + ``read_version`` give a client-direct writer its write TARGET and the optimistic-commit
    BASE version in one round-trip (#2): the client writes fragments to ``location`` then commits them via
    ``POST /{id}/commit`` at ``read_version`` — no second ``describe`` needed."""

    mode: str
    credentials: VendedCredentials | None = None
    location: str | None = None
    read_version: int | None = None


# --------------------------------------------------------------------------- #
# Client-direct fragment commit (#2)
# --------------------------------------------------------------------------- #


class CommitFragmentsRequest(BaseModel):
    """A client-direct APPEND commit (#2): the serialized Lance ``FragmentMetadata`` the client wrote
    DIRECTLY to object storage (``[fragment.to_json() for fragment in write_fragments(...)]``), plus the
    ``read_version`` those fragments were built against (optimistic concurrency)."""

    fragments: list[dict[str, Any]]
    read_version: int


class CommitFragmentsResponse(BaseModel):
    version: int
    row_count: int
