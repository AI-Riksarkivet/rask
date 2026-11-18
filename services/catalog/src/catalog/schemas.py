"""Catalog wire schemas — the request/response + value Pydantic models the API exposes.

Gathered here (mirroring ``services/lineage/schemas.py``) instead of scattered inline across the endpoint
modules, so the wire contract has one home and the endpoints stay routing-only. Grouped by concern. Class
names are the OpenAPI schema names, so they are preserved verbatim on any move.

Domain value objects that are NOT wire schemas stay where they belong: ``VendedSession`` in ``core/vending``
(a vend as the catalog holds it, its secret and session token typed secret, [[LH-072]]) and ``InputPin`` in
``core/lineage_emit`` (a lineage-emit input reference).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from catalog.services.changes import ChangeKind
from catalog.services.control_records import DeleteMode
from catalog.services.object_labels import LabelDefinition, LabelKind, LabelScope
from lakehouse.control_events import CatalogControlEvent
from lakehouse.features import TableFileFormat
from lakehouse.maintenance_due import CompactionDue, IndexDue


class RequestBody(BaseModel):
    """The body of a request to one of rask's own doors, which refuses a field it does not define.

    A field the door does not define is refused 400 (code 13) naming it, never ignored: a misspelled bound
    (``retain_version``) read as absent would apply a default the caller never chose, and the request would answer as if
    it had been understood. The spec's request models are the installed ``lance_namespace``'s, not these.
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Access review / simulation / grant / graph (#51 / #68 / #72 / #81)
# --------------------------------------------------------------------------- #


class RelationGrants(BaseModel):
    """One ``can_*`` action and every user subject holding it (``"*"`` = a public wildcard grant)."""

    relation: str
    users: list[str]
    #: True when OpenFGA's ``listUsersMaxResults`` cap was hit, so ``users`` is a PREFIX of the holders,
    #: not the set. Per-relation because the cap applies per call: on a wide object one relation can be
    #: complete while its neighbour is cut off, and a response-level flag would smear that distinction.
    #: An access review that silently returns 1000 of 1500 holders is worse than one that refuses —
    #: the reviewer concludes the other 500 have no grant. The admin door
    #: (``AccessListUsersResponse.truncated``) has carried this since it shipped; this is the same flag
    #: on the PER-OBJECT review, which is the surface a human actually reads (diff2 F10 item 8).
    truncated: bool = False


class AccessListResponse(BaseModel):
    object: str
    #: The table's or namespace's name beside ``object``, which names its uuid ([[LH-524]]); ``None`` for any other object.
    name: str | None = None
    grants: list[RelationGrants]
    #: True when ANY relation above was truncated — a roll-up so a caller can spot an incomplete review
    #: without walking the list. The per-relation flags say WHICH.
    truncated: bool = False


class ManagedAccessRequest(RequestBody):
    """Turn managed access on or off for one container."""

    enabled: bool


class ManagedAccessResponse(BaseModel):
    """The container and the state of its flag after the write.

    Echoes the resolved state rather than the request, so a caller learns the outcome even when the
    write was a no-op (the flag was already in the asked-for state).
    """

    object: str
    #: The table's or namespace's name beside ``object``, which names its uuid ([[LH-524]]); ``None`` for any other object.
    name: str | None = None
    managed_access: bool


class MyPermissionsResponse(BaseModel):
    """What the CALLING subject may do on one object — every ``can_*`` the model defines, answered
    for them alone.

    Deliberately not a projection of :class:`AccessListResponse`. That one enumerates *who holds
    what*, which discloses principals and therefore clears the owner bar; this one answers "what may
    **I** do here", which discloses nothing about anyone else and must stay reachable by the reader
    it is describing. Same relation set, a `check` per relation instead of a `list_users`, and no
    ``user`` parameter — a self-view that accepts a subject IS the enumeration question wearing a
    different name.
    """

    object: str
    #: The table's or namespace's name beside ``object``, which names its uuid ([[LH-524]]); ``None`` for any other object.
    name: str | None = None
    subject: str
    permissions: dict[str, bool]


class AccessCheckRequest(RequestBody):
    """A simulated authorization question — does ``user`` hold ``relation`` on this object? The
    ``user`` may be a bare subject (``alice``, taken as ``user:alice``) or a fully-qualified userset
    (``role:<id>#assignee``)."""

    user: str
    relation: str


class AccessCheckResponse(BaseModel):
    object: str
    #: The table's or namespace's name beside ``object``, which names its uuid ([[LH-524]]); ``None`` for any other object.
    name: str | None = None
    user: str
    relation: str
    allowed: bool


class AccessGrantRequest(RequestBody):
    """Grant or revoke ONE base rung to a subject. ``user`` may be a bare id (``alice`` → ``user:alice``)
    or a fully-qualified userset (``role:<id>#assignee``); ``relation`` must
    be a grantable base rung the compiled model defines on the type (owner/writer/reader/maintainer) — never
    a derived ``can_*`` action nor the structural ``parent`` edge."""

    user: str
    relation: str


class AccessGrantResponse(BaseModel):
    object: str
    #: The table's or namespace's name beside ``object``, which names its uuid ([[LH-524]]); ``None`` for any other object.
    name: str | None = None
    user: str
    relation: str
    granted: bool  # True after a grant, False after a revoke — the resulting state of the tuple


class TupleCondition(RequestBody):
    """A CEL condition attached to a grant, with the parameters that ride the TUPLE.

    For the model's ``non_expired_grant`` that is ``grant_time`` + ``grant_duration`` — the window.
    ``current_time`` is deliberately NOT here: the clock belongs to the caller and is supplied per
    check, so a grant cannot pin the present moment at the instant it was written."""

    name: str
    context: dict[str, Any] = Field(default_factory=dict)


class AccessTuple(RequestBody):
    """One raw relationship tuple, verbatim (``user`` is a full subject — ``user:<id>`` or a userset like
    ``role:<id>#assignee``). The estate-admin tuple API's unit: the read page lists these, write/delete take
    one as the body and echo it back, and a check verdict carries the exact tuple it probed."""

    user: str
    relation: str
    object: str
    condition: TupleCondition | None = None
    """Present when the grant is TIME-BOXED (or otherwise conditional). ``None`` is a permanent grant —
    and the distinction has to survive the read path, or an expiring grant renders as forever."""


class NamedAccessTuple(AccessTuple):
    """A tuple as the estate admin's doors answer it: ``name`` is the name its table or namespace object holds now, or the
    id its project's or warehouse's creation is held under, beside ``object``, which names its uuid ([[LH-591]], ADR
    0245); ``user_name`` the same for a user naming a project's or a warehouse's creation; ``None`` for any other and for
    one no name holds."""

    name: str | None = None
    user_name: str | None = None


class AccessTuplesPage(BaseModel):
    """One page of the raw tuple listing. ``continuation`` is the OpenFGA Read token for the next page —
    ``null`` on the last page."""

    tuples: list[NamedAccessTuple]
    continuation: str | None


class PrincipalRecord(BaseModel):
    """A principal the estate admin offboarded ([[LH-233]]): who it was, who removed it and when."""

    principal: str
    #: ``service`` for a principal the cluster's service-account issuer vouched for, else ``person``.
    kind: Literal["person", "service"]
    deleted_at: datetime
    #: The estate admin's principal that ran the door.
    deleted_by: str


class PrincipalDeleted(BaseModel):
    """What one run of the offboarding door did: the record, and how many tuples naming the principal it revoked."""

    record: PrincipalRecord
    revoked: int


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
    checked: NamedAccessTuple


class AccessListObjectsRequest(RequestBody):
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
    #: True when OpenFGA stopped at its ListObjects ceiling, so ``objects`` is a part of what the subject reaches, not
    #: all of it: a reach review that reads a cut answer as whole concludes the rest is out of reach.
    truncated: bool = False
    #: The name each table or namespace object in ``objects`` holds now ([[LH-591]]); an object no name holds is absent.
    names: dict[str, str] = Field(default_factory=dict)


class AccessListUsersRequest(RequestBody):
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
    """The effective subject set (ListUsers EXPANDS the model — role assignees and the
    parent cascade are included, so this is effective access, not stored tuples). ``"*"`` is a public
    wildcard. ``truncated`` flags the server's silent ``listUsersMaxResults`` ceiling: an under-reported
    access review must never read as a complete one."""

    users: list[str]
    object: str
    #: The name ``object`` holds now, when it is a table or a namespace, or its id, when it is a project's or a
    #: warehouse's creation ([[LH-591]], ADR 0245).
    name: str | None = None
    relation: str
    user_type: str
    user_relation: str | None = None
    truncated: bool
    #: The id each user naming a project's or a warehouse's creation is held under (ADR 0245); others are absent.
    names: dict[str, str] = Field(default_factory=dict)


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


class AccessSimulateRequest(RequestBody):
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
    checked: NamedAccessTuple
    hypothetical: list[AccessTuple]


class AccessExpandRequest(RequestBody):
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
    #: The name ``object`` holds now, when it is a table or a namespace ([[LH-591]]).
    name: str | None = None
    relation: str
    depth: int
    """Hops actually requested (clamped server-side). 1 is a plain Expand; higher follows the cascade."""


class GraphNode(BaseModel):
    """One node in the authorization graph — an FGA object or subject. ``type`` is the FGA type
    (user/role/table/namespace/warehouse/project), ``label`` the id without its ``type:`` prefix."""

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
    #: The table's or namespace's name beside ``object``, which names its uuid ([[LH-524]]); ``None`` for any other object.
    name: str | None = None
    nodes: list[GraphNode]
    edges: list[GraphEdge]


# --------------------------------------------------------------------------- #
# On-demand maintenance — GC + compaction (#75 / #76)
# --------------------------------------------------------------------------- #


class GcRequest(RequestBody):
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


class CondemnRequest(RequestBody):
    """One reclaim batch a reclaimer asks of the catalog (ADR 0246): the versions past their retention it would delete,
    at most a batch of them; none to finish only what a dead batch or ``version/delete`` left condemned."""

    versions: list[int] = Field(default_factory=list, max_length=16)


class CondemnedBatch(BaseModel):
    """The catalog's answer to one reclaim batch: what the reclaimer may delete now."""

    #: What keeps the table's whole history, when a hold at a level that can govern it does: nothing may be deleted.
    held_by: str | None = None
    #: The versions the reclaimer may delete, each condemned and past its batch's pin and policy reads.
    versions: list[int] = Field(default_factory=list)
    #: The ref's versions left condemned that this batch does not license: a pin keeps them, or a hold.
    standing: list[int] = Field(default_factory=list)


class CondemnedVersion(BaseModel):
    """A version a reclaim may be deleting: what a policy's answer does not promise (ADR 0246)."""

    #: The table's name, as it is named now.
    table: str
    #: The branch the version is of, ``None`` for main.
    branch: str | None = None
    version: int


class CleanupDisarmed(BaseModel):
    """What the disarm of a ref's commit-path cleanup did ([[LH-575]])."""

    #: The ref's version after it: the config-only commit it made, or the version it found when no key was set.
    version: int
    #: The ``lance.auto_cleanup.*`` keys it removed; empty when none was set.
    disarmed: list[str] = Field(default_factory=list)


class FloorRaiseRequest(RequestBody):
    """A raise of a table's listing floor, or its dry run. A field it does not name is refused, since a misnamed
    ``dry_run`` would otherwise commit the raise its caller only meant to preview."""

    #: Answer as the raise would be answered, the caller's rung and the table's name judged, and commit nothing.
    dry_run: bool = False


class FloorRaised(BaseModel):
    """What a raise of a table's listing floor committed ([[LH-094]], [[LH-575]]); nothing on a dry run."""

    #: The version the raise committed.
    version: int | None = None
    #: The epoch the ``rask.maintenance.floor_raised_at`` stamp it committed names.
    raised_at: float | None = None


class DueTable(BaseModel):
    """One table a compaction or an index optimize is due on, as its maintenance pass measured it (ADR 0162)."""

    #: The table's name now, or the name the record kept when the table is gone.
    table: str
    #: The table's uuid (ADR 0173 §11), which the record is kept under.
    uuid: str
    version: int
    #: What Lance's planner would rewrite under the winning policy; ``None`` when no compaction is due.
    compaction: CompactionDue | None = None
    indices: list[IndexDue] = Field(default_factory=list)
    #: The policy record that governed the evaluation, ``None`` when none matched.
    policy: str | None = None
    evaluated_at: datetime


class MaintenanceDueResponse(BaseModel):
    """A page of :class:`DueTable`, in table order; ``page_token`` continues it."""

    tables: list[DueTable]
    page_token: str | None = None


class ErasureRequest(RequestBody):
    """What to erase, and how much history the caller means to take with it ([[LH-073]], [[LH-493]])."""

    #: The rows to remove, as a Lance filter expression — the same dialect `delete_from_table` takes.
    predicate: str
    #: How much version history to KEEP, in days, held on the obligation and applied by every verification of it.
    #: DEFAULTS TO NONE, which is the opposite of every other retention knob in the estate and is the point: an
    #: erasure that leaves the subject readable at an old version has not erased them, so a caller preserving
    #: unrelated history opts INTO it rather than having to remember to opt out.
    retain_days: Annotated[int, Field(ge=0, le=36500)] = 0
    #: The erasure obligation, on a table this one was derived from, that this one follows ([[LH-332]]): that obligation is
    #: held open until every table derived from its table holds one following it, directly or through a chain. It must
    #: name an obligation the catalog recorded.
    follows: str | None = None


class ErasureVerifyRequest(RequestBody):
    """The predicate an erasure obligation was recorded with, which the catalog holds only as a digest ([[LH-493]])."""

    predicate: str


# --------------------------------------------------------------------------- #
# Maintenance policy (#50 / #76)
# --------------------------------------------------------------------------- #


class PolicyRequest(RequestBody):
    """The maintenance policy for one table, namespace or project — every field optional-with-defaults.

    ``retention_days`` / ``retain_versions`` override the sweep's global old-version cleanup (Lance
    keeps tag-pinned versions regardless); ``compact_enabled=False`` opts the target out of maintenance
    entirely; ``compact_interval_hours`` bounds how often the sweep maintains it.

    **Resolution is WINNER-TAKES-ALL, not field-by-field.** An exact table match shadows the namespace
    record which shadows the project record — the winning record supplies EVERY field, and a field the
    winner leaves unset falls to the sweep's global default rather than to the record it shadowed.
    Any surface that displays an effective policy has to say so: an inherited value rendered
    identically to a set one is how nobody can tell what is actually governing their data.

    **A HOLD BINDS AT EVERY LEVEL (ADR 0243 §7).** ``cleanup_enabled`` false on the table's record, on any namespace above
    it or on its project keeps the table's whole history, whatever a nearer record says: a narrower level cannot lift a
    hold. A write of a namespace's or a project's policy answers 409 (ConcurrentModification, retryable) while a reclaim of
    a table it governs runs, and a table's while a reclaim of the table runs.
    """

    retention_days: int | None = Field(default=None, ge=1, le=3650)
    retain_versions: int | None = Field(default=None, ge=1, le=100_000)
    compact_enabled: bool = True
    compact_interval_hours: int | None = Field(default=None, ge=1, le=8760)
    # #76 compaction target-size tuning: the target rows per compacted fragment the sweep passes to
    # `compact_files` (Lance's `delta.targetFileSize` analog). None → Lance's default fragment sizing.
    #
    # Sizing is a real tradeoff, not a knob to leave alone: manifest work scales with fragment COUNT,
    # while conflict detection is PER-FRAGMENT — so a table under many concurrent updates/deletes/
    # merge_inserts wants MORE, smaller fragments, and a scan-heavy
    # table wants fewer, larger ones. A policy per table or namespace is the point: page-image rows are ~1.8 MB,
    # feature rows are not, and one value cannot be right for both.
    target_rows_per_fragment: int | None = Field(default=None, ge=1024, le=10_000_000)
    # Per-OPERATION opt-outs. `compact_enabled` historically gated the whole pass; these split the
    # ordered per-dataset pass into its three real steps, because they have different costs and
    # different risks. The ORDER is fixed and not configurable — compact, then optimize indices
    # (compaction leaves its new fragments unindexed), then cleanup LAST, because it reclaims the
    # superseded versions both earlier steps produced, in one pass.
    cleanup_enabled: bool = Field(
        default=True,
        description="False keeps the whole version history (a legal hold): at a namespace or the project it holds every table beneath it, whatever their own policies say; no narrower level lifts it.",
    )
    optimize_indices_enabled: bool = True
    # The sweep's READ batch, passed to `compact_files`. Lance's default is 8192 ROWS, and rows are
    # not a unit of memory: against page-image rows (~1.8 MB each, measured) 8192 rows is
    # ~15 GB *per compute thread* — the default is safe for feature tables and ruinous for a blob
    # table. A policy per table or namespace is the whole point, exactly as with `target_rows_per_fragment`. None → Lance's
    # default, which is correct wherever rows are small.
    scan_batch_size: int | None = Field(default=None, ge=1, le=8192)
    # The same ceiling expressed in BYTES rather than rows — pylance 11's `max_source_bytes`, a bound
    # on the compaction PASS instead of on a read chunk. Per policy for the reason above: a table whose
    # rows turn out larger than whoever set the row count assumed is precisely how the read bound gets
    # away from you, and this one does not depend on knowing the row size. None → the sweep's default.
    # The range is `MaintenanceSettings.max_source_bytes`'s, which states the ceiling's reason.
    max_source_bytes: int | None = Field(default=None, ge=1024 * 1024, le=1024 * 1024 * 1024)
    # HOW this table's bytes move. Per policy for the same reason the two bounds above are: a table of
    # uniformly-encoded fragments can binary-copy where a table that has evolved its schema cannot, and
    # `try_binary_copy` falls back rather than failing. None = Lance's re-encode, today's behaviour.
    repack_mode: Literal["reencode", "try_binary_copy", "force_binary_copy"] | None = None
    # NO FIELD hands version reclamation to Lance's commit-path auto-cleanup (`lance.auto_cleanup.*`).
    # That lane deletes inside whichever writer commits, past a legal hold and a protected base and with
    # no record, so the sweep is the one reclaimer and disarms those keys on every tick ([[LH-245]]).
    # "Lance's default" for a field left unset holds because no writer sets Lance's own compaction config
    # (`lance.compaction.*`), which a plan naming no option reads: `version/create`, every property door, the register
    # door and the restore door refuse those keys ([[LH-538]]), so this record alone plans a table's compaction.
    # #60 — the columns this target's queries DEPEND ON having an index for. Reporting only: the sweep
    # never builds or drops an index, it says when one it expected is absent.
    #
    # Opt-in by necessity rather than by taste. Without a declared expectation there is no way to tell
    # "this column was never indexed" from "this column lost its index", and reporting every unindexed
    # column in a wide table is noise nobody reads — so `inspect_indices` asks for the columns a policy
    # names and checks the other three #60 states unconditionally.
    #
    # It had no way to be named until 2026-08-16. `compact_one(index_columns=…)` existed, no caller ever
    # supplied it, and no policy field carried it, so the dropped-index check was unreachable by
    # construction while its docstring promised "a policy that names the columns it depends on gets a
    # real answer". A dropped index makes `optimize_indices()` a successful no-op — the pass stays green
    # while every query on that column falls back to a full scan — which is exactly the silent-degradation
    # class this whole report exists to surface.
    index_columns: list[str] | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        # Gate on what was PROVIDED, not on value-equality with defaults: an explicit
        # ``{"compact_enabled": true}`` is meaningful (a table-level re-enable under a disabled
        # namespace policy — the exact-table match shadows the namespace record), so only a body
        # that sets nothing at all is refused.
        if not self.model_fields_set:
            raise ValueError("an empty policy changes nothing — set a field or delete the policy")
        return self


#: The three rungs a maintenance policy is kept on.
type PolicyKind = Literal["table", "namespace", "project"]


class PolicyResponse(BaseModel):
    # The policy fields must mirror PolicyRequest — model_validate ignores extras, so a request field
    # missing here would be silently dropped from every response.
    kind: PolicyKind
    #: The table's or namespace's name as the door was addressed (as the record kept it, in a listing), or the project id.
    id: str
    #: The uuid of the table or namespace the record is kept for (ADR 0173 §8); None for a project record.
    uuid: str | None = None
    # #84 project-level records match by bucket (their warehouse buckets, resolved at set time). None for
    # table/namespace records.
    buckets: list[str] | None = None
    retention_days: int | None = None
    retain_versions: int | None = None
    compact_enabled: bool = True
    compact_interval_hours: int | None = None
    target_rows_per_fragment: int | None = None
    # These four were MISSING while PolicyRequest accepted them, and the omission was destructive
    # rather than cosmetic: `put_policy` overwrites the whole record, so a client that read a policy,
    # edited one field and sent it back wrote NULL over every field the response never mentioned —
    # silently removing the compaction memory bound and turning the step flags back on.
    cleanup_enabled: bool = True
    optimize_indices_enabled: bool = True
    scan_batch_size: int | None = None
    max_source_bytes: int | None = None
    repack_mode: str | None = None
    #: A record is kept for the object; a describe of an object that holds none answers :class:`PolicyUnset`. Required,
    #: with no default, so the schema marks it on both answers and a typed client narrows the union on it.
    set: Literal[True]
    #: On a set's answer: the versions of the tables the policy governs that were already condemned when it landed, read
    #: after its write (ADR 0246). A reclaim may be deleting them; every other version is kept as the policy says. Absent
    #: on a describe or a listing.
    condemned: list[CondemnedVersion] | None = None


class PolicyUnset(BaseModel):
    """What a policy describe answers for an object that holds no maintenance policy: the object, and ``set`` false.

    The object stands: its door refuses a missing or hidden one before the policy is read, by resolving a table's or
    namespace's name and by reading a project's registry record. Lance's error codes name only missing objects (code 4
    says the table does not exist), so an absent setting is an answer, not an error.
    """

    kind: PolicyKind
    #: The table's or namespace's name as the door was addressed, or the project id.
    id: str
    #: The uuid of the table or namespace (ADR 0173 §8); None for a project.
    uuid: str | None = None
    set: Literal[False]


class PolicyDeleteResponse(BaseModel):
    status: str
    kind: str
    id: str
    #: The versions of the tables the deleted policy governed that were already condemned when it went, read after the
    #: delete (ADR 0246): a reclaim may be deleting them. A hold that governs the tables keeps every version it does not name.
    condemned: list[CondemnedVersion] = Field(default_factory=list)


class ProjectPoliciesResponse(BaseModel):
    """Every maintenance policy record governing data inside ONE project (#65) — the tenant-scoped VIEW.

    ``set``/``describe``/``delete`` answer for a single object each; nothing could ever enumerate what is
    actually in force across a tenant, so a project admin could not tell which of their namespaces or
    tables carries a retention override without already knowing its id. This is that read.

    ``buckets`` and ``warehouses`` are the SCOPE the listing was computed from — the project's warehouses and
    their buckets — reported so a surprising list can be explained without a second call. A table or namespace
    record is in scope when its name begins with one of those warehouses.

    **Resolution is winner-takes-all** (``PolicyRequest``): a table record shadows a namespace record
    shadows the project record, and the winner supplies EVERY field. These records are what EXISTS, not
    what governs any one dataset — a surface that renders them as an effective, merged policy is lying.
    """

    project: str
    buckets: list[str]
    warehouses: list[str]
    policies: list[PolicyResponse]


# --------------------------------------------------------------------------- #
# Warehouse provisioning (catalog-parity control plane)
# --------------------------------------------------------------------------- #


class DeleteProfile(RequestBody):
    """What a drop under a warehouse does (ADR 0146): ``hard`` destroys the table at once; ``soft`` keeps it
    recoverable for ``grace_days``, after which its grants are revoked and its bytes wait for the purge; ``estate``
    clears the warehouse's own profile, so it takes the estate's ``LANCE_TRASH_GRACE_DAYS``."""

    mode: DeleteMode
    #: Days a soft drop stays recoverable, a fraction allowed; set for ``soft`` and only for it.
    grace_days: float | None = Field(default=None, gt=0, le=365)

    @model_validator(mode="after")
    def _grace_matches_mode(self) -> Self:
        if (self.mode is DeleteMode.SOFT) != (self.grace_days is not None):
            raise ValueError("a soft delete profile names its grace_days, and a hard one names none")
        return self


class CreateWarehouseRequest(RequestBody):
    id: str
    project: str
    bucket: str | None = None  # defaults to the id (a warehouse = one bucket)
    #: Deletion protection as the declaration states it ([[LH-548]]): `true` arms it and `false` clears it, so a
    #: re-POST converges on what it declares either way. Absent leaves it as it stands, so a declaration that never
    #: names it leaves it to `POST /management/v1/warehouses/{warehouse_id}/protection`; a new warehouse is then
    #: unprotected, its record carrying no `protected` key.
    protected: bool | None = None
    # THE PROJECT'S PRIMARY WAREHOUSE, a mark the operator sets for whoever picks one of a project's several active
    # warehouses. Recorded and read back, never routed by: a name's first segment names its warehouse ([[LH-593]]).
    # Absent (default) keeps every existing record byte-identical; a re-POST carries an existing flag forward.
    primary: bool = False
    # [[LH-067]] THE OBJECT STORE THIS WAREHOUSE'S BUCKET LIVES AT. Absent (default) means the estate's
    # and keeps existing records byte-identical; an explicit empty string re-points a record at the
    # estate's. Any other store is refused 400 ([[LH-205]]): the catalog signs every connection with the
    # estate's own key pair and consumes no per-warehouse credential, so admitting one would sign that
    # key toward a host the caller chose. A respelling of the estate's endpoint is the estate's.
    #
    # NON-SECRET, and there is deliberately no credential beside it. Material never travels in a
    # record; the estate signs with its ServiceAccount token and holds no S3 key (ADR 0170), and a
    # second store's key belongs in the secret store with the record naming a reference.
    endpoint: str | None = None
    #: What a drop under this warehouse does. Absent means unchanged on a re-POST, and the estate's
    #: `LANCE_TRASH_GRACE_DAYS` on a warehouse that never named one; `{"mode": "estate"}` returns a
    #: warehouse to the estate's, so a declaration that drops its profile converges by naming that.
    delete_profile: DeleteProfile | None = None


class WarehouseResponse(BaseModel):
    id: str
    bucket: str
    root_uri: str
    project: str
    status: str | None = None  # "active" / "deactivated" (P2.3 lifecycle); absent on pre-lifecycle records
    # #123: the delete-door safety, finally OBSERVABLE — records carry "true" (string), coerced here.
    # None on unprotected/pre-protection records (exclude_none keeps them byte-identical on the wire).
    protected: bool | None = None
    #: Who armed the protection (`user:<principal>`), so a refused delete can be traced to its armer ([[LH-548]]); None
    #: while unprotected.
    protected_by: str | None = None
    # Which warehouse the project marked primary, made OBSERVABLE: an operator has to be able to read back which
    # record they marked. None on unmarked records (exclude_none keeps them byte-identical on the wire).
    primary: bool | None = None
    # [[LH-067]] Which object store this warehouse is reached at. Readable back for the same reason
    # `primary` is: an operator who set it must be able to confirm what the record now says, and
    # "which store" is the field whose being wrong looks like a missing table rather than like
    # misconfiguration. None on records using the estate endpoint.
    endpoint: str | None = None
    # [[LH-067]] The NAME of the secret holding this warehouse's credential, never the credential, and
    # safe to expose precisely because it is a reference: it identifies a key in the secret store,
    # and holding it grants nothing without access to that store. No request sets it and no door reads
    # it ([[LH-205]]), which is why a record naming another store is refused rather than served.
    credential_ref: str | None = None
    created_at: str | None = None
    # `git` while git declares the warehouse ([[XC-213]]): its doors then refuse a change to its status, protection,
    # delete profile and its delete, which the declaration owns. `self` for a warehouse its own doors manage.
    managed_by: Literal["git", "self"] = "self"
    #: The warehouse's own delete profile; None where it takes the estate's.
    delete_profile: DeleteProfile | None = None
    #: What the probe's seal check last measured of the warehouse's store (ADR 0242): `enforced` (a put over a sealed file
    #: is refused), `unenforced` (the store took one) or `untaggable` (it refused the seal). None where no check ran: a
    #: deployment vending no direct credential. A warehouse other than `enforced` registers only where git declares it
    #: detection-only.
    sealed: Literal["enforced", "unenforced", "untaggable"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _profile_from_record(cls, data: Any) -> Any:  # noqa: ANN401 — the record or a model, as pydantic hands it
        """A record keeps its profile as the strings ``delete_mode`` and ``trash_grace_days``."""
        if isinstance(data, dict) and data.get("delete_mode") not in (None, DeleteMode.ESTATE) and "delete_profile" not in data:
            grace = data.get("trash_grace_days")
            return {**data, "delete_profile": {"mode": data["delete_mode"], "grace_days": float(grace) if grace else None}}
        return data


class VendedCredentials(BaseModel):
    """Scoped storage credentials for one table at one tier.

    ``storage_options`` is consumed directly by pylance / lance-ray /
    object_store. ``expires_at_millis`` is when the client must refresh
    (``None`` for long-lived static keys).
    """

    storage_options: dict[str, str]
    expires_at_millis: int | None = None


class CredentialResponse(BaseModel):
    """The vending result. ``mode="direct"`` carries scoped ``credentials``; ``mode="server_mediated"``
    means no credential was issued (Mode B / unknown bucket) — the client uses the data endpoints.

    ``location`` + ``read_version`` give a direct writer its write TARGET and the version its commit follows in
    one round-trip: the client writes under ``location`` and stages its commit, which the catalog finalizes
    through the spec's ``CreateTableVersion`` (ADR 0157)."""

    mode: str
    credentials: VendedCredentials | None = None
    location: str | None = None
    read_version: int | None = None
    #: The vended ref's data file version and stable-row-id setting, which every file a writer stages must match, or
    #: ``version/create`` refuses the commit ([[LH-260]]); for a table holding no committed version, the format its first
    #: commit must carry. The table vend door answers it in every mode; a trash reclaim session, which writes nothing,
    #: answers none.
    file_format: TableFileFormat | None = None


class TrashReleaseResponse(BaseModel):
    """What releasing a trash record past its deadline did: the grants revoked, and whether the record was stamped."""

    tuples_revoked: int
    #: ``False`` when the record was gone by the stamp, purged or undropped meanwhile; the grants are revoked either way.
    stamped: bool


class TrashEndResponse(BaseModel):
    """What ending a trash record did beside ending its records: the grants it still held, revoked."""

    tuples_revoked: int


class TrashRefusalRequest(RequestBody):
    """Why maintenance declined to act on a trash record this tick, as its own report names it."""

    reason: str = Field(min_length=1, max_length=2000)


class TrashRefusalResponse(BaseModel):
    """How many refusals the record now counts; ``None`` when the note could not be made (the record went meanwhile)."""

    attempts: int | None = None


class CloneEndingEntry(BaseModel):
    """One registered clone's ending that its door could not clear (ADR 0198 §1): the digest its finish is asked by."""

    #: The SHA-256 of the clone's decoded location, which its tags, its claim and its endings are kept under.
    digest: str
    #: The token of the request that filed it, which names this ending under the digest.
    filing: str
    #: The door that ended the clone.
    door: str
    filed_at: datetime


class CloneEndingsResponse(BaseModel):
    """Every clone's ending not yet cleared, oldest first."""

    endings: list[CloneEndingEntry]


class CloneEndingFinishResponse(BaseModel):
    """What finishing one clone's ending did: how many source tags it released, or that the clone itself stands there
    for good and keeps them."""

    tags_released: int
    kept: bool


class IdempotencyKeysReclaimResponse(BaseModel):
    """What one reclaim of expired `Idempotency-Key` records did ([[LH-576]])."""

    #: Records deleted.
    reclaimed: int
    #: Expired records a retry wrote again since they were listed, kept for another lifetime.
    kept: int
    #: Expired records past this call's limit, for the caller to ask again.
    left: int
    #: Objects under the records' prefix that are no record, left alone.
    unreadable: int


class AnnouncementIntentsFinishResponse(BaseModel):
    """What one finish of the announcements doors left did ([[LH-576]])."""

    #: Filings judged and removed.
    finished: int = 0
    #: Announcements sent, their change shown landed.
    sent: int = 0
    #: Announcements not sent, the store showing their change did not land.
    unlanded: int = 0
    #: Filings left to their door, younger than a door can act.
    young: int = 0
    #: Filings past this call's limit, for the caller to ask again.
    left: int = 0


class GrantCensusResponse(BaseModel):
    """Every object's grant count, by type, as the store holds them now: what a drift report's authorization categories
    read. The objects a deployment keeps for itself are left out, since no registry names them."""

    #: ``False`` when authorization is off in the catalog: the store holds nothing to count, which is not an empty one.
    enabled: bool
    #: ``{object_type: {object_id: tuple_count}}``, bookkeeping relations excluded.
    counts_by_type: dict[str, dict[str, int]] = Field(default_factory=dict)
    #: Why the read stopped short of the store, when it did; a reader treats such a census as incomplete.
    truncated: str | None = None


class GhostRevokeRequest(RequestBody):
    """The objects a drift report found holding grants that no registry names, each an OpenFGA object (``kind:id``)."""

    objects: list[str] = Field(min_length=1, max_length=500)


class GhostRevokeResponse(BaseModel):
    """What the catalog revoked, each object's tuple count, and what it refused to, with why."""

    revoked: dict[str, int] = Field(default_factory=dict)
    refused: dict[str, str] = Field(default_factory=dict)


class TenancyRebuildRequest(RequestBody):
    """The warehouses whose tenancy edge a drift report found missing, by id."""

    warehouses: list[str] = Field(min_length=1, max_length=500)


class TenancyRebuildResponse(BaseModel):
    """The tenancy edges the catalog wrote, each ``project:<p> project warehouse:<id>``, and the warehouses whose record
    justifies none, with why."""

    written: list[str] = Field(default_factory=list)
    unjustified: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Deletion protection + recoverable drops (#73 / #75)
# --------------------------------------------------------------------------- #


class SetProtectionRequest(RequestBody):
    """The one field the protection doors write. Setting is idempotent; clearing removes the flag.

    ONE definition for the table, namespace and warehouse doors, so the contract names one schema: a copy per
    endpoint module makes FastAPI qualify each name with its module path
    (``catalog__api__v1__endpoints__tables__SetProtectionRequest``), which every generated client then carries.
    """

    protected: bool


class ProtectionResponse(BaseModel):
    id: str
    protected: bool
    # Who armed it (`user:<principal>`), from the record — None on an unprotected object and on the SET
    # response (the caller knows who they are). A refused drop can then NAME the armer (#123).
    set_by: str | None = None


class LabelDefinitionRequest(RequestBody):
    """A label a project defines, or the definition its name holds, evolved (ADR 0211).

    The name is the label's for good; its scope and its allowed values only grow, and its kind never changes, so a
    label already attached stays valid.
    """

    name: str
    description: str | None = None
    #: The object kinds it may be attached to.
    scope: list[LabelScope]
    #: ``marker`` carries no value, ``free_text`` any value, ``enumerated`` one of ``allowed_values``.
    kind: LabelKind
    allowed_values: list[str] | None = None


class SetLabelRequest(RequestBody):
    """Attach a label the object's project defines, with its value; a marker carries none."""

    name: str
    value: str | None = None


class RemoveLabelRequest(RequestBody):
    """Remove a label from the object."""

    name: str


class ObjectLabelsResponse(BaseModel):
    """The labels a table or namespace carries, each with its value (``None`` for a marker)."""

    kind: LabelScope
    id: str
    uuid: str
    labels: dict[str, str | None]
    #: On a describe, the labels its project defines for its kind, each a ``LabelDefinition``: what its owner may
    #: attach, read by whoever may read the object, whether or not they are a member of the project.
    available: list[LabelDefinition] | None = None


class ContractResponse(BaseModel):
    """The data contract a table holds (ADR 0212): the ODCS v3 document its owner set, or none."""

    table: str
    uuid: str
    #: The ODCS v3 document as it was set; ``None`` for a table holding no contract.
    contract: dict[str, Any] | None = None
    set_by: str | None = None
    set_at: datetime | None = None
    #: Whether the set that wrote ``contract`` answered 200: false while it is in flight, or for one that died there, whose
    #: record holds every commit to it and to the contract in force before it until its owner sets or deletes one (ADR
    #: 0244). ``None`` for a table holding no contract.
    in_force: bool | None = None


class LabelledObjectsResponse(BaseModel):
    """A page of a project's labelled objects the caller may see, each named as it is named now."""

    project: str
    objects: list[ObjectLabelsResponse]
    page_token: str | None = None


class TrashEntry(BaseModel):
    """One recoverable drop — what the owner needs to decide whether to undrop before the deadline."""

    #: A dropped table or a namespace a recoverable cascade dropped; each has its own undrop door.
    kind: Literal["table", "namespace"]
    #: The name the object held when it was dropped, which the undrop restores it under.
    id: str
    #: The object's uuid: several dropped objects may have held one name, and an undrop names one by it (`?uuid=`).
    uuid: str
    location: str
    dropped_by: str
    dropped_at: str
    #: When recovery ends (ADR 0146): past it the undrop is refused, the object's grants are revoked, and its
    #: bytes wait for the purge.
    expires_at: str
    #: True once `expires_at` has passed: the drop is final, though its bytes may stand until the purge.
    expired: bool = False


class TrashListing(BaseModel):
    """A page of a warehouse's trash: each recoverable drop under it its caller holds a grant on ([[LH-556]])."""

    objects: list[TrashEntry]
    #: Continues the listing; absent once every drop was judged (`lance_docs/ns_catalog/spec.yaml` PageToken).
    page_token: str | None = None


class MeProject(BaseModel):
    """One tenant the caller belongs to, at their strongest role (``admin`` wins over ``member``)."""

    project: str
    role: Literal["admin", "member"]


class MeResponse(BaseModel):
    """The caller's self-describe: verified claims + effective (possibly degraded) FGA standing."""

    sub: str
    name: str | None
    email: str | None
    estate_admin: bool
    #: Whether ``POST /management/v1/projects`` would admit the caller: the estate admin or an operator (ADR 0143).
    can_create_project: bool
    projects: list[MeProject]


#: The membership ladder the members API will grant, most privileged first, and the request type that
#: names one rung of it. Here rather than in ``endpoints/members.py`` because ``MemberList`` advertises the ladder
#: on the wire (so a UI does not keep a second copy of the model's rungs) — the endpoint's own policy
#: constants (lock-out protection's ``ADMINISTRATIVE``) stay with the routing logic that enforces them.
GRANTABLE: tuple[str, ...] = ("admin", "member")
Rung = Literal["admin", "member"]


class Member(BaseModel):
    """One direct grant on this tenant."""

    #: The FGA user string as stored (``user:gina``, ``role:<id>#assignee``). Verbatim,
    #: because it is what a revoke must send back and a prettified name that cannot round-trip is a trap.
    user: str
    relation: str


class MemberList(BaseModel):
    members: list[Member]
    #: The rungs this API will grant, so a UI does not keep a second copy of the model's ladder.
    grantable: list[str] = Field(default_factory=lambda: list(GRANTABLE))


class GrantRequest(RequestBody):
    #: A bare subject (``gina``) or a full FGA user string. Normalised by the endpoint — asking a UI to
    #: know the prefix is asking it to know the authorization model.
    user: str = Field(min_length=1, max_length=256)
    relation: Rung


class RoleCreateRequest(RequestBody):
    """A role to mint in a project ([[LH-557]]): its name, unique in the project, and what it is for."""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1024)


class RoleRecord(BaseModel):
    """A project's role: ``role:<id>`` in OpenFGA is this ``id``."""

    id: UUID
    project: str
    name: str
    description: str | None = None
    created_at: datetime
    #: The principal that minted it.
    created_by: str


class Role(RoleRecord):
    """A role and its direct assignees, each the FGA user string a revoke sends back (``user:<principal>``, or
    ``role:<id>#assignee`` for a nested role). The assignees are left out of a listing."""

    assignees: list[str] | None = None


class RoleList(BaseModel):
    roles: list[Role]


class AssigneeRequest(RequestBody):
    """One assignee of a role: a principal (``dex~gina``), its ``user:`` id, or another role of the same project
    (``role:<id>#assignee``)."""

    user: str = Field(min_length=1, max_length=256)


class RoleDeleted(BaseModel):
    """What one delete of a role did: the role, and how many tuples on it and naming it went with it."""

    role: RoleRecord
    tuples_revoked: int


# --------------------------------------------------------------------------- #
# Control events (`/management/v1/events`)
# --------------------------------------------------------------------------- #


class EventsResponse(BaseModel):
    """A poll response: control events strictly after the caller's cursor, read off the control stream, the cursor after
    them (the stream's sequence, the same on every replica), and a reset flag (``True`` when the stream no longer holds
    what came after the caller's cursor → the console should re-read all)."""

    events: list[CatalogControlEvent]
    cursor: int
    reset: bool


# --------------------------------------------------------------------------- #
# Warehouse registry reads + deletion (`/management/v1/warehouses`)
# --------------------------------------------------------------------------- #


class DeleteWarehouseResponse(BaseModel):
    """What the delete ACTUALLY did — reported step by step, never assumed.

    A warehouse delete touches four independent stores (the native namespaces, OpenFGA, the registry, the
    bucket) and only three of them run by default. Reporting each separately is what makes a partial
    failure diagnosable instead of a lie: ``bucket_purged=false`` names bytes that are still there, and
    ``tuples_revoked`` is a count OpenFGA really returned.
    """

    id: str
    #: The bucket the warehouse owned — named even when it was NOT purged, so the operator knows what survived.
    bucket: str
    #: Every namespace a ``cascade`` actually dropped, nested ones included, in the order it dropped them (empty on a
    #: non-cascade delete).
    namespaces_dropped: list[str]
    #: Tuples removed across the warehouse object AND every namespace and table a cascade removed under it, those a
    #: recoverable drop kept included. 0 when FGA is off.
    tuples_revoked: int
    bucket_purged: bool
    objects_purged: int
    #: Why ``?purge_bucket=true`` deleted no byte: the bucket's claim was released, absent or another tenant's, so the
    #: purge could not take it for itself and kept every byte (ADR 0241 §10 Q1). Absent where nothing was kept back.
    bytes_kept: str | None = None


# --------------------------------------------------------------------------- #
# Tenants (`/management/v1/projects`)
# --------------------------------------------------------------------------- #


class ProjectWarehouse(BaseModel):
    """One warehouse owned by the project — the registry facts an estate observer needs at a glance."""

    id: str
    bucket: str
    status: str


class ProjectResponse(BaseModel):
    """A derived tenant: its name, the warehouses claiming it, its effective FGA admins (``[]`` when
    FGA is off/unavailable — degraded, never fabricated), and its deletion protection."""

    project: str
    warehouses: list[ProjectWarehouse]
    admins: list[str]
    #: Whether the project's delete is refused without `force=true` ([[LH-549]]), set and cleared through
    #: `POST /management/v1/projects/{project_id}/protection`; a tenant no registry record names is never protected.
    protected: bool = False
    #: Who armed the protection (`user:<principal>`); None while the project is unprotected.
    protected_by: str | None = None
    #: `git` while git declares the project ([[XC-213]]): its doors then refuse a change to its protection and its
    #: delete, which the declaration owns. `self` for a project its own doors manage.
    managed_by: Literal["git", "self"] = "self"


class CreateProjectRequest(RequestBody):
    """The create payload: the tenant's id, and who administers it when a machine creates it."""

    id: str
    #: The person seeded as the project's admin. Required when the caller is a machine identity, which
    #: never becomes a tenant's admin itself (LH-433); refused from a person, who is the admin.
    admin: str | None = None


class DeleteProjectResponse(BaseModel):
    """What the delete actually removed: the retired tenant and how many FGA tuples went with it.

    The count is reported rather than assumed — an operator retiring a tenant needs to see that the authz
    side really happened (``0`` on an FGA-off stack is a fact, not a silent success)."""

    project: str
    tuples_revoked: int


class TableChangesRequest(RequestBody):
    """What a consumer asks for when following a table (§ J4).

    NOT a spec model — the Lance Namespace spec has no change-feed op — so it is declared here rather
    than imported. That makes this a NON-SPEC route on a spec-conformant surface, which § B2 is already
    carving out (25 route groups); this is a deliberate 26th, placed with `query`/`count_rows`/`blobs`
    because a change feed is a DATA-PLANE read, not a management operation. When B2 carves, it moves
    with the data doors, not with the admin ones.
    """

    begin_version: int
    end_version: int | None = None
    kind: ChangeKind = "inserted"
    columns: list[str] | None = None
    #: Honoured like every other read door's: a branch-scoped feed that answered from main would report
    #: changes for a dataset the caller did not ask about — the defect `count_rows` already paid for.
    branch: str | None = None
