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


class TupleCondition(BaseModel):
    """A CEL condition attached to a grant, with the parameters that ride the TUPLE.

    For the model's ``non_expired_grant`` that is ``grant_time`` + ``grant_duration`` — the window.
    ``current_time`` is deliberately NOT here: the clock belongs to the caller and is supplied per
    check, so a grant cannot pin the present moment at the instant it was written."""

    name: str
    context: dict[str, Any] = Field(default_factory=dict)


class AccessTuple(BaseModel):
    """One raw relationship tuple, verbatim (``user`` is a full subject — ``user:<id>`` or a userset like
    ``team:acme#member``). The estate-admin tuple API's unit: the read page lists these, write/delete take
    one as the body and echo it back, and a check verdict carries the exact tuple it probed."""

    user: str
    relation: str
    object: str
    condition: TupleCondition | None = None
    """Present when the grant is TIME-BOXED (or otherwise conditional). ``None`` is a permanent grant —
    and the distinction has to survive the read path, or an expiring grant renders as forever."""


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
    conditions: dict[str, dict[str, str]] = Field(default_factory=dict)
    """``{condition: {parameter: type}}`` — e.g. ``{"non_expired_grant": {"grant_time": "timestamp",
    "grant_duration": "duration", "current_time": "timestamp"}}``.

    Served so a client can render TYPED inputs for a conditional grant instead of a free-text box. The
    alternative is a hand-kept copy in the frontend, which drifts from the model the moment a parameter
    is added — and a missing operand is not a soft failure: the CEL expression errors, OpenFGA 400s, and
    this API's fail-closed handling turns that into a 503."""


class AccessCheckBody(AccessTuple):
    """A check, plus the runtime ``context`` any CONDITION on the path needs (``current_time`` for
    ``non_expired_grant``).

    Separate from :class:`AccessTuple` because context is a property of the QUESTION, not of the stored
    fact — the tuple carries the window, the caller carries the clock."""

    context: dict[str, Any] = Field(default_factory=dict)


class AccessCheckResult(BaseModel):
    """The estate-admin check verdict: ``checked`` echoes the exact resolved tuple that was probed (the
    ``user`` after bare-id → ``user:<id>`` resolution), so a verdict can never be misread against a
    different subject than the one OpenFGA saw."""

    allowed: bool
    checked: AccessTuple


class AccessListObjectsRequest(BaseModel):
    """ "What can this subject reach?" — every object of ``type`` on which ``user`` holds ``relation``.
    ``user`` may be a bare id (``alice`` → ``user:alice``) or a full subject/userset, as everywhere else."""

    user: str
    relation: str
    type: str


class AccessListObjectsResponse(BaseModel):
    """The objects the subject reaches, plus the RESOLVED query that produced them — echoed for the same
    reason ``AccessCheckResult.checked`` is: a result read against a different subject than OpenFGA saw
    is worse than no result."""

    objects: list[str]
    user: str
    relation: str
    type: str


class AccessListUsersRequest(BaseModel):
    """ "Who can do this?" — every subject holding ``relation`` on ``object``. The inverse of
    :class:`AccessListObjectsRequest`, and the blast-radius primitive before a revoke.

    ``user_type`` (+ ``user_relation``) picks WHICH subject kind to enumerate; OpenFGA takes exactly one
    filter per call, so ``role``/``assignee`` is a different question from ``user``, not a superset.
    """

    object: str
    relation: str
    user_type: str = "user"
    user_relation: str | None = None


class AccessListUsersResponse(BaseModel):
    """The effective subject set (ListUsers EXPANDS the model — role assignees, team members and the
    parent cascade are included, so this is effective access, not stored tuples). ``"*"`` is a public
    wildcard. ``truncated`` flags the server's silent ``listUsersMaxResults`` ceiling: an under-reported
    access review must never read as a complete one."""

    users: list[str]
    object: str
    relation: str
    user_type: str
    user_relation: str | None = None
    truncated: bool


class AccessExpandTupleToUserset(BaseModel):
    """A ``X from Y`` edge: ``tupleset`` is the relation walked (e.g. ``namespace:db1#parent``) and
    ``computed`` the relations read on whatever it points at."""

    tupleset: str | None = None
    computed: list[str] = Field(default_factory=list)


class AccessExpandLeaf(BaseModel):
    """A terminal of the userset tree — exactly one of the three is populated. ``users`` are literal
    subjects, ``computed`` a same-object rung (``owner`` implying ``writer``), ``tuple_to_userset`` the
    hop to a parent object."""

    users: list[str] | None = None
    computed: str | None = None
    tuple_to_userset: AccessExpandTupleToUserset | None = None
    expanded: list[AccessExpandNode] | None = None
    """The leaf's continuation, resolved: the rung this one implies, or the parent objects the
    ``X from Y`` hop lands on. ``None`` means "not followed", not "leads nowhere"."""
    continues: bool | None = None
    """This leaf goes further but the depth budget stopped here. The pair (``expanded`` absent,
    ``continues`` true) is what lets a client offer "expand further" instead of drawing a dead end
    that is not one — and it is decided before any round trip, so asking costs nothing."""


class AccessExpandNode(BaseModel):
    """One node of the OpenFGA userset tree, keeping the model's own vocabulary. Flattening ``union`` /
    ``intersection`` / ``difference`` away would discard precisely the operator that explains a
    surprising grant, so the shape is preserved verbatim. ``truncated`` marks a depth-capped branch."""

    name: str | None = None
    leaf: AccessExpandLeaf | None = None
    union: list[AccessExpandNode] | None = None
    intersection: list[AccessExpandNode] | None = None
    difference: AccessExpandDifference | None = None
    truncated: bool | None = None
    """The walk hit the depth ceiling here — the chain continues, this response just stops."""
    cycle: bool | None = None
    """This ``object#relation`` was already visited on this walk (``namespace`` self-nests, so a tuple
    misconfiguration can make one). Marked, never silently pruned — a loop IS the finding."""


class AccessExpandDifference(BaseModel):
    """``base but not subtract`` — the exclusion operator, the one shape a tuple table cannot show."""

    base: AccessExpandNode | None = None
    subtract: AccessExpandNode | None = None


class AccessSimulateRequest(BaseModel):
    """ "Would this grant do what I think?" — a Check evaluated AS IF ``hypothetical`` existed.

    Nothing is written. The model is concentric, so a grant three levels up cascades to every child;
    predicting that by reading the DSL is exactly the reasoning people get wrong, and today the only
    way to find out is to write the tuple and look.
    """

    user: str
    relation: str
    object: str
    hypothetical: list[AccessTuple] = Field(
        default_factory=list,
        max_length=10,
        description="Tuples to pretend exist. Capped — a Check is not a bulk what-if engine.",
    )


class AccessSimulateResult(BaseModel):
    """The DELTA, not the verdict.

    ``allowed`` alone cannot be acted on: a grant that changes nothing and a grant that unlocks access
    both answer true. ``baseline`` is the same Check WITHOUT the hypothesis, so the caller can say
    "this grant is what changes the answer" — or "this grant is a no-op", which is the more common and
    more useful finding.
    """

    allowed: bool
    baseline: bool
    checked: AccessTuple
    hypothetical: list[AccessTuple]


class AccessExpandRequest(BaseModel):
    """ "Why does this resolve?" — the userset tree for ``relation`` on ``object``.

    ``depth`` counts hops: 1 is a plain OpenFGA Expand (one level, the faithful primitive), higher
    follows ``computed`` rungs and ``X from Y`` edges through the cascade. Clamped server-side.
    """

    object: str
    relation: str
    depth: int = Field(default=1, ge=1, le=6)


class AccessExpandResponse(BaseModel):
    """The derivation of ``relation`` on ``object``: HOW the grant is composed, not whether it holds.

    ``tree`` is empty when the relation resolves to nothing on this object — an empty tree and an
    unavailable store are different answers, and the latter is a 503, never an empty tree.
    """

    tree: AccessExpandNode | None = None
    object: str
    relation: str
    depth: int
    """Hops actually requested (clamped server-side). 1 is a plain Expand; higher follows the cascade."""


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
    """The maintenance policy for one table, namespace or project — every field optional-with-defaults.

    ``retention_days`` / ``retain_versions`` override the sweep's global old-version cleanup (Lance
    keeps tag-pinned versions regardless); ``compact_enabled=False`` opts the target out of maintenance
    entirely; ``compact_interval_hours`` bounds how often the sweep maintains it.

    **Resolution is WINNER-TAKES-ALL, not field-by-field.** An exact table match shadows the namespace
    record which shadows the project record — the winning record supplies EVERY field, and a field the
    winner leaves unset falls to the sweep's global default rather than to the record it shadowed.
    Any surface that displays an effective policy has to say so: an inherited value rendered
    identically to a set one is how nobody can tell what is actually governing their data.
    """

    retention_days: int | None = Field(default=None, ge=1, le=3650)
    retain_versions: int | None = Field(default=None, ge=1, le=100_000)
    compact_enabled: bool = True
    compact_interval_hours: int | None = Field(default=None, ge=1, le=8760)
    # #76 compaction target-size tuning: the target rows per compacted fragment the sweep passes to
    # `compact_files` (Lance's `delta.targetFileSize` analog). None → Lance's default fragment sizing.
    #
    # Sizing is a real tradeoff, not a knob to leave alone: manifest work scales with fragment COUNT,
    # while conflict detection is PER-FRAGMENT — so a tier under many concurrent updates/deletes/
    # merge_inserts (the annotator's write pattern) wants MORE, smaller fragments, and a scan-heavy
    # tier wants fewer, larger ones. Per-tier is the point: bronze page images are ~1.8 MB rows,
    # silver/gold features are not, and one value cannot be right for both.
    target_rows_per_fragment: int | None = Field(default=None, ge=1024, le=10_000_000)
    # Per-OPERATION opt-outs. `compact_enabled` historically gated the whole pass; these split the
    # ordered per-dataset pass into its three real steps, because they have different costs and
    # different risks. The ORDER is fixed and not configurable — compaction obsoletes files, so
    # cleanup must follow it, and index optimization must follow that.
    cleanup_enabled: bool = True
    optimize_indices_enabled: bool = True
    # The sweep's READ batch, passed to `compact_files`. Lance's default is 8192 ROWS, and rows are
    # not a unit of memory: against bronze page-image rows (~1.8 MB each, measured) 8192 rows is
    # ~15 GB *per compute thread* — the default is safe for feature tables and ruinous for a blob
    # tier. Per-tier is the whole point, exactly as with `target_rows_per_fragment`. None → Lance's
    # default, which is correct wherever rows are small.
    scan_batch_size: int | None = Field(default=None, ge=1, le=8192)
    # #58 — WHO reclaims old versions. Lance ships its own auto-cleanup ON THE COMMIT PATH
    # (`lance.auto_cleanup.interval` / `.older_than`), so a tier that writes often may need no cron of
    # ours at all: the writer reclaims as it goes, and our sweep's cleanup step is redundant work
    # against the same versions. Set this to hand version reclamation to the DATASET.
    #
    # It is not free, and that is why it is a choice rather than a default: auto-cleanup runs inside
    # the commit, so it needs delete permission on the store and it adds latency to every Nth write.
    # A tier that writes rarely is better served by the sweep, which costs the writer nothing.
    #
    # Turning it on DISABLES the sweep's own cleanup for that target — one owner, never two. Both
    # running is not additive, it is two processes racing to delete the same manifests.
    auto_cleanup_interval_commits: int | None = Field(default=None, ge=1, le=1_000_000)

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


class PublishRequest(BaseModel):
    """Ask the catalog to gate a committed version and, if it passes, publish it.

    `version` is explicit rather than "whatever is latest": between a writer's commit and its publish
    call another writer may have committed, and publishing a version nobody gated is the failure the
    gate exists to prevent. The caller names what it wrote.
    """

    version: int = Field(ge=1)
    #: The identity column the `not_null` assertion checks. Stages key differently — bronze on `id`,
    #: a derived tier possibly on something else — so the caller names it rather than the gate guessing.
    key_column: str = Field(min_length=1, max_length=128)
    required_columns: list[str] = Field(default_factory=list)


class PublishResult(BaseModel):
    """The outcome, and the RANGE a notification should carry (§ D2 D-R3).

    `published=False` is a normal answer, not an error: the gate refused, the pointer did not move,
    and the previously published version keeps serving. `assertions` come back either way so a
    blocked batch is auditable without re-running anything.
    """

    table: str
    published: bool
    from_version: int | None
    to_version: int
    assertions: list[dict[str, object]] = Field(default_factory=list)
    reason: str | None = None


# --------------------------------------------------------------------------- #
# Deletion protection + recoverable drops (#73 / #75)
# --------------------------------------------------------------------------- #


class SetProtectionRequest(BaseModel):
    """The one field the protection doors write. Setting is idempotent; clearing removes the record.

    ONE definition for both the table and namespace doors: two identically-shaped copies in the two
    endpoint modules made FastAPI mangle the schema names into
    ``catalog__api__v1__endpoints__{tables,namespaces}__SetProtectionRequest`` — module paths leaking
    into the public contract and into every generated client.
    """

    protected: bool


class ProtectionResponse(BaseModel):
    id: str
    protected: bool


class TrashEntry(BaseModel):
    """One recoverable drop — what the owner needs to decide whether to undrop before the deadline."""

    id: str
    location: str
    dropped_by: str
    dropped_at: str
    expires_at: str
