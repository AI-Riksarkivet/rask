"""OpenFGA authorization dependency.

Router-level guard: when FGA is enabled it maps the request (resource + operation) to a
``can_*`` ACTION relation defined in the model (``service_kit/governed/auth/model.fga``) and checks the
authenticated caller against OpenFGA. A no-op when FGA is disabled.

The op -> ``can_*`` mapping below is the ONLY policy logic in the app; the privilege math
(which rung each ``can_*`` reduces to, and how owner/writer/reader cascade through
``parent``) lives in the model — so it is testable in ``model.fga.yaml`` and precise for
``list_objects``. Mapping (behaviour-identical to the previous reader/writer/owner code):

  read ops   -> can_get_metadata / can_read_data        (reader rung)
  mutations  -> can_write_data / can_update_properties   (writer rung)
  lifecycle  -> can_drop / can_deregister / can_delete   (owner rung)
  create     -> can_create_table / can_create_namespace on the PARENT (create-on-parent)
  undeclared -> refused for every caller                 (no rung is a default)

Create-on-parent: creating a child authorizes the parent (the child has no id yet); a
top-level child gates on the DEFAULT WAREHOUSE only when ``fga_lock_root_create`` is set,
else it is open self-serve. Batch routes read the (Starlette-cached) body and check each
named table. Fail-closed twice over: an unwired client raises 503 here, and
``fga.check``/``fga.batch_check`` raise 503 on an OpenFGA outage rather than allowing.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from functools import lru_cache
from typing import Final, Literal

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    InternalError,
    InvalidInputError,
    InvalidTableStateError,
    LanceNamespace,
    NamespaceAlreadyExistsError,
    NamespaceExistsRequest,
    NamespaceNotEmptyError,
    NamespaceNotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableAlreadyExistsError,
    TableExistsRequest,
    TableNotFoundError,
    UnauthenticatedError,
    UnsupportedOperationError,
)
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict

from catalog.api.dependencies import FgaClientDep, SettingsDep
from catalog.api.security import Caller, CurrentToken
from catalog.core import run_context, tier_policy
from catalog.core.config import Settings
from catalog.core.identifiers import MAX_NAMESPACE_DEPTH, parse_identifier
from catalog.services import native, object_uuids, table_claims, warehouses
from catalog.services.table_claims import ResolvedTable
from service_kit.governed import fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, audit
from service_kit.governed.machine_identity import ServicePrincipal
from service_kit.lakehouse import object_records, protection, trash
from service_kit.lakehouse.subjects import is_person_subject


log = logging.getLogger(__name__)

# Guarded resource prefixes, in match order. Each maps to its own dedicated FGA type in the model
# (``service_kit/governed/auth/model.fga``): ``materialized_view`` and ``transaction`` are no longer aliased
# to ``table``. A transaction is authorized PARENT-SCOPED against its enclosing namespace (see
# ``_authorize_transaction``) — the old ``table:<txn-id>`` alias targeted an object nothing ever seeds,
# so every transaction op always-denied.
#: `classification` is the odd one and belongs here anyway: it is a governance VOCABULARY rather than a
#: data object — no rows, no parent warehouse, and the path segment IS the label ([[LH-055]]). It
#: reaches this map because its grant/revoke doors must be authorized like every other, through
#: `_authorize_grant` reading the rung out of the body.
_RESOURCES: tuple[str, ...] = ("namespace", "table", "materialized_view", "transaction", "classification")
_FGA_TYPE: dict[str, str] = {
    "namespace": "namespace",
    "table": "table",
    "materialized_view": "materialized_view",
    "transaction": "transaction",
    "classification": "classification",
}

# Read-only trailing actions (reader rung). ``query``/``count_rows`` read DATA; the rest
# read metadata — split so list_objects(can_read_data) is meaningful (both are reader).
# ``credentials`` (vending) is a DATA read at minimum — the router guard requires can_read_data; the
# endpoint additionally requires can_write_data for a write-tier vend and can_vend_maintain for a maintain-tier one.
# ``blobs`` (GET /v1/table/{id}/blobs — the credential-less blob serving path) returns raw payload
# bytes, so it is a DATA read exactly like ``query``.
# ``changes`` (§ J4) is the MOST disclosing read a table has — every row it ever received, in order —
# so it is a data read and nothing weaker. Unmapped it fell to WRITER, and the live audit trail is what
# said so (2026-09-08: `can_write_data ALLOW` on `table:bronze$pages`, logged one line above the
# `read_data` record for the same call). That refuses every reader who is not also a writer, which for a
# change feed is its entire audience, and it writes a fiction into the compliance trail.
_DATA_READ_ACTIONS = frozenset({"query", "count_rows", "credentials", "blobs", "changes"})
# `tasks` (#75) reports what is SCHEDULED against this object — a pending undrop deadline today.
# Reader tier: it discloses no data and no principals, and an owner must be able to see the
# deadline they are racing.
# `history` (GET /v1/table/{id}/history) is the commit log — WHAT changed and WHEN, never who. Its own
# docstring already argued the rung ("the same rung as describe/list-versions") and the endpoint checks
# `can_get_metadata` itself; what was missing is this line, so the ROUTER demanded `can_write_data`
# first and that weaker second check was unreachable for anyone who had not already cleared the higher
# bar. Measured on the estate 2026-09-09: one GET logged `can_write_data ALLOW` then
# `can_get_metadata ALLOW`, in that order.
_META_READ_ACTIONS = frozenset({"describe", "exists", "list", "stats", "explain_plan", "analyze_plan", "version", "tasks", "history"})

#: The rungs that mean "this door only READS". Keyed on the relation `_action_relation` resolved, so a
#: door is classified by what it DOES rather than by how its path happens to be spelled — `access/list`
#: is owner-tier and its trailing segment is the word "list".
_READ_RELATIONS: Final = frozenset({"can_get_metadata", "can_read_data", "can_read"})

#: A SECOND door for one action, checked through :func:`_require_any` only when the primary denies.
#:
#: `credentials` is a data read for everyone else, so `can_read_data` stays the primary rung. But a
#: MAINTAINER is deliberately not a reader — `model.fga` states that and its test block asserts it —
#: while it must still obtain the credential its own rewrite needs. The two rules are individually
#: right and jointly denied: MEASURED 2026-09-08, 207 of 285 rewrites a tick fell back to the
#: deployment's ROOT key because this router refused before the endpoint's tier check ever ran.
#:
#: PER ACTION, never per tier. The router could read `?tier=`, and it would buy nothing: a maintain-tier
#: session policy grants `s3:GetObject` alongside `PutObject`, so a maintainer that can rewrite can
#: already read those bytes. Gating the second door on the tier would add a moving part and no
#: privilege boundary. What it must NOT become is a rung on the reader tier at large — `query` and
#: `blobs` stay `can_read_data`, pinned by
#: `tests/integration/test_the_maintainer_rung_opens_the_maintain_tier_vend.py`.
_ALTERNATIVE_RUNGS: dict[tuple[str, str], str] = {("table", "credentials"): "can_maintain"}


# Full route suffixes that create a NEW child -> authorize the parent (create-on-parent).
_CREATE_ON_PARENT_SUFFIXES: dict[str, frozenset[str]] = {
    "table": frozenset({"create", "declare", "register"}),
    "namespace": frozenset({"create"}),
    "materialized_view": frozenset({"create"}),
}

# Full route suffixes that are lifecycle/destructive/ref-plane on the object itself (owner rung).
# Matched on the FULL suffix so ``index/{index_name}/drop`` does not inherit ``drop``'s owner rung.
# ``rename`` is owner-tier: it DESTROYS the source id (revokes its tuples) and re-seeds the caller as owner
# of the destination, so a writer-tier gate would let a non-owner rename another user's table and seize sole
# ownership (the same escalation closed for Overwrite via require_can_drop_table). Owner-gate the source.
# ``restore`` REWINDS the table to an older version (destructive) and ``branches/create`` FORKS history;
# both are privileged and stay at the owner rung. ``tags/create`` / ``tags/update`` re-point the
# ref-plane and are privileged too — above the writer rung, since a plain data writer appends and
# overwrites within the current line only — but they resolve through ``owner or publisher`` rather than
# ``owner`` alone (owner ruling 2026-09-10). A tag is a NAMED POINTER at a version that already exists,
# so blessing one destroys nothing, and welding that to ownership is what forced every cascade identity
# to hold ``owner`` on every tenant. Their ``*/list`` / ``*/version`` siblings are reads, resolved on the
# trailing segment below; ``tags/delete`` is mapped explicitly just below.
_OWNER_SUFFIX_RELATION: dict[str, dict[str, str]] = {
    "table": {
        "drop": "can_drop",
        "deregister": "can_deregister",
        "rename": "can_drop",
        "restore": "can_restore",
        "branches/create": "can_create_branch",
        # DESTROYING a branch destroys its data AND its own version sequence — irreversible, and the
        # heavier of the two deletions this table offers. Absent from this map it fell through to the
        # writer default, so measured on the deployed catalog 2026-09-11 a non-owner cleared
        # `branches/delete` (422, past the gate) while the same caller was refused `tags/delete` and
        # `deregister` (403): the estate permitted the greater destruction and refused the lesser,
        # because a tag is a POINTER and its delete was already mapped here.
        #
        # `can_drop`, not a new relation: the rung already means "may destroy this table's contents" and
        # is what `tags/delete` and `version/delete` clear. A `can_delete_branch` would be a rung the
        # model must define and every existing owner grant would then lack.
        "branches/delete": "can_drop",
        "tags/create": "can_create_tag",
        "tags/update": "can_update_tag",
        # DELETE completes the tag lifecycle, and it clears the owner bar because at the writer rung a plain
        # data writer could remove the pin that `published` is —
        # and then defeat the owner-tier rollback guard, since publication refuses to move `published`
        # BACKWARDS but has nothing to say about republishing after the tag is gone.
        #
        # `can_drop` rather than a new relation: the bar it has to clear is the one already carried by
        # `maintenance/run`, which reclaims version history and EXEMPTS tagged versions. The door that
        # respects the tag cannot be the expensive one while the door that removes it is cheap.
        "tags/delete": "can_drop",
        # Same rung, same reason, one step more direct: this destroys VERSIONS rather than a pin — the
        # reclamation `maintenance/run` performs, on versions the caller names. The door refuses a
        # tagged, forked-from or current version itself; the rung decides who may shorten the history at
        # all. `version/list` and the read-side siblings still fall through to the reader tier.
        #
        # This rung carries more weight here than it would in an Iceberg catalog, and deliberately:
        # Lance keeps the CAS in the object store, so the serving pointer is a tag INSIDE the dataset
        # rather than a row a catalog transaction owns. Authorization is the only thing guarding it.
        "version/delete": "can_drop",
        # #50 maintenance policies: a retention policy authorizes destroying this table's version
        # history over time — the drop rung, not a plain write. `policy/describe` falls through to
        # the reader tier via its trailing segment.
        "policy/set": "can_drop",
        "policy/delete": "can_drop",
        # #51 access review: enumerating who holds access reveals principals, so it clears the
        # owner bar — a reader must not be able to map the ACL they are inside of.
        "access/list": "can_drop",
        # #68 access simulation (the "who can do what" playground check): probing an arbitrary
        # (user, relation) is the same authz-graph disclosure as enumerating it, so it clears the
        # same owner bar.
        "access/check": "can_drop",
        # The SELF-view is the one access route that must NOT clear the owner bar. `access/list` and
        # `access/check` disclose principals; "what may I do here" discloses only the caller to
        # themselves. Owner-gating it would mean only the people who already know the answer could
        # ask — which is precisely why no surface over these primitives ever shipped. Reader tier:
        # you may ask about anything you can see. Mapped EXPLICITLY: `my-permissions` is no read word, so
        # the trailing-segment rule below cannot classify it.
        "access/my-permissions": "can_get_metadata",
        # NOTE: `access/grant` and `access/revoke` are NOT here. They gated on the owner bar until the
        # grant axis landed, which meant handing out access required owning the data. They are now
        # authorized per-rung from the BODY (`can_grant_<relation>`) by `_authorize_grant`, which
        # intercepts before `_action_relation` is consulted — a mapping here would be dead code that
        # reads like the live rule.
        # #81 authorization graph: one hop of the relationship graph discloses principals + the cascade,
        # the same disclosure as access/list — owner bar.
        "access/graph": "can_drop",
        # #75 on-demand maintenance: previewing and reclaiming version history are the drop rung, like the
        # retention policy that schedules them.
        "maintenance/preview": "can_drop",
        "maintenance/run": "can_drop",
        # [[LH-073]] erasure destroys the table's PAST — every ref's rows, the tags pinning the versions
        # that held them, and the history itself. That is a stronger claim than a drop, and `can_drop`
        # is the highest rung there is, so it gates here. The writer rung would let a plain data writer
        # reclaim an owner's history under a compliance-shaped verb.
        "erasure": "can_drop",
        # [[LH-493]] verifying an erasure obligation reclaims the history the requester's `retain_days` already ordered
        # reclaimed and releases the obligation. It is the bring-your-own job's step, so it sits at the maintainer rung
        # the job holds beside `writer` (ADR 0163); the owner holds it too. The caller's word decides nothing: the
        # catalog reads storage itself and holds the predicate to the obligation's digest.
        "erasure/{obligation_id}/verify": "can_maintain",
        # #73 deletion protection: arming/disarming the safety on an object is a statement about its
        # DESTRUCTION, so it clears the same owner bar as the drop it guards — a writer must not be
        # able to disarm protection they could never act on.
        #
        # ITS OWN RELATION at that same tier ([[LH-055]]), not `can_drop`: `#41` audits every decision by
        # the RELATION, and the route suffix is not in the record, so sharing the name made "dropped this
        # table" and "armed the safety on it" write identical audit lines. `can_set_protection: owner` is
        # a computed userset, so every owner already holds it and no grant had to change.
        "protection": "can_set_protection",
        # #75 undrop RESTORES the object into its namespace — the same authority as removing it.
        "undrop": "can_drop",
        # Publication ADVANCES the `published` tag, so it clears the same bar as `tags/update` — the
        # rung that already exists for re-pointing the ref plane. Anything lower would make `publish`
        # a way for a plain data writer to move a tag they are not allowed to move directly, which is
        # the escalation the ref-plane rungs exist to close. The quality gate is orthogonal: it
        # decides whether the DATA may be published, this decides whether the CALLER may.
        "publish": "can_update_tag",
        # The tag gate's answer (ADR 0156 C1, [[LH-466]]): approving a held version moves `published` past the
        # project's gate, and rejecting it restores main. Both are the VALIDATOR's act, a rung no writer or
        # publisher holds, so a writer cannot approve its own held version (ADR 0160).
        "promotion/approve": "can_promote",
        "promotion/reject": "can_promote",
    },
    "namespace": {
        "drop": "can_delete",
        # #96 undrop RESTORES the subtree into the estate — the same authority as removing it (the
        # table door's #75 rule at the namespace rung).
        "undrop": "can_delete",
        # The namespace twin of the table door's rung — same tier as the `can_delete` it guards, its own
        # name for the same audit reason.
        "protection": "can_set_protection",
        "policy/set": "can_delete",
        "policy/delete": "can_delete",
        "access/list": "can_delete",
        "access/check": "can_delete",
        # Same as the table block: grant/revoke are body-authorized by `_authorize_grant`.
        # #C4 managed access. SUFFIX PER OPERATION (`policy/describe` vs `policy/set`, the convention
        # already here) — and not stylistic: this map is keyed on the PATH ALONE, so one `managed-access`
        # suffix serving both a read and a write can only carry ONE tier, and the write's tier is the
        # wrong one for the read.
        #
        # The write is a grant-management act, not a property edit — `can_set_managed_access` derives
        # from `manage_grants`. At the writer rung (`can_update_properties`) any writer could switch off
        # the control that stops owners widening access.
        "managed-access/set": "can_set_managed_access",
        # The read sits at the READER bar deliberately: this flag is the REASON someone's grant controls
        # are missing, so the person who most needs the answer is exactly the one who may not change it.
        # Gating the read at the write's bar turns a stated policy back into an unexplained absence.
        "managed-access/describe": "can_get_metadata",
        # The self-view sits at the READER bar here too — see the table block for why. Spelled out per
        # type: each type's map is its own, and a suffix it does not declare is refused.
        "access/my-permissions": "can_get_metadata",
        "access/graph": "can_delete",
    },
}

#: The WRITER rung, declared door by door on the FULL suffix exactly like the owner map above, because a
#: suffix that neither map nor the read vocabulary names is REFUSED rather than
#: defaulted. A default rung is a permission nobody chose. At the writer rung it is one every plain data
#: writer already holds, so a new destructive verb would ship open to all of them with nothing red; and
#: a new read door would refuse its whole audience while the audit trail recorded a write. The refusal
#: is loud for every caller, owners included, and
#: `services/catalog/tests/test_an_undeclared_door_is_refused.py` drives the guard over every mounted
#: route, so a door added without a rung fails there rather than in front of its first caller.
#:
#: A `{name}` segment stands for exactly one path segment, as the route's own `str` convertor does, so
#: `index/{index_name}/drop` names the index-drop door without reaching the table's own `drop`.
#:
#: `namespace` declares no writer door. Every namespace route is a read, an owner-tier verb or a
#: create-on-parent, so a namespace mutation nobody declared is refused rather than resolved to
#: `can_update_properties`.
_WRITER_SUFFIX_RELATION: Final[dict[str, dict[str, str]]] = {
    "table": dict.fromkeys(
        (
            "insert",
            "merge_insert",
            "update",
            "delete",
            "add_columns",
            "alter_columns",
            "drop_columns",
            "backfill_column",
            "update_field_metadata",
            "schema_metadata/update",
            "create_index",
            "create_scalar_index",
            "index/{index_name}/drop",
            "version/create",
        ),
        "can_write_data",
    ),
    # The async rematerialize. `create` is create-on-parent and never reaches the resolver.
    "materialized_view": {"refresh": "can_refresh"},
}

# Grant/revoke are authorized from the BODY, per rung, by `_authorize_grant` — they appear in neither
# block above on purpose. `authorize` intercepts these suffixes before `_action_relation` runs, so a
# mapping here would never be consulted while reading exactly like the live rule.
_GRANT_SUFFIXES = frozenset({"access/grant", "access/revoke"})

# Body-keyed batch routes (no ``{id}`` path param); the tables are named in the body.
_BATCH_PATHS = frozenset({"/v1/table/version/batch-create", "/v1/table/batch-commit"})

# A batch sub-operation is authorized at its dedicated route's rung, and a destructive one at the
# owner tier: deregister_table is the only destructive kind, since the wire model
# (CommitTableOperation) has no drop op. Both batch doors answer 406 and run nothing ([[LH-206]]);
# the guard answers first, as it does at every door, so a caller without the rung reads 403.
# Maps op key -> can_* relation.
_BATCH_OWNER_OPS: dict[str, str] = {"deregister_table": "can_deregister"}


#: The mount prefixes this guard understands. `/management/v1` is the rask-only surface ([[LH-021]]),
#: and it is HERE rather than assumed because both helpers below matched `/v1/` literally: the first
#: route mounted on the management prefix resolved to no resource, fell out of `authorize` entirely,
#: and was reachable by any authenticated principal. It was the ERASURE door, which destroys a table's
#: history. `test_stores_reads_are_gated.py` caught it before it shipped, and this list is what keeps
#: the next management route from repeating it.
_MOUNTS: Final = ("/management/v1", "/v1")


def _resource_for(path: str) -> str | None:
    """The guarded resource a ``<mount>/<resource>/…`` path belongs to, else ``None``."""
    for mount in _MOUNTS:
        for resource in _RESOURCES:
            if path.startswith(f"{mount}/{resource}/"):
                return resource
    return None


#: The only actions allowed on the root namespace, which names no FGA object: reads that filter every
#: item they return (`list`, `table/list`) or reveal nothing a tuple guards (`describe`, `exists`).
_ROOT_READ_SUFFIXES: Final = frozenset({"list", "table/list", "describe", "exists"})


def _suffix(path: str, resource: str, object_id: str) -> str:
    """The route part after ``<mount>/<resource>/{id}/`` (e.g. ``version/create``).

    Checked against every mount, longest first, so `/management/v1/table/{id}/erasure` yields
    `erasure`. A path no mount strips yields `""`, which names no door and is refused.
    """
    clean = path.rstrip("/")
    for mount in _MOUNTS:
        prefix = f"{mount}/{resource}/{object_id}/"
        if clean.startswith(prefix):
            return clean.removeprefix(prefix)
    return ""


def _object(fga_type: str, id_segments: list[str] | str, delimiter: str) -> str:
    """``<type>:<canonical-id>`` for an object (e.g. ``table:db1$users``)."""
    return f"{fga_type}:{fga.canonical_object_id(id_segments, delimiter=delimiter)}"


def _declared_relation(declared: Mapping[str, str], suffix: str) -> str | None:
    """The relation ``declared`` names for ``suffix``, else ``None``.

    A ``{name}`` segment of a declared suffix matches exactly one non-empty segment of ``suffix``.
    """
    if suffix in declared:
        return declared[suffix]
    parts = suffix.split("/")
    for pattern, relation in declared.items():
        segments = pattern.split("/")
        if len(segments) == len(parts) and all(
            segment == part or (segment.startswith("{") and segment.endswith("}") and part != "") for segment, part in zip(segments, parts, strict=True)
        ):
            return relation
    return None


def _undeclared_door(fga_type: str, suffix: str) -> InternalError:
    """The refusal for a door no rung map declares, for every caller — see ``_WRITER_SUFFIX_RELATION``.

    ``InternalError`` (spec code 18), not ``PermissionDenied``: authorization runs after routing
    matched, so a caller cannot invent a suffix and only a route shipped without its rung reaches this.
    Code 15 would send the caller to ask for access no grant can give.
    """
    return InternalError(f"{fga_type} declares no rung for {suffix!r}, so no caller may reach it")


def _record_undeclared(path: str, fga_type: str, suffix: str, *, user: str, obj: str) -> None:
    """Log and audit an undeclared-door refusal. The ERROR line names the path and suffix because the
    500 the caller receives is redacted, so it is where an operator finds the route that shipped bare."""
    log.error("undeclared_door", extra={"path": path, "fga_type": fga_type, "suffix": suffix})
    audit("authz", FAILURE, subject=user, resource=obj, reason="undeclared_door", suffix=suffix)


def _action_relation(fga_type: str, suffix: str) -> str:
    """The ``can_*`` relation to check for a non-create op, by object type + route suffix.

    Owner- and writer-tier doors are declared on the FULL suffix; reads are classified by the trailing segment. Every returned relation exists on ``fga_type`` in the model.

    Raises:
        InternalError: Nothing declares ``suffix`` for ``fga_type`` (see :func:`_undeclared_door`).
    """
    if (relation := _declared_relation(_OWNER_SUFFIX_RELATION.get(fga_type, {}), suffix)) is not None:
        return relation
    action = suffix.rsplit("/", 1)[-1]
    if fga_type == "namespace" and (action in _META_READ_ACTIONS or action in _DATA_READ_ACTIONS):
        return "can_get_metadata"
    if fga_type == "materialized_view":
        if action in _DATA_READ_ACTIONS:
            return "can_read"
        if action in _META_READ_ACTIONS:
            return "can_get_metadata"
    # `transaction` and `classification` resolve in `authorize`'s own branches, so neither is served here.
    if fga_type == "table":
        if action in _DATA_READ_ACTIONS:
            return "can_read_data"
        if action in _META_READ_ACTIONS:
            return "can_get_metadata"
    if (relation := _declared_relation(_WRITER_SUFFIX_RELATION.get(fga_type, {}), suffix)) is not None:
        return relation
    raise _undeclared_door(fga_type, suffix)


def _create_parent_check(resource: str, id_segments: list[str] | str, settings: Settings) -> tuple[str, str] | None:
    """``(parent_object, can_create_* relation)`` for a create-on-parent op, or ``None`` to allow.

    Nested child -> its parent ``namespace:<parent>``. Top-level child -> the default WAREHOUSE
    object only when ``fga_lock_root_create`` is set; otherwise ``None`` (open self-serve
    top-level create — preserves the default behaviour). The relation is named after the child
    being created (``can_create_namespace`` / ``can_create_materialized_view`` / ``can_create_table``),
    all of which reduce to the writer rung on the parent namespace.

    ``fga_default_warehouse_object``, never ``fga_root_object`` ([[LH-055]]): the three relations above
    are defined on ``warehouse`` and on ``namespace``, and on no other type. This lock defaults ON for a
    real deployment (``LANCE_FGA_LOCK_ROOT_CREATE`` = ``rask.isRealDeployment``) and OFF for local k3s,
    so checking them against a non-warehouse root would fail top-level creation in production and pass
    every local run — the failure mode with no local reproduction.
    """
    relation = {
        "namespace": "can_create_namespace",
        "materialized_view": "can_create_materialized_view",
    }.get(resource, "can_create_table")
    parent_id = fga.parent_namespace_id(id_segments, delimiter=settings.delimiter)
    if parent_id is not None:
        return f"namespace:{parent_id}", relation
    if settings.fga_lock_root_create:
        return settings.fga_default_warehouse_object, relation
    return None


async def _require(client: OpenFgaClient, *, user: str, relation: str, obj: str) -> None:
    """Check one ``relation`` on ``obj``, audit the decision, and raise 403 on denial."""
    try:
        allowed = await fga.check(client, user=user, relation=relation, obj=obj)
    except ServiceUnavailableError:  # authz layer down during an access attempt — audit, then fail closed
        audit(relation, FAILURE, subject=user, resource=obj, reason="authz_unavailable")
        raise
    audit(relation, ALLOW if allowed else DENY, subject=user, resource=obj)  # #41 audit every authz decision
    if not allowed:
        log.info("access_denied", extra={"sub": user, "relation": relation, "object": obj})
        raise PermissionDeniedError(f"{relation} required on {obj}")


async def _require_any(client: OpenFgaClient, *, user: str, doors: list[tuple[str, str]]) -> None:
    """Allow if ANY ``(relation, object)`` door checks out; audit the decision, and raise 403 otherwise.

    :func:`_require` expresses a single rung; a two-door gate (the project's own admin OR the estate admin)
    cannot be built by chaining it, because the first call would raise on the door the caller did not come
    through. Chaining with a swallowed 403 is no better: it writes a DENY into the #41 compliance trail for
    a legitimate admin's allowed request. Here exactly ONE ALLOW record is written, naming the door that
    decided; a full denial writes one DENY per door probed, so the trail still says precisely which rungs
    the caller lacked. Fail-closed identically to :func:`_require`: an OpenFGA outage on ANY probe audits a
    FAILURE and propagates (503) rather than falling through to the next door.
    """
    for relation, obj in doors:
        try:
            allowed = await fga.check(client, user=user, relation=relation, obj=obj)
        except ServiceUnavailableError:  # authz layer down mid-decision — audit, then fail closed
            audit(relation, FAILURE, subject=user, resource=obj, reason="authz_unavailable")
            raise
        if allowed:
            audit(relation, ALLOW, subject=user, resource=obj)
            return
    for relation, obj in doors:  # reaching here means every door was probed and every one denied
        audit(relation, DENY, subject=user, resource=obj)
    wanted = " or ".join(f"{relation} on {obj}" for relation, obj in doors)
    log.info("access_denied", extra={"sub": user, "relation": "any_of", "object": wanted})
    raise PermissionDeniedError(f"one of [{wanted}] required")


#: The transaction doors `_authorize_transaction` serves.
_TRANSACTION_SUFFIXES: Final = frozenset({"describe", "alter"})


async def _authorize_transaction(client: OpenFgaClient, settings: Settings, segments: list[str], suffix: str, *, user: str) -> None:
    """Authorize a transaction op — PARENT-SCOPED to the enclosing namespace when the id carries one.

    A transaction is a coordination artifact over its namespace's tables; nothing mints a per-transaction
    FGA object, so the previous ``table:<txn-id>`` check always denied (that object is never seeded).
    Instead:

    - A NAMESPACED txn id (``<ns>$<txn>``) inherits the caller's grant on ``namespace:<ns>``. ``describe``
      is a read (``can_get_metadata``); ``alter`` may commit/abort, so it is writer-tier
      (``can_update_properties``). Resolves on the FIRST call with no seed — this is the always-deny fix.
    - An OPAQUE root txn id (no enclosing namespace) is authorized against the ``transaction:`` object
      itself (``can_describe`` / ``can_set_status``), so it stays fail-closed until a grant is seeded on it.

    The dedicated ``transaction`` model type (``can_describe``/``can_set_property``/``can_set_status``/
    ``can_cancel``, cascading from ``parent``) backs the object-scoped branch and any future per-transaction
    direct grants; the finer set_status/set_property split lives in the model.

    The writer-tier relation on the PARENT branch must be one the ``namespace`` type actually defines: a
    namespace has no data plane, so ``can_write_data`` is a ``table``-only action and checking it against a
    ``namespace:`` object is a model error (OpenFGA 400 ``relation 'namespace#can_write_data' not found``)
    that fails closed to a 503 for EVERY caller — owners included. ``can_update_properties: writer`` IS the
    namespace writer rung, and it is exactly the rung the object-scoped branch resolves to for a parented
    transaction (``can_set_status: committer``, and ``committer`` ⊇ ``writer from parent``), so the two
    branches gate on the same privilege. ``tests/unit/test_fga_model_contract.py`` now proves every
    (type, relation) this module can check exists in the compiled model.

    ``describe`` and ``alter`` are the only transaction doors (``_TRANSACTION_SUFFIXES``); ``authorize``
    refuses any other suffix before calling this, the same rule ``_WRITER_SUFFIX_RELATION`` states for
    the resolver.
    """
    is_read = suffix == "describe"
    parent_ns = fga.parent_namespace_id(segments, delimiter=settings.delimiter)
    if parent_ns is not None:
        relation = "can_get_metadata" if is_read else "can_update_properties"
        obj = f"namespace:{parent_ns}"
    else:
        relation = "can_describe" if is_read else "can_set_status"
        obj = _object("transaction", segments, settings.delimiter)
    await _require(client, user=user, relation=relation, obj=obj)


@lru_cache
def _grant_actions(fga_type: str) -> frozenset[str]:
    """Every ``can_grant_*`` the compiled model defines on ``fga_type``.

    Read from ``model.json`` (what the app loads), never a hand-kept list: a relation named here but
    absent there is an OpenFGA 400 that fails CLOSED to a 503 for every caller, which is an outage
    wearing a permission error's clothes."""
    for td in fga.load_model()["type_definitions"]:
        if td["type"] == fga_type:
            return frozenset(r for r in (td.get("relations") or {}) if r.startswith("can_grant_"))
    return frozenset()


async def _authorize_grant(
    request: Request,
    client: OpenFgaClient,
    settings: Settings,
    *,
    user: str,
    fga_type: str,
    segments: list[str],
    revoking: bool,
) -> None:
    """Authorize grant/revoke on the RUNG in the body — ``can_grant_<relation>``, not a blanket bar.

    This route used to gate on the owner tier (``can_drop`` / ``can_delete``), which welded handing
    out access to owning the data: the only way to let someone administer access was to give them
    everything. The model now separates the two (``manage_grants`` / ``pass_grants``), and the gate
    has to read the body to use it — the path says *that* a grant is happening, only the body says
    *which rung*. Same shape as ``_authorize_batch`` above, and ``request.json()`` is cached by
    Starlette so the endpoint re-parses the same bytes.

    Fails CLOSED on anything unexpected. The body is client-controlled and this runs BEFORE Pydantic,
    so a non-dict, a non-string relation, or a rung the model has no ``can_grant_*`` for is a 403 —
    never a 500, and never a check on a phantom relation.

    **REVOKE IS NOT GATED IDENTICALLY TO GRANT, and used to be.** The old rule said "taking a rung
    away is the same authority as handing it out, and a weaker revoke bar would let a delegate strip
    the owner who delegated to them" — true of ``owner``, and false of everything at or below the
    delegate's own rung. ``can_grant_reader`` is ``manage_grants or (reader and pass_grants)``, so
    kevin (``writer`` + ``pass_grants``, the fixture's "delegate and no more") could DELETE carol's
    reader tuple, and every other reader/writer/validator tuple on the object including the owner's.
    Revoke goes to ``can_revoke_grant``, which every type the door serves defines stricter than its grants:
    ``manage_grants`` alone on a table or a namespace, ``curator`` alone on a classification label.

    SQL's WITH GRANT OPTION — which this axis is modelled on — lets a delegate revoke the grants THEY
    made. That needs the granting actor recorded ON the tuple, which this store does not carry, so
    the conservative bar is the honest one until it does.

    **AND A GRANT MAY NOT BE SELF-DIRECTED UNLESS IT IS A NO-OP.** ``can_grant_owner`` is
    ``manage_grants``, so the C2 headline persona — "administers access, cannot read the data" — was
    a state its holder could leave in one authorized call: judy POSTs herself ``owner`` and now holds
    ``can_read_data`` on data she was deliberately never given, leaving one audit row identical to a
    legitimate delegation. OpenFGA cannot express "grantee is not the caller" — there is no such
    construct — so this separation of duties belongs HERE, at the only layer that knows both. A
    caller who ALREADY holds the rung is unaffected (re-granting yourself what you have changes
    nothing), which is what keeps an owner's ordinary self-referential grant working.

    The userset form is covered too, and has to be: refusing only a literal ``user:<principal>`` would leave
    ``role:x#assignee`` as the same escalation one indirection away, for any userset the caller is
    already inside.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise PermissionDeniedError("malformed access request body")
    relation = body.get("relation")
    if not isinstance(relation, str) or not relation:
        raise PermissionDeniedError("malformed access request body: 'relation' must be a string")
    obj = _object(fga_type, segments, settings.delimiter)
    if revoking:
        if f"can_grant_{relation}" not in _grant_actions(fga_type):
            # Shape-checked against the GRANT actions on purpose: the set of rungs that may be taken
            # away is the set that may be handed out. Only the AUTHORITY differs.
            raise PermissionDeniedError(f"{relation!r} is not a grantable rung on {fga_type}")
        await _require(client, user=user, relation="can_revoke_grant", obj=obj)
        return
    action = f"can_grant_{relation}"
    if action not in _grant_actions(fga_type):
        raise PermissionDeniedError(f"{relation!r} is not a grantable rung on {fga_type}")
    await _require(client, user=user, relation=action, obj=obj)
    await _refuse_self_elevation(client, user=user, grantee=body.get("user"), relation=relation, obj=obj)


async def _refuse_self_elevation(client: OpenFgaClient, *, user: str, grantee: object, relation: str, obj: str) -> None:
    """Refuse a grant that would hand the CALLER a rung they do not already hold.

    Separation of duties, enforced at the only layer that can see both sides. See
    :func:`_authorize_grant` for why it cannot live in the model.

    Reads the grantee the same way :func:`~catalog.api.v1.endpoints.access._access_mutate` does
    (:func:`fga.grantee_object`) — because a guard that resolved it
    DIFFERENTLY from the code that writes the tuple would be guarding a grant nobody makes.

    Order matters: this runs AFTER the ``can_grant_*`` check, so a caller who may not grant the rung
    at all is refused for that reason and never learns whether the self-grant rule would also have
    caught them.
    """
    if not isinstance(grantee, str) or not grantee:
        return  # not a grant this guard can reason about; the endpoint's own validation owns the shape
    subject = fga.user_object(user)
    resolved = fga.grantee_object(grantee)
    if resolved != subject:
        if "#" not in resolved:
            return  # a plain grant to somebody else
        # A USERSET. `role:validators#assignee` names everyone holding `assignee` on that role, so a
        # caller already inside it is granting themselves by indirection. One check, and only on the
        # userset path — the common case costs nothing.
        userset_object, _, userset_relation = resolved.partition("#")
        try:
            inside = await fga.check(client, user=subject, relation=userset_relation, obj=userset_object, qualify=False)
        except ServiceUnavailableError:  # authz layer down mid-decision — fail closed, like every other door
            audit("can_grant_self", FAILURE, subject=user, resource=obj, reason="authz_unavailable")
            raise
        if not inside:
            return
    # The caller is the grantee. Allowed only when it changes nothing they do not already have.
    try:
        already = await fga.check(client, user=subject, relation=relation, obj=obj, qualify=False)
    except ServiceUnavailableError:
        audit("can_grant_self", FAILURE, subject=user, resource=obj, reason="authz_unavailable")
        raise
    if already:
        return
    audit("can_grant_self", DENY, subject=user, resource=obj, grantee=resolved, relation=relation)
    log.info("self_elevation_refused", extra={"sub": user, "relation": relation, "object": obj, "grantee": resolved})
    raise PermissionDeniedError(f"a grant may not raise your own access: you do not hold {relation} on {obj}")


def require_wired(client: OpenFgaClient | None) -> OpenFgaClient:
    """Fail CLOSED on an authz layer that is enabled but unwired — the 503 half of the estate gate.

    One body for one message (catalog-api-09). Eleven handlers carried their own copy of
    ``if client is None: raise ServiceUnavailableError(...)``, which is how two of them ended up
    disagreeing about whether an absent client is 503 or 409.
    """
    if client is None:
        raise ServiceUnavailableError("authorization service is not available")
    return client


def require_fga(settings: Settings, client: OpenFgaClient | None, *, feature: str) -> OpenFgaClient:
    """The estate gate every FGA-BACKED door opens with, and the client it needs, in one call.

    Two refusals, and the distinction is deliberate: with FGA OFF the deployment genuinely does not
    implement the feature (406 ``UnsupportedOperation`` — there are no tuples to review, simulate or
    grant), while ENABLED-but-unwired is a fault the caller should retry against (503, fail-closed —
    never a silent allow, never an empty grant list that reads as "nobody has access").

    ``feature`` names what is unavailable, so the 406 still says which door refused.

    Doors that answer an FGA-off stack with DATA rather than a refusal (``_read_managed_access``
    reports ``managed_access=False``; ``list_members`` reports an empty list) do not use this — for
    them "off" is an answer, not a refusal.
    """
    if not settings.fga_enabled:
        raise UnsupportedOperationError(f"{feature} requires OpenFGA (this stack runs auth-off)")
    return require_wired(client)


async def _authorize_batch(request: Request, client: OpenFgaClient, settings: Settings, *, user: str) -> None:
    """Authorize body-keyed batch routes (no ``{id}`` path param); tables named in the body.

    version/commit/delete on an existing table -> ``can_write_data`` on the table; a
    ``declare_table`` operation is create-on-parent -> ``can_create_table`` on the parent
    namespace. Two batch_checks (different relations/types). ``request.json()`` is cached by
    Starlette, so the downstream endpoint re-parses the same body.

    Fails CLOSED on malformed input: a non-dict body or a non-list ``entries``/``operations``
    (client-controlled JSON, validated here BEFORE Pydantic since authorize runs first) is a
    403 deny, never a 500 crash or a silently-skipped check.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise PermissionDeniedError("malformed batch request body")
    entries = body.get("entries")
    operations = body.get("operations")
    if entries is not None and not isinstance(entries, list):
        raise PermissionDeniedError("malformed batch request body: 'entries' must be a list")
    if operations is not None and not isinstance(operations, list):
        raise PermissionDeniedError("malformed batch request body: 'operations' must be a list")
    delimiter = settings.delimiter
    table_objs: set[str] = set()
    parent_objs: set[str] = set()
    owner_checks: list[tuple[str, str]] = []  # (object, owner-tier can_* relation)

    for entry in entries or []:
        segments = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(segments, list):
            table_objs.add(_object("table", segments, delimiter))

    for op in operations or []:
        if not isinstance(op, dict):
            continue
        for kind, sub in op.items():
            if not isinstance(sub, dict):
                continue
            segments = sub.get("id")
            if not isinstance(segments, list):
                continue
            if kind == "declare_table":  # creating a table -> can_create_table on its parent
                check = _create_parent_check("table", segments, settings)
                if check is not None:
                    parent_objs.add(check[0])
            elif kind in _BATCH_OWNER_OPS:  # destructive -> owner tier (no writer back-door)
                owner_checks.append((_object("table", segments, delimiter), _BATCH_OWNER_OPS[kind]))
            else:  # version/commit/append on an existing table -> can_write_data on it
                table_objs.add(_object("table", segments, delimiter))

    denied: list[str] = []
    if table_objs:
        objs = sorted(table_objs)
        allowed = await fga.batch_check(client, user=user, relation="can_write_data", objects=objs)
        for obj in objs:  # #41 audit every batch authz decision (allow AND deny), like the single-route gate
            audit("can_write_data", ALLOW if allowed.get(obj) else DENY, subject=user, resource=obj)
        denied += [o for o in objs if not allowed.get(o)]
    if parent_objs:
        objs = sorted(parent_objs)
        allowed = await fga.batch_check(client, user=user, relation="can_create_table", objects=objs)
        for obj in objs:
            audit("can_create_table", ALLOW if allowed.get(obj) else DENY, subject=user, resource=obj)
        denied += [o for o in objs if not allowed.get(o)]
    # ONE round trip per DISTINCT owner relation, not per object (catalog-api-12). Grouping keeps what
    # the per-object loop was written for — the relation stays exact per operation — while `operations`
    # is client-supplied and unbounded, so the loop cost N sequential authorization round trips before
    # any work was authorized. `_BATCH_OWNER_OPS` has one entry today; the loop was O(objects), not
    # O(relations), so "rare" described the mapping table rather than the request.
    for relation in sorted({r for _, r in owner_checks}):
        objs = sorted({obj for obj, r in owner_checks if r == relation})
        allowed = await fga.batch_check(client, user=user, relation=relation, objects=objs)
        for obj in objs:
            audit(relation, ALLOW if allowed.get(obj) else DENY, subject=user, resource=obj)
        denied += [o for o in objs if not allowed.get(o)]
    if denied:
        log.info("access_denied", extra={"sub": user, "objects": sorted(denied)})
        raise PermissionDeniedError(f"permission denied on {', '.join(sorted(denied))}")


#: The rung each WRITING credential tier opens, one rung per tier and never either-of ([[LH-202]]).
#: `can_write_data` is a LOGICAL writer, whose policy reaches `data/` only; `can_vend_maintain` is the
#: maintenance identity's alone (ADR 0156 C4, [[LH-473]]), a PHYSICAL writer whose policy reaches the whole
#: prefix, `_versions/` and `_refs/` included, so it can finalize a version or move a tag around
#: `version/create`. An owner is a maintainer and keeps every maintainer door the catalog judges, and is not
#: vended this. The whole-prefix credential, scoped to one table for 900 s, is the owner's ruling of
#: 2026-09-08: the alternative measured before it was 207 of 285 rewrites a tick signed by the deployment's
#: ROOT key, because a refused vend falls back to the ambient credential.
VEND_TIER_RUNGS: Final[dict[str, str]] = {"write": "can_write_data", "maintain": "can_vend_maintain"}

#: The resources whose non-read doors write a table's data or its history, and so reach the tier judge.
_JUDGED_RESOURCES: Final = frozenset({"table", "materialized_view"})


async def _rename_destination(request: Request, segments: list[str]) -> tuple[str, ...]:
    """The table a rename lands in: ``new_namespace_id`` (else the source's namespace) plus ``new_table_name``.

    A body the door would refuse names no destination, so the source stands in; the door answers the 400.
    """
    body = await request.json()
    name = body.get("new_table_name") if isinstance(body, dict) else None
    if not isinstance(name, str) or not name:
        return tuple(segments)
    namespace = body.get("new_namespace_id")
    parent = namespace if isinstance(namespace, list) and all(isinstance(part, str) for part in namespace) else segments[:-1]
    return (*parent, name)


async def _write_door(request: Request, settings: Settings) -> tier_policy.Write | None:
    """The write this route is about to make, or ``None`` for a door the tier judge does not decide.

    A write door is every non-read door on a table or materialized view, owner-rung ones included, and every
    create of one (ADR 0160). The classification is the same one the router's own gate uses, so a door is
    judged by what its declared rung says it does. ``None`` leaves the request to the rest of ``authorize``:
    reads, the grant doors (authorized from the body), the root, and a door no map declares, which that path
    refuses and audits.
    """
    path: str = request.scope["path"]
    object_id = request.path_params.get("id")
    resource = _resource_for(path)
    if object_id is None or resource not in _JUDGED_RESOURCES:
        return None
    suffix = _suffix(path, resource, object_id)
    segments = parse_identifier(object_id, settings.delimiter)
    if not segments or suffix in _GRANT_SUFFIXES:
        return None
    obj = _object(_FGA_TYPE[resource], segments, settings.delimiter)
    run = run_context.from_headers(request.headers) if suffix in tier_policy.STAMPING_DOORS else None
    input_rungs = input_read_rungs(run, settings.delimiter)
    if suffix in _CREATE_ON_PARENT_SUFFIXES.get(resource, frozenset()):
        check = _create_parent_check(resource, segments, settings)
        rung = tier_policy.Rung(relation=check[1], obj=check[0]) if check is not None else None
        return tier_policy.Write(op=suffix, obj=obj, rung=rung, table=tuple(segments), run=run, input_rungs=input_rungs)
    if suffix == "credentials":
        # A vend is a read at its default tier and a WRITE at a writing one: the credential is how a
        # direct-to-storage writer writes, so it is judged on the rung its tier opens.
        writing = VEND_TIER_RUNGS.get(request.query_params.get("tier", "read"))
        return tier_policy.Write(op=suffix, obj=obj, rung=tier_policy.Rung(relation=writing, obj=obj), table=tuple(segments)) if writing else None
    try:
        relation = _action_relation(_FGA_TYPE[resource], suffix)
    except InternalError:
        return None
    if relation in _READ_RELATIONS:
        return None
    table = await _rename_destination(request, segments) if suffix == "rename" else tuple(segments)
    moved_from = tuple(segments) if suffix == "rename" else ()
    return tier_policy.Write(
        op=suffix, obj=obj, rung=tier_policy.Rung(relation=relation, obj=obj), table=table, run=run, input_rungs=input_rungs, moved_from=moved_from
    )


def input_read_rungs(run: run_context.RunContext | None, delimiter: str) -> tuple[tier_policy.Rung, ...]:
    """The ``can_read_data`` rung on each input ``run`` names."""
    return tuple(tier_policy.Rung(relation="can_read_data", obj=_object("table", list(entry.table), delimiter)) for entry in (run.inputs if run else ()))


def _rung_check(settings: Settings, token: Caller | None, injected: OpenFgaClient | None) -> tier_policy.RungCheck | None:
    """The tier judge's rung check, asked through :func:`_require`; ``None`` with FGA off, when every rung allows.

    Raises:
        ServiceUnavailableError: FGA is on and unwired.
        UnauthenticatedError: FGA is on and nobody is signed in.
    """
    if not settings.fga_enabled:
        return None
    client = require_wired(injected)
    if token is None:
        raise UnauthenticatedError("authentication required")
    user = token.subject

    async def check(rung: tier_policy.Rung) -> None:
        await _require(client, user=user, relation=rung.relation, obj=rung.obj)

    return check


async def _judge(request: Request, settings: Settings, token: Caller | None, injected: OpenFgaClient | None, write: tier_policy.Write) -> None:
    """Hand ``write`` to the tier judge, with the rung asked through :func:`_require` when FGA is on."""
    check = _rung_check(settings, token, injected)
    # Imported HERE rather than at module scope: `dependencies` imports this module for its guards.
    from catalog.api.dependencies import namespace_for_top_ns

    async def namespace(top_ns: str) -> LanceNamespace:
        return await namespace_for_top_ns(request, settings, top_ns)

    await tier_policy.judge(write, caller=token.subject if token is not None else None, check=check, namespace=namespace)


async def judge_staged_run(
    settings: Settings,
    token: Caller | None,
    injected: OpenFgaClient | None,
    *,
    ns: LanceNamespace,
    segments: Sequence[str],
    run: run_context.RunContext | None,
    adds_data: bool,
) -> None:
    """Judge the run a stock client's staged commit to table ``segments`` names (ADR 0157).

    Only a commit that ``adds_data`` must name a run on a derived tier, whose tier is then read through ``ns``, the
    connection the door resolved for the table; any named input is checked either way.

    Raises:
        InvalidInputError: The table is silver or gold and the commit names no run or no input.
        PermissionDeniedError: The caller lacks ``can_read_data`` on a named input.
    """
    check = _rung_check(settings, token, injected)
    tier = await tier_policy.tier_of(ns, segments[:-1]) if adds_data and len(segments) > 1 else None
    names = f"the staged manifest's transaction properties {run_context.RUN_ID_PROPERTY} and {run_context.INPUTS_PROPERTY}"
    await tier_policy.judge_run(tier, run, input_read_rungs(run, settings.delimiter), check=check, names=names)


async def spec_vend_tier(
    settings: Settings, token: Caller | None, injected: OpenFgaClient | None, ns: LanceNamespace, segments: Sequence[str], *, door: str
) -> Literal["read", "write"] | None:
    """The highest tier a spec door (``describe``, ``declare``) vends the caller on table ``segments`` (ADR 0156 C3).

    ``can_write_data`` opens the write tier, and that is a write, so the tier judge decides it and audits the
    tier it was taken under, as it does for the management vend door; ``ns`` is the connection the door already
    resolved for the table, which is the one its namespace path's tier is read through. ``can_read_data`` opens
    the read tier. Neither, no credential: the door still answers, because describing a table is a metadata
    read. Maintain is never vended here; it is the maintenance identity's (LH-473).

    The rungs are probed with ``fga.check``, which writes no DENY: a reader's describe asking whether it may also
    write is not an access attempt. The rung that decides the tier is audited ALLOW, as every authorization
    decision is; whether a credential is then minted is the door's ``vend_credentials`` record. With FGA off
    every rung allows, so the write tier is vended, as the management door vends it.

    Raises:
        ServiceUnavailableError: FGA is on and unwired, or OpenFGA failed: fail closed, as every gate here does.
        UnauthenticatedError: FGA is on and nobody is signed in.
    """
    obj = _object("table", list(segments), settings.delimiter)
    writing = VEND_TIER_RUNGS["write"]
    if settings.fga_enabled:
        client = require_wired(injected)
        if token is None:
            raise UnauthenticatedError("authentication required")
        if not await fga.check(client, user=token.subject, relation=writing, obj=obj):
            if await fga.check(client, user=token.subject, relation="can_read_data", obj=obj):
                audit("can_read_data", ALLOW, subject=token.subject, resource=obj)
                return "read"
            return None
        audit(writing, ALLOW, subject=token.subject, resource=obj)

    async def namespace(_top_ns: str) -> LanceNamespace:
        return ns

    write = tier_policy.Write(op=door, obj=obj, rung=tier_policy.Rung(relation=writing, obj=obj), table=tuple(segments))
    await tier_policy.judge(write, caller=token.subject if token is not None else None, check=None, namespace=namespace)
    return "write"


async def authorize(request: Request, settings: SettingsDep, token: CurrentToken, injected: FgaClientDep) -> None:
    """Enforce the caller's permission for this route: the tier judge decides every write door, with FGA on
    or off, and OpenFGA every other door (a no-op when FGA is off)."""
    if (write := await _write_door(request, settings)) is not None:
        await _judge(request, settings, token, injected, write)
        return
    if not settings.fga_enabled:
        return
    # Fail closed: enabled but unwired authz must never silently allow the request. The client is
    # INJECTED (`FgaClientDep`) rather than read off `app.state` here — one place knows where it lives.
    client = require_wired(injected)
    if token is None:
        raise UnauthenticatedError("authentication required")

    # The raw ASGI path (what routing actually matched) — NOT request.url.path, which re-parses the
    # decoded path as a URL string and truncates at a decoded '#'/'?' inside an id, so for an
    # exotically-named table the suffix would stop naming the route that matched.
    path: str = request.scope["path"]
    object_id = request.path_params.get("id")

    # Body-keyed batch routes have no {id} path param — authorize by reading the body.
    if object_id is None:
        if path.rstrip("/") in _BATCH_PATHS:
            await _authorize_batch(request, client, settings, user=token.subject)
        # Other id-less routes (collection lists, health) need only authentication.
        return

    resource = _resource_for(path)
    if resource is None:
        return  # not a guarded resource path — authentication already enforced
    suffix = _suffix(path, resource, object_id)
    # Canonicalize the id ONCE, the same way the endpoints + grant path do (parse_identifier),
    # so the check object and the seeded object agree byte-for-byte even for edge forms
    # (e.g. the delimiter-only root id). Passing the raw URL string here would diverge.
    segments = parse_identifier(object_id, settings.delimiter)

    # Create-on-parent: child does not exist yet → authorize the parent.
    if suffix in _CREATE_ON_PARENT_SUFFIXES.get(resource, frozenset()):
        check = _create_parent_check(resource, segments, settings)
        if check is not None:  # None => open top-level create (authn already enforced)
            await _require(client, user=token.subject, relation=check[1], obj=check[0])
        return

    # Transactions authorize parent-scoped (against their enclosing namespace), not on a per-txn object.
    if resource == "transaction":
        if suffix not in _TRANSACTION_SUFFIXES:
            _record_undeclared(path, "transaction", suffix, user=token.subject, obj=_object("transaction", segments, settings.delimiter))
            raise _undeclared_door("transaction", suffix)
        await _authorize_transaction(client, settings, segments, suffix, user=token.subject)
        return

    # THE ROOT NAMES NO OBJECT, and composing one is a 500 rather than a decision. lance-ns encodes
    # the root namespace as the delimiter alone, so the stock client's `list_namespaces(id=[])` issues
    # `GET /v1/namespace/%24/list` and `parse_identifier` returns ZERO segments — `_object` then builds
    # `namespace:` with an empty id, which OpenFGA refuses outright:
    #   ValidationException: [check] HTTP 400 invalid relation: invalid 'object' field format
    # and the exception leaves this guard as a 500 on a spec-legal request. No tuple could ever name
    # that object, so the check was never a permission question. Measured 2026-09-24 by driving the
    # STOCK `lance_namespace` client at a governed catalog: `ServiceUnavailableError: Internal Server
    # Error`.
    #
    # ONLY THE PER-ITEM READS RETURN. `list` and `table/list` filter every NAME they return through
    # `fga.list_objects` on `can_get_metadata` — the route opens and the ITEMS are checked, because a
    # 403 there breaks the breadcrumb a grantee needs to reach their own table. Every other action on the
    # root is refused: exempting a cascade drop of the root lets any signed-in caller remove every
    # default-root table. Authentication was enforced above.
    #
    # ANY OTHER RESOURCE WITH NO SEGMENTS IS MALFORMED, not a root: a table or transaction id has to
    # name something. That earns the spec's own typed 400 rather than an empty object or a silent
    # open, which is the estate's rule — raise a `lance_namespace` error and let
    # `install_problem_handlers` choose the status.
    if not segments:
        if resource != "namespace":
            raise InvalidInputError(f"{resource} id is empty: {object_id!r} names no {resource}")
        if suffix in _ROOT_READ_SUFFIXES:
            return
        raise PermissionDeniedError(f"the root namespace names no object, so {suffix!r} cannot be authorized on it")

    fga_type = _FGA_TYPE[resource]
    obj = _object(fga_type, segments, settings.delimiter)
    # Grant/revoke authorize on the RUNG BEING HANDED OUT, which lives in the body — so, like the
    # batch routes above, this one cannot be answered from the path alone.
    if suffix in _GRANT_SUFFIXES:
        await _authorize_grant(request, client, settings, user=token.subject, fga_type=fga_type, segments=segments, revoking=suffix == "access/revoke")
        return
    # A CLASSIFICATION IS A LABEL, NOT A DATASET ([[LH-055]]). Its router carries exactly three routes
    # and the two grant ones returned above, so only `access/my-permissions` reaches here. It resolves
    # HERE rather than in `_action_relation`, whose vocabulary names relations this type does not define,
    # and OpenFGA answers an undefined relation with a 400 that fails closed to 503 — an outage wearing a
    # permission error's clothes.
    if fga_type == "classification":
        if suffix != "access/my-permissions":
            _record_undeclared(path, fga_type, suffix, user=token.subject, obj=obj)
            raise _undeclared_door(fga_type, suffix)
        await _require(client, user=token.subject, relation="can_get_metadata", obj=obj)
        return
    try:
        relation = _action_relation(fga_type, suffix)
    except InternalError:
        _record_undeclared(path, fga_type, suffix, user=token.subject, obj=obj)
        raise
    if (alternative := _ALTERNATIVE_RUNGS.get((fga_type, suffix))) is not None:
        await _require_any(client, user=token.subject, doors=[(relation, obj), (alternative, obj)])
        return
    try:
        await _require(client, user=token.subject, relation=relation, obj=obj)
    except PermissionDeniedError:
        # A READ door refusing an object that is not there answers the spec's 404 instead, but only to
        # a caller who already holds the parent's read rung. Owner ruling 2026-09-11.
        await _absent_to_a_reader_of_the_parent(request, client, settings, token=token, fga_type=fga_type, denied=relation, segments=segments)
        raise


async def _absent_to_a_reader_of_the_parent(
    request: Request,
    client: OpenFgaClient,
    settings: Settings,
    *,
    token: Caller,
    fga_type: str,
    denied: str,
    segments: list[str],
) -> None:
    """Raise the spec's NOT-FOUND when a denied READ names an object that does not exist.

    Returns — never raises — in every other case, so the caller's `raise` re-throws the 403 unchanged.

    THE SPEC ASKS FOR 404 AND THIS DOOR ANSWERED 403 FOR EVERYTHING. `authorize` runs before any
    endpoint, so an object with no tuples is refused identically whether it is forbidden or absent.
    Measured on the deployed catalog 2026-09-11: an absent table under a namespace alice holds
    `can_get_metadata`, `can_create_table` AND `can_delete` on answered 403 code 15, so a generated
    client cannot tell "ask for access" from "you typed the wrong name".

    THREE CONDITIONS, and each one is load-bearing:

    * THE DENIED RUNG MUST BE A READ RUNG, and it is keyed on the relation `_action_relation` actually
      resolved — never re-derived from the suffix. Deriving it again from the trailing path segment
      classified `access/list` (owner-tier: it lists who holds what) as a read, because the segment is
      the word "list"; `tests/integration/test_authz.py` caught it. The rung the door resolved to is the
      only thing that knows what the door DOES. The estate's no-existence-oracle rule is deliberate on
      the destructive doors — `delete_warehouse`, `delete_project` and `_set_warehouse_status` collapse
      PermissionDenied into NotFound precisely so a delete cannot enumerate ids — and keying on the rung
      keeps every one of them out of this path by construction.
    * THE CALLER MUST HOLD THE PARENT'S READ RUNG. Someone who can read the parent can already LIST
      it, so "does this child exist" is not information the 403 was protecting. Without the parent
      rung the answer is today's 403 and no probe runs — so this cannot be used to walk the id space
      from outside the hierarchy, and the common denial pays no extra round trip.
    * ONLY AN ABSENCE CONVERTS. An object that EXISTS and is forbidden still answers 403. Converting
      that would tell an unauthorized caller which names are real — the oracle this estate refuses —
      and it is the property worth more than the feature.

    The request never reaches the endpoint either way: this raises, and its caller re-raises. Nothing
    is read, and no grant is implied by the 404.
    """
    if denied not in _READ_RELATIONS:
        return
    parent = segments[:-1]
    if not parent:
        return
    parent_obj = f"namespace:{fga.canonical_object_id(parent, delimiter=settings.delimiter)}"
    try:
        if not await fga.check(client, user=token.subject, relation="can_get_metadata", obj=parent_obj):
            return
    except ServiceUnavailableError:
        return  # the authz layer is down; the 403 the caller already has is the fail-closed answer

    # Imported HERE rather than at module scope: `dependencies` imports this module for its guards, so
    # a top-level import would close the cycle.
    from catalog.api.dependencies import get_namespace

    ns = await get_namespace(request, settings)
    probe, absent = ("table_exists", TableNotFoundError) if fga_type == "table" else ("namespace_exists", NamespaceNotFoundError)
    request_model = TableExistsRequest(id=segments) if fga_type == "table" else NamespaceExistsRequest(id=segments)
    try:
        await run_in_threadpool(native.call, ns, probe, request_model)
    except (TableNotFoundError, NamespaceNotFoundError) as exc:
        ident = fga.canonical_object_id(segments, delimiter=settings.delimiter)
        raise absent(f"{fga_type} {ident!r} does not exist") from exc
    except Exception:  # noqa: BLE001 — an unanswerable probe leaves the 403 exactly as it was
        return


# --------------------------------------------------------------------------- #
# Hierarchy enforcement — the estate's containment rule, checked BEFORE the write.
# --------------------------------------------------------------------------- #


def require_parent(resource: str, segments: list[str], *, delimiter: str) -> None:
    """Reject a create that would produce an object with no parent to belong to.

    THE RULE, which the authz model already states and the write path did not:
    ``project -> warehouse -> namespace (self-nesting) -> table``. `model.fga` declares
    ``table.parent: [namespace]`` and ``namespace.parent: [warehouse, namespace]``, so an object
    whose parent is absent is not "a table at the root" — it is a table the model has no way to
    describe.

    Until this existed, a single-segment table was created anyway: ``fga.parent_object`` returned
    ``None`` for it, the create proceeded, and the object got an owner grant and no containment edge.
    Nothing failed at write time and nothing was visibly wrong — the row simply sat outside every
    cascade, invisible to a warehouse-level or namespace-level grant, and impossible to reach by the
    hierarchy the UI navigates. Orphans are cheap to create and expensive to find, so the answer is a
    refusal at the door with a message that says which rung is missing.

    Raised as the spec's own ``InvalidInput`` (error code 13 → HTTP 400, RFC 9457 problem body via
    ``install_problem_handlers``), NOT a bespoke 422: the Lance Namespace error model has no 422, and
    every client SDK dispatches on the numeric ``code`` — a status outside the spec's mapping is one
    no generated client understands. The DETAIL does the teaching: it names the rule and the fix.

    Namespaces are deliberately NOT rejected here. A top-level namespace legitimately parents to the
    catalog's warehouse root, and until a warehouse can be NAMED in the identifier (see the module
    note below) rejecting one would refuse the only shape the API can currently express.
    """
    if resource != "table":
        return
    if fga.parent_namespace_id(segments, delimiter=delimiter) is not None:
        return
    ident = fga.canonical_object_id(segments, delimiter=delimiter)
    raise InvalidInputError(
        f"table '{ident}' has no namespace to belong to. A table must live inside a namespace "
        f"(project > warehouse > namespace > table), so its identifier needs at least one "
        f"namespace segment before the table name — e.g. '<namespace>{delimiter}{ident}'. "
        "Create the namespace first if it does not exist."
    )


async def require_parent_exists(ns: LanceNamespace, resource: str, segments: list[str], *, delimiter: str) -> None:
    """`require_parent`, plus the half it never did: the parent must actually EXIST (#118).

    `require_parent` counts SEGMENTS — it proves the identifier names a parent, not that the parent
    is real. So `POST /v1/table/ghostns$t9/create` wrote a real Lance dataset into a namespace that
    does not exist, seeded an owner grant, and left no `parent` edge: the cascade-invisible orphan
    the guard's own docstring exists to prevent. Only `register_table` refused, and only because the
    `dir` backend itself checks — an accident of the backend, not a guard.

    Costs one `namespace_exists` round trip per create. That is the price of the rule the Lifecycle
    states ("Creates are top-down: parent must EXIST"), and it is paid BEFORE the native write so a
    refusal leaves nothing half-made.

    Subsumes `require_parent` rather than sitting beside it, so a door cannot acquire half the rule:
    the segment check is the cheap half and runs first, before any round trip.

    A backend that does not implement `namespace_exists` (an optional op — the spec has 54 and every
    backend implements a subset) cannot be interrogated, and refusing every create against such a
    backend would break far more than it protects. That case falls through to the segment rule alone,
    deliberately and narrowly: `UnsupportedOperationError` only, never a transport failure.
    """
    require_parent(resource, segments, delimiter=delimiter)
    parent = segments[:-1]
    if not parent:
        return
    try:
        await run_in_threadpool(native.call, ns, "namespace_exists", NamespaceExistsRequest(id=parent))
    except UnsupportedOperationError:
        return
    except NamespaceNotFoundError as exc:
        ident = fga.canonical_object_id(segments, delimiter=delimiter)
        parent_id = fga.canonical_object_id(parent, delimiter=delimiter)
        raise NamespaceNotFoundError(
            f"cannot create '{ident}': its parent namespace '{parent_id}' does not exist. "
            f"Create it first (POST /v1/namespace/{parent_id}/create, or the warehouse-scoped door "
            "for a top-level namespace) — a table whose namespace is absent is invisible to every "
            "cascade and to every parent-scoped grant."
        ) from exc


async def require_no_live_trash(settings: Settings, segments: list[str], *, kind: str = "table") -> None:
    """Refuse a create at an id whose PREVIOUS occupant is still in the trash (diff2 F10 item 4).

    THE BLEED. A recoverable drop deliberately KEEPS the object's FGA tuples — `drop_table` revokes
    only `if not trashed`, because the owner is the one person who needs them to undrop, and revoking
    made undrop unreachable for exactly that caller. Correct for undrop; a hole for create. Nothing
    else read the trash, so a create at the same id during the grace window produced a brand-new
    object silently wearing the DEAD table's grants — every reader, writer and validator the old table
    ever had, on data they have never been granted. That is the stale-grant privilege bleed
    `revoke_object_tuples` exists to prevent, arriving through the one door that had opted out of it.

    Refusing is the honest answer rather than revoking-then-creating: the id is not free yet. Its bytes
    are still on storage and its owner still holds a live claim to recover them, so a create that
    quietly took the name would also make that recovery impossible — trading a privilege bleed for
    silent data loss. The 409 names the deadline and both ways out, because "already exists" on a
    table the caller cannot see is otherwise unactionable.

    Checked BEFORE the native write, like `require_parent_exists`, so a refusal leaves nothing
    half-made. A read failure does NOT fail closed: an unreadable trash store must not make the whole
    create path unavailable, and the window this closes is narrow and time-boxed. Logged instead.
    """
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    try:
        record = await run_in_threadpool(trash.get, settings.registry_root, settings.storage_options(), canonical, kind=kind)
    except Exception as exc:  # noqa: BLE001 — an unreadable trash store must not 500 every create
        log.warning("trash_probe_failed", extra={"object": canonical, "kind": kind, "error": str(exc)})
        return
    if record is None:
        return
    expires = str(record.get("expires_at", "")) or "an unrecorded deadline"
    detail = (
        f"cannot create '{canonical}': a dropped {kind} of that name is still recoverable until {expires}, "
        f"and its grants are still live — creating here would hand the new {kind} the old one's readers and "
        f"writers. Undrop it (POST /v1/{kind}/{canonical}/undrop) to restore it, or purge it to free the name."
    )
    # The type-specific AlreadyExists error, not a generic conflict: both map to 409 (spec code 2), and
    # a caller's error handling switches on the CODE, so a table create must fail as a table conflict.
    raise (NamespaceAlreadyExistsError(detail) if kind == "namespace" else TableAlreadyExistsError(detail))


def require_namespace_depth(segments: list[str], *, delimiter: str) -> None:
    """Refuse a namespace nested deeper than the estate's ceiling — a SHAPE rule, so a 400.

    THE CEILING IS AN OPENFGA LIMIT, NOT A PREFERENCE. `warehouse#can_get_metadata` and
    `namespace#can_get_metadata` are recursive (`can_get_metadata from child`), and OpenFGA abandons a
    resolution needing too many rewrite rules. Measured against the shipped model with the real
    evaluator: at roughly 13 levels of nesting, `Check(user, can_get_metadata, warehouse:X)` stops
    answering true/false and returns `Code(2002) ... too many rewrite rules`.

    That is an ERROR, not a deny, and the fleet fails closed on an unrecognised authz error — so it
    surfaces as a 503 on the browse path. Worse, it is LATERAL: the positive check short-circuits, so
    a shallow readable table still resolves, while the NEGATIVE check for any subject on that
    warehouse errors. One pathological branch — created by a plain `writer`, or by a buggy importer —
    takes warehouse browsing down for every user of that bucket, its owners included.

    Nothing bounded creation before this. Both read walkers capped at `MAX_NAMESPACE_DEPTH` and the
    doors did not, so the estate could be driven into a state its own authz model cannot evaluate.

    The comparison is `>=`, IDENTICAL to both walkers'. It is a bound they share, and sharing the
    number while disagreeing about the operator would put the deepest legal namespace one level past
    where the walkers stop descending — its tables enumerated by nothing, so a cascade drop would
    silently leave them behind. That is the F10 item 10 failure with an off-by-one instead of a
    second literal.

    Refused at the SHAPE rung (`InvalidInputError` -> 400), before any native write, per the create
    door's order: identity -> shape -> parent exists -> authz -> conflict -> write. A depth violation
    is a malformed request, not a permission problem, and answering 403 would send the caller looking
    for a grant that would not help.
    """
    if len(segments) >= MAX_NAMESPACE_DEPTH:
        raise InvalidInputError(
            f"namespace {delimiter.join(segments)!r} nests {len(segments)} levels deep; the maximum is "
            f"{MAX_NAMESPACE_DEPTH - 1}. Deeper trees exceed what the authorization model can resolve, "
            f"which takes metadata reads down for the whole warehouse rather than denying just this one."
        )


def require_warehouse_scoped(segments: list[str], *, delimiter: str, warehouses_enabled: bool) -> None:
    """Refuse a TOP-LEVEL namespace created outside a warehouse.

    A namespace is logical separation INSIDE a tenant, so it has to live in one:
    ``project > warehouse > namespace > table``. The warehouse-scoped door
    (``POST /management/v1/warehouses/{warehouse_id}/namespaces``) binds the new namespace to a real bucket, and
    every table beneath it then lands in that tenant's storage — physically isolated from every other
    tenant's.

    This generic door could not. It created the namespace UNBOUND, and the resolver fell back to the
    shared default root: the namespace worked, tables under it worked, and the data quietly went to
    the wrong bucket — a multi-tenancy hole that no error ever reported. That fallback existed for
    backward compatibility, which is exactly what this refusal gives up on purpose.

    NESTED namespaces are untouched: their parent is the namespace above them, so they inherit that
    one's warehouse binding. Only the top-level rung has a warehouse to name.

    NO-OP WHEN WAREHOUSES ARE DISABLED, and that is not a loophole. With ``warehouses_enabled`` off
    there are no warehouses to belong to — the deployment is single-bucket by configuration, the
    default root is the only correct destination, and demanding a warehouse would be a rule no caller
    could satisfy. The rule binds exactly where it is meaningful: a multi-tenant deployment.
    """
    if not warehouses_enabled:
        return
    if len(segments) > 1:
        return
    ident = fga.canonical_object_id(segments, delimiter=delimiter)
    raise InvalidInputError(
        f"top-level namespace '{ident}' must belong to a warehouse. A namespace is logical "
        f"separation inside a tenant (project > warehouse > namespace > table), and an unbound "
        f"namespace resolves to the shared default bucket instead of the tenant's own. "
        f"Create it through its warehouse — POST /management/v1/warehouses/{{warehouse_id}}/namespaces with "
        f"name '{ident}' — or nest it under an existing namespace "
        f"('<parent>{delimiter}{ident}')."
    )


def require_not_protected(record: dict[str, str], *, kind: str, obj_id: str, force: bool) -> None:
    """Refuse to delete an object marked ``protected`` unless the caller passed ``force=true``.

    Deletion protection (`open_hierarchy_lifecycle.md` Decision 5): an opt-in flag on the registry
    record that makes an object refuse deletion. It is what makes ``cascade`` survivable — a
    fat-fingered cascade cannot take a protected object with it — and it is the ONE thing standing
    between a mistyped id and an irreversible purge.

    ``force`` overrides the PROTECTION ONLY. It is not an authz bypass: the FGA gate has already run
    (and runs identically with or without it), so a caller who may not delete this object cannot
    delete it by forcing. Two independent locks, and force turns exactly one.

    THE REFUSAL IS SHAPED LIKE THE OBJECT IT REFUSES, because a client acts on the code and not on the
    sentence. A protected TABLE raises ``InvalidTableStateError`` — `lance_docs/ns_catalog/spec.yaml:2431`
    defines code 19 as "Table is in an invalid state for the operation", which is exactly what protected
    is. Everything else raises ``NamespaceNotEmptyError`` (code 3), the spec's container-shaped "this
    will not be deleted right now".

    THE TABLE CASE USED TO RAISE CODE 3 TOO, and that is a live defect rather than a cosmetic one:
    ``NamespaceNotEmpty`` tells a generated client the container has contents, so the correct response
    to it is to empty the container and retry — against a table that was never full, and whose refusal
    no amount of emptying changes. The cascade path (`namespaces.py::_require_descendants_unprotected`)
    makes it worse, because there the namespace genuinely IS non-empty: the client's retry is
    well-founded, it drops every unprotected sibling, and it still cannot pass the one protected table.

    ``warehouse`` and ``project`` keep code 3 by SCOPE, not by oversight: they are rask's own hierarchy
    and appear nowhere in the lance-ns spec, so every code is an approximation there and the
    container-shaped one is the closest. The table is the rung the spec covers and the rung clients are
    generated against, so it is the one where a wrong code has a reader.
    """
    if force or (record.get("protected") or "false").lower() != "true":
        return
    refusal = InvalidTableStateError if kind == "table" else NamespaceNotEmptyError
    raise refusal(
        f"{kind} '{obj_id}' is protected against deletion. Pass force=true to override "
        f"(protection only — the same authorization is still required), or clear the flag first."
    )


class GovernedObject(BaseModel):
    """A table or namespace a door acts on: its name and the uuid every record kept for it is keyed by (ADR 0173 §8)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["table", "namespace"]
    segments: list[str]
    uuid: str

    def fga_object(self, delimiter: str) -> str:
        """The FGA object its grants are written on, which is still its name until LH-524."""
        return f"{self.kind}:{fga.canonical_object_id(self.segments, delimiter=delimiter)}"


async def identify(ns: LanceNamespace, settings: Settings, kind: Literal["table", "namespace"], segments: list[str]) -> GovernedObject:
    """The object ``segments`` names now, with its uuid.

    Raises:
        TableNotFoundError: No such table.
        NamespaceNotFoundError: No such namespace.
        InternalError: The object carries no uuid, which no door leaves.
    """
    if kind == "table":
        resolved = await run_in_threadpool(table_claims.resolve, ns, table_claims.claim_store(settings), segments)
        return GovernedObject(kind=kind, segments=segments, uuid=resolved.require_uuid())
    return GovernedObject(kind=kind, segments=segments, uuid=await run_in_threadpool(object_uuids.require_namespace_uuid, ns, segments))


async def require_table_unprotected(settings: Settings, table: ResolvedTable, *, force: bool) -> None:
    """Refuse a door that destroys some of ``table`` while its protection record stands, unless ``force``.

    The record is read under the uuid the door's name resolved to (ADR 0173 §8): a table that gave up the name, or
    one that took it since, holds a record under another key.

    Raises:
        TableNotFoundError: The name named no table.
        InvalidTableStateError: The table is protected.
    """
    if force:
        return
    guard = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "table", table.require_uuid())
    require_not_protected(guard or {}, kind="table", obj_id=fga.canonical_object_id(table.segments, delimiter=settings.delimiter), force=False)


async def require_namespace_unprotected(settings: Settings, namespace: GovernedObject, *, force: bool) -> None:
    """Refuse a door that removes ``namespace`` while its protection record stands, unless ``force``.

    Raises:
        NamespaceNotEmptyError: The namespace is protected.
    """
    if force:
        return
    guard = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "namespace", namespace.uuid)
    require_not_protected(guard or {}, kind="namespace", obj_id=fga.canonical_object_id(namespace.segments, delimiter=settings.delimiter), force=False)


async def require_project_exists(settings: Settings, project: str) -> None:
    """Refuse an operation naming a project the registry does not know.

    Existence is a LAYER-3 invariant (`open_hierarchy_lifecycle.md`): it comes from the project
    registry, not from FGA tuples and not from warehouse records implying a tenant. Checked before
    authz on purpose — the answer is the same for every caller, discloses nothing, and a 404 here
    tells an authorized admin the actual fix (create the project) instead of a misleading 403.

    Raises the same ``TableNotFoundError`` (spec code 4 → HTTP 404 problem body) the project detail
    route already answers for an unknown tenant, so one client error path covers both.
    """
    from catalog.services import projects as project_registry

    record = await run_in_threadpool(project_registry.get_project, settings.registry_root, settings.storage_options(), project)
    if record is None:
        raise TableNotFoundError(
            f"project not found: {project}. A warehouse must belong to an existing project "
            f"(project > warehouse > namespace > table) — create it first: POST /management/v1/projects "
            f'with {{"id": "{project}"}}.'
        )


def project_admin_for(token: Caller | None, named: str | None) -> str | None:
    """The subject seeded as admin of a project ``token`` creates: the person creating it, or the person
    a machine names. ``None`` only for an unauthenticated stack, where nothing is seeded.

    A MACHINE NEVER ADMINISTERS A TENANT (LH-433, the 2026-09-10 ruling ``seed_ownership`` applies to
    tables). A project has no parent to own it transitively, so a machine-created project would be left
    with no admin; the machine names the person instead (ADR 0143's provisioning operator knows the
    tenant's owner). The name must be a person: not a userset or wildcard, and not a service account's subject
    (``is_person_subject``). The caller's own type is judged by how it was proved, never by its name.
    """
    if not isinstance(token, ServicePrincipal):
        if named is not None:
            raise InvalidInputError("`admin` is named only by a machine creating a project; a person creating one is its admin")
        return token.subject if token is not None else None
    if named is None:
        raise InvalidInputError("a machine creating a project names the person who administers it in `admin`")
    if not is_person_subject(named):
        raise InvalidInputError(f"`admin` must name a person; {named!r} is not one")
    return named


async def seed_project_admin(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    project: str,
    admin: str | None,
) -> bool:
    """Grant ``admin`` (from :func:`project_admin_for`) on the new ``project:<id>`` — the tuple that makes
    a tenant self-sustaining (every later warehouse/namespace/table grant cascades from it). The caller
    is recorded as the actor.

    Written by ``POST /management/v1/projects`` in the same operation as the registry record, which is the whole
    point of Decision 1: existence and permission are minted together and cannot drift. Idempotent —
    ``write_tuples`` swallows duplicate writes on a create retry. Returns True when the tuple was
    actually written, so the caller audits a grant that really happened (never one an FGA-off stack
    silently skipped)."""
    if not (settings.fga_enabled and token is not None and client is not None and admin is not None):
        return False
    await fga.write_tuples(  # noqa: TID251
        client,
        [fga.ClientTuple(user=fga.user_object(admin), relation="admin", object=f"project:{project}")],
        actor=token.subject,
        origin="project_create",
    )
    return True


# --------------------------------------------------------------------------- #
# Post-create ownership seeding — the single write-side authz policy. Every create
# endpoint calls this instead of inlining the guard + grant_on_create + parent_object.
# --------------------------------------------------------------------------- #


async def seed_ownership(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    resource: str,
    segments: list[str],
    created: bool,
    parent_object: str | None = None,
    may_grant_owner: bool = True,
    carried: Sequence[fga.ClientTuple] = (),
    origin: fga.TupleOrigin = "create",
) -> None:
    """Grant the creator ``owner`` + the ``parent`` edge on a just-created object.

    ``created`` says THIS request brought the object into being, so any tuple already on its id belongs to a
    previous incarnation: a drop revokes after its irreversible native mutation, and an OpenFGA outage there
    leaves the tuples behind. They are revoked before the seed, or the new object would hand the dropped one's
    grantees its data with nobody having granted it (ADR 0146, [[LH-480]]). False when the door cannot prove
    the id was free: an object it kept or adopted, or a table written under ExistOk or Overwrite, whose
    tuples may be another caller's live grants. Required, not defaulted, so a new create door has to say
    which it is. The revoke is audited per tuple with origin ``stale_on_create``; it emits no
    ``grant_revoked``, as the drop that orphaned the tuples emits none either.

    ``may_grant_owner=False`` writes the EDGE ALONE. The caller uses it when this request did not
    create the object — an ExistOk that kept an existing table — where granting ``owner`` would seize
    another caller's table, but the structural edge is still required and is safe: it names where the
    table lives, it is idempotent, and it confers nothing by itself.

    No-op when FGA is off, the request is unauthenticated, or the client is unwired — so
    create endpoints call it unconditionally with one line. This is the ONE place the
    post-create seed policy lives (previously copy-pasted across four endpoint modules).
    Ids come from ``fga`` so this grant and the later :func:`authorize` check agree
    byte-for-byte.

    ``parent_object`` overrides the derived parent for the one door whose hierarchy is not
    positional: a warehouse-scoped namespace hangs off ``warehouse:<id>``, not off the shared
    catalog root its segments would imply. That door open-coded its own ``grant_on_create`` for
    exactly this reason, which also left it outside every compensation this module grew — so the
    override exists to pull it back in rather than to make the parent generally configurable.

    ``carried`` rides the same batch (:func:`move_ownership`), audited under ``origin``.
    """
    if not (settings.fga_enabled and token is not None and client is not None):
        return
    if created:
        obj = f"{resource}:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
        stale = await fga.revoke_object_tuples(client, obj, actor=token.subject, origin="stale_on_create")  # noqa: TID251
        if stale:
            log.warning("fga_stale_tuples_revoked_on_create", extra={"object": obj, "removed": len(stale)})
    # A MACHINE DOES NOT OWN WHAT IT REGISTERS (owner ruling 2026-09-10, zero trust). A service principal
    # registering its own output would become that table's owner, keeping `can_drop`, `can_deregister`,
    # `can_restore` and `manage_grants` on every governed tier of every tenant it ever wrote to.
    #
    # THE TABLE IS NOT LEFT UNOWNED. `warehouse.owner: … or admin from project` →
    # `namespace.owner: … or owner from parent` → `table.owner: … or owner from parent`, so the
    # project's admin already owns it transitively — which is what makes this a withdrawal rather than
    # an orphaning, and is why the parent edge below is still written unconditionally. Pinned against
    # the real evaluator in `model.fga.yaml`, not argued from the model text.
    #
    # THE PRINCIPAL'S TYPE IS THE DISCRIMINATOR, never a name pattern: only the service-account
    # verifier builds a `ServicePrincipal`, so the question is answered by how the caller was proved,
    # not by what its subject is called — a name test drifts the moment a service is renamed.
    await fga.grant_on_create(  # noqa: TID251
        client,
        grant_owner=may_grant_owner and not isinstance(token, ServicePrincipal),
        user_sub=token.subject,
        resource=resource,
        obj_id=fga.canonical_object_id(segments, delimiter=settings.delimiter),
        actor=token.subject,
        origin=origin,
        parent_object=parent_object
        or fga.parent_object(resource, segments, delimiter=settings.delimiter, warehouse_object=settings.fga_default_warehouse_object),
        carried=carried,
    )


#: Object types whose tenant resolves through the binding registry (namespace -> warehouse -> project).
#: A `project:` object IS its own tenant and a `warehouse:` one carries a direct tuple, so neither needs
#: the registry; they are left out rather than guessed at.
_TENANT_RESOLVES_BY_NAMESPACE = frozenset({"namespace", "table", "materialized_view"})


async def refuse_a_cross_tenant_role_grant(client: OpenFgaClient, grantee: str, *, fga_type: str, segments: list[str], settings: Settings) -> None:
    """Refuse granting a role from one tenant a rung on another tenant's object.

    A role name is estate-global unless the model says otherwise, so `role:analyst#assignee` minted in
    one project could be granted on any other project's namespace and every layer below would honour
    it: the tuple is well-formed, the rung is real, and the grantor holds the owner rung on the object
    they are granting. Nothing downstream can tell the role came from somewhere else.

    FAIL-OPEN FOR AN UNSCOPED ROLE, DELIBERATELY. A role carrying no `project` edge is estate-wide and
    keeps exactly the reach it has today, so this refuses something that was previously possible only
    where the model now carries the fact to refuse it on. The alternative — refusing every role whose
    tenant is unknown — would break every existing grant on the day the edge landed, which is how a
    security control gets reverted rather than adopted.

    Also fail-open when the OBJECT's tenant cannot be established: an unbound namespace has no tenant
    to disagree with, and inventing a refusal from a failed registry read would turn a registry blip
    into a governance outage on a path that already has an owner check in front of it.

    InvalidInput (13 -> 400), matching the bad-rung refusal above and lance-ns's own vocabulary: the
    grantor may well hold every rung this needs, so it is a malformed request rather than a denial.
    """
    if not grantee.startswith("role:") or fga_type not in _TENANT_RESOLVES_BY_NAMESPACE or not segments:
        return
    role_object = grantee.split("#", 1)[0]
    owned_by = {
        tup.user.removeprefix("project:")
        for tup in await fga.read_object_tuples(client, role_object)
        if tup.relation == "project" and tup.user.startswith("project:")
    }
    if not owned_by:
        return
    target = await run_in_threadpool(warehouses.project_for_namespace, settings.registry_root, settings.storage_options(), segments[0])
    if target is None or target in owned_by:
        return
    raise InvalidInputError(f"{grantee!r} belongs to project {min(owned_by)!r} and cannot be granted on {segments[0]!r}, which belongs to project {target!r}")


async def require_grants_may_follow(client: OpenFgaClient | None, settings: Settings, *, resource: str, source: list[str], destination: list[str]) -> None:
    """Refuse a rename whose carried grants the grant door would refuse on ``destination`` ([[LH-215]]).

    :func:`move_ownership` writes every grant held on ``source`` onto ``destination``, which is a grant on the
    destination, so each is held to the grant door's own rule (:func:`refuse_a_cross_tenant_role_grant`): a role one
    tenant owns is not granted a rung on another tenant's object by renaming the object across. Asked BEFORE the native
    rename, so a refusal leaves the table where it was. A destination under the same top-level namespace has the
    source's tenant, and nothing is read.
    """
    if not (settings.fga_enabled and client is not None) or source[:1] == destination[:1]:
        return
    for held in await fga.read_object_tuples(client, f"{resource}:{fga.canonical_object_id(source, delimiter=settings.delimiter)}"):
        if held.user and held.relation != "parent":
            await refuse_a_cross_tenant_role_grant(client, held.user, fga_type=resource, segments=destination, settings=settings)


async def move_ownership(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    resource: str,
    source: list[str],
    destination: list[str],
) -> None:
    """Carry the grants a renamed object held under ``source`` to ``destination``, then revoke ``source`` ([[LH-215]]).

    A lance-ns id is the object's name and Lance keeps no table UUID, so every grant is keyed by the name and a rename
    must move them: revoking the old name's grants without carrying them drops every reader, writer and owner someone
    granted, silently. Each grant held directly on ``source`` is written on ``destination`` in the SAME batch as the
    renamer's owner grant and the new parent edge (:func:`seed_ownership`), so the destination is seeded with all of
    them or none. The old ``parent`` edge is not carried: the new one names where the object lives now. What the object
    inherits changes with its parent, as it would for an object created there.

    The source is revoked AFTER the seed, and best-effort: the native rename has committed by now, so a failed revoke
    is logged rather than raised, leaving a grant on a name that holds no object (owner ruling 2026-08-15, diff2 F3).
    A create at that name revokes it before seeding ([[LH-480]]).
    """
    if not (settings.fga_enabled and token is not None and client is not None):
        return
    old = f"{resource}:{fga.canonical_object_id(source, delimiter=settings.delimiter)}"
    new = f"{resource}:{fga.canonical_object_id(destination, delimiter=settings.delimiter)}"
    held = await fga.read_object_tuples(client, old)
    carried = [fga.ClientTuple(user=t.user, relation=t.relation, object=new, condition=t.condition) for t in held if t.relation != "parent"]
    await seed_ownership(client, settings, token, resource=resource, segments=destination, created=True, carried=carried, origin="rename")
    try:
        await fga.revoke_object_tuples(client, old, actor=token.subject, origin="rename")  # noqa: TID251
    except Exception as revoke_exc:  # noqa: BLE001 — the rename has committed; a stale grant on a dead name is logged
        log.warning("rename_source_revoke_failed", extra={"source": old, "error": str(revoke_exc)})


async def revoke_ownership(
    client: OpenFgaClient | None,
    settings: Settings,
    *,
    resource: str,
    segments: list[str],
    token: Caller | None,
) -> None:
    """Delete every FGA tuple on a just-dropped / renamed-away object (the revoke counterpart of
    :func:`seed_ownership`).

    No-op when FGA is off or the client is unwired — so lifecycle endpoints call it with one line.
    Removes the owner grant, the ``parent`` edge, AND any later reader/writer/validator grants, so a
    reused id can never inherit a stale grant (privilege bleed). Needs no token: the mutation was already
    authorized by :func:`authorize` (owner tier for drop/deregister/drop_namespace; writer tier for a
    rename — where the revoke targets the now-DEAD source id, so the tier is immaterial to safety), and the
    cleanup deletes every subject's tuple regardless of who. Ids come from ``fga`` so the object matches
    what :func:`seed_ownership` wrote byte-for-byte.

    Revoke runs AFTER the (irreversible) native mutation, so on an OpenFGA outage it fails closed (503)
    with the tuples left stale until reconciled — the mutation itself already committed.
    """
    if not (settings.fga_enabled and client is not None):
        return
    obj = f"{resource}:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    # The actor is the caller who dropped the object. `system:catalog` is the honest fallback for an
    # auth-off stack, where there genuinely is no principal — never a stand-in for one we simply did
    # not thread through.
    actor = token.subject if token is not None else "system:catalog"
    removed = await fga.revoke_object_tuples(client, obj, actor=actor, origin="create")  # noqa: TID251
    if removed:
        log.info("fga_tuples_revoked", extra={"object": obj, "removed": len(removed)})


async def revoke_removed_object(client: OpenFgaClient | None, settings: Settings, obj: str, *, token: Caller | None) -> int:
    """Delete every tuple on ``obj``, an object a lifecycle delete removed, and return how many the server removed.

    Distinct from :func:`revoke_ownership` in two ways that matter to a cascade: the audit origin is
    ``lifecycle_delete`` (a deletion of the hierarchy, not one object's drop), and the count comes back for the door to
    report. 0 when FGA is off or unwired. An outage part-way raises :class:`fga.RevokeInterrupted` naming what went.
    """
    if not (settings.fga_enabled and client is not None):
        return 0
    actor = token.subject if token is not None else "system:catalog"
    removed = await fga.revoke_object_tuples(client, obj, actor=actor, origin="lifecycle_delete")  # noqa: TID251
    if removed:
        log.info("fga_tuples_revoked", extra={"object": obj, "removed": len(removed)})
    return len(removed)


async def end_removed_subtree(
    client: OpenFgaClient | None,
    settings: Settings,
    *,
    root: list[str],
    root_uuid: str | None,
    removed: Sequence[GovernedObject],
    token: Caller | None,
) -> AsyncIterator[int]:
    """End what namespace ``root`` and ``removed``, which a lifecycle delete removed, still hold, children first.

    Each object's protection and maintenance-policy records go, keyed by its uuid, then its grants. Yields each
    object's revoked count as its revoke lands, so a door reporting what it revoked counts every object revoked before a
    failure. Run ONLY ONCE EVERY OBJECT IN THE SUBTREE IS GONE ([[LH-495]]): a grant revoked before its object is
    removed strands the object, alive and with nobody left who may read, drop or recover it through a door.

    THE GRANTS GO LAST, BECAUSE THEY ARE WHAT FINDS AN OBJECT AGAIN. Every object created under a namespace leaves a
    ``child`` edge on it (``fga.hierarchy_edge_tuples``), and only that object's own revoke removes the edge. A
    re-issue lists only what still stands, so an edge still standing is what names an object a failure stopped before
    its end, and this walk revokes its grants then. Each edge is followed before the object holding it is revoked. An
    object found only by its edge is gone, and so is what recorded its uuid: its records stay, keyed by a uuid no
    other object holds, so they govern nothing. ``removed`` (the catalog's own listing) covers an object created before
    the edges existed; with FGA off nothing names an object once it is gone, and only the listed ones are ended. Every
    clear is idempotent, so a protection record is cleared without asking whether one exists: an object a forced
    attempt left unfinished may be re-issued without ``force``.

    AN OBJECT IN THE TRASH KEEPS ITS RECORDS AND GRANTS. A recoverable drop keeps them for its undrop (#75), and a
    trashed table is undropped into its namespace once that namespace is created again, so they go only when its
    trash record does: at the trash purge, or with its warehouse (``catalog.services.warehouses.trash_owned_by``).
    """
    so = settings.storage_options()
    seen: set[str] = set()
    uuids = {item.fga_object(settings.delimiter): item.uuid for item in removed}
    root_obj = f"namespace:{fga.canonical_object_id(root, delimiter=settings.delimiter)}"
    if root_uuid is not None:
        uuids[root_obj] = root_uuid

    async def trashed(obj: str) -> bool:
        kind, _, canonical = obj.partition(":")
        return await run_in_threadpool(trash.get, settings.registry_root, so, canonical, kind=kind) is not None

    async def end(obj: str) -> AsyncIterator[int]:
        seen.add(obj)
        if settings.fga_enabled and client is not None and obj.startswith("namespace:"):
            for held in await fga.read_object_tuples(client, obj):
                if held.relation == "child" and held.user and held.user not in seen and not await trashed(held.user):
                    async for count in end(held.user):
                        yield count
        if (uuid := uuids.get(obj)) is not None:
            await run_in_threadpool(object_records.end_object_records, settings.registry_root, so, obj.partition(":")[0], uuid)
        yield await revoke_removed_object(client, settings, obj, token=token)

    children_first = sorted(removed, key=lambda item: (item.kind == "namespace", -len(item.segments)))
    for obj in [*(item.fga_object(settings.delimiter) for item in children_first), root_obj]:
        if obj not in seen:
            async for count in end(obj):
                yield count


async def seed_ownership_or_compensate(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    resource: str,
    segments: list[str],
    undo: Callable[[], Awaitable[None]] | None,
    created: bool,
    parent_object: str | None = None,
    may_grant_owner: bool = True,
) -> None:
    """:func:`seed_ownership`, but UNDO the native create when the grant fails (diff2 F3).

    The dual-write hazard this closes: every create door runs the native mutation FIRST and seeds
    tuples second. If the seed fails — an OpenFGA blip is enough — the object exists on storage with
    no owner and no ``parent`` edge, so per-item list filtering hides it from every caller including
    its creator, and the obvious retry hits native ``AlreadyExists`` and never reaches the seed again.
    The object is then unreachable by anything except a hand-written tuple. ``undo`` runs the native
    delete of what THIS request just created, so the retry starts from a clean slate and converges.

    TWO ORDERING FACTS CARRY THIS, and the first is why the pattern moved here rather than being
    copy-pasted a ninth time:

    1. The revoke and the undo are INDEPENDENT best-effort steps, not one try block. They were one
       (`data.py`, the single door that had any compensation at all), and the revoke goes through
       ``read_object_tuples`` — an OpenFGA call. So in the exact scenario the compensation exists for,
       a down OpenFGA, the revoke raised and the native undo NEVER RAN. The compensation was inert
       precisely when it was needed, which is worse than absent because the code reads as covered.
       The undo needs no FGA, so nothing FGA-shaped may stand in front of it.
    2. The revoke is attempted FIRST anyway, when it can run. A grant can commit server-side while its
       response is lost, and a stale owner tuple on a freed id silently grants its holder the NEXT
       object created at that id — the reused-id privilege bleed :func:`revoke_ownership` exists for.

    ``undo=None`` means the caller has decided a native delete is unsafe here — an ExistOk that KEPT a
    pre-existing object, or an Overwrite that replaced one whose id still carries the prior
    incarnation's time-travel history. Dropping either escalates a transient blip into irreversible
    loss; stranded-but-admin-recoverable beats destroyed. Passing ``None`` is a decision, not a
    default: it leaves the stranded state reachable, and the structural repair is F3's second half.

    The original grant error is always re-raised — compensation changes what is left behind, never
    what the caller is told.
    """
    try:
        await seed_ownership(
            client, settings, token, resource=resource, segments=segments, created=created, parent_object=parent_object, may_grant_owner=may_grant_owner
        )
    except Exception:
        if undo is not None:
            await compensate_create(client, settings, token, resource=resource, segments=segments, undo=undo, reason="grant_failed")
        raise


async def compensate_create(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    resource: str,
    segments: list[str],
    undo: Callable[[], Awaitable[None]],
    reason: str,
) -> None:
    """Take back a create whose request is about to fail: revoke the ownership it seeded, then run ``undo``.

    The two steps are independent and best effort, revoke first, as :func:`seed_ownership_or_compensate` explains;
    nothing here raises, because the caller re-raises the error that made it compensate.
    """
    obj = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    try:
        await revoke_ownership(client, settings, resource=resource, segments=segments, token=token)
    except Exception as revoke_exc:
        # Expected when OpenFGA is the thing that is down. Logged, never fatal — the undo below
        # is what actually makes the retry converge, and it must not be skipped for this.
        log.warning("compensation_revoke_failed", extra={"object": obj, "resource": resource, "error": str(revoke_exc)})
    try:
        await undo()
        log.warning("create_compensated", extra={"object": obj, "resource": resource, "reason": reason})
    except Exception as undo_exc:
        # Both halves failed: the object is stranded and only the structural reconcile can
        # reach it. Loud on purpose — this is the state an operator has to be told about.
        log.error("create_compensation_failed", extra={"object": obj, "resource": resource, "error": str(undo_exc)})


async def require_can_drop_table(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    segments: list[str],
) -> None:
    """Raise 403 unless the caller holds owner-tier ``can_drop`` on the table at ``segments``.

    The gate for a destructive create ``mode=Overwrite`` against an EXISTING table: Overwrite is spec-defined
    as drop-then-recreate (spec.yaml CreateTableRequest.mode), and it replaces the tip's schema and rows, so it
    must clear the same owner-tier bar a real ``drop_table`` does — NOT the writer-tier ``can_create_table`` on
    the parent that ``authorize`` already applied for a create. Without this, a mere namespace writer could
    replace another user's table. No-op when FGA is off / unwired / unauthenticated (the create path enforces
    those elsewhere)."""
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    obj = f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    await _require(client, user=token.subject, relation="can_drop", obj=obj)


#: The rung a staged commit's operation needs at ``version/create`` beyond the router's ``can_write_data`` ([[LH-471]]).
#: Each is the rung the catalog's own door for that operation asks, so staging an operation never clears a lower bar
#: than doing it through the door: restore is ``restore``'s owner rung, a compaction's two commits (the ``ReserveFragments``
#: and the ``Rewrite`` after it) are the maintainer rung, and an Overwrite of a table that already has a version is
#: drop-then-recreate (``require_can_drop_table``). The writer operations need nothing more than the router's rung.
_STAGED_OPERATION_RELATION: Final[dict[str, str | None]] = {
    **dict.fromkeys(("Append", "Delete", "Update", "Merge", "Project", "CreateIndex", "DataReplacement", "DataOverlay", "UpdateConfig")),
    "Restore": "can_restore",
    "Rewrite": "can_maintain",
    "ReserveFragments": "can_maintain",
    "Overwrite": "can_drop",
}


async def require_staged_operation(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    segments: list[str],
    location: str,
    operation: str,
    creates: bool,
) -> None:
    """Refuse a staged commit whose operation needs a rung the caller lacks, or that no door commits this way.

    ``creates`` is true for the table's first version, which the declare that reserved it already authorized. An
    Overwrite of a table with a version is drop-then-recreate, so the protection record refuses it as it refuses a
    drop, with FGA on or off; ``force`` has no channel on the spec door. The rung check is a no-op when FGA is off / unwired / unauthenticated, as the other ``require_*`` gates are; an
    operation outside the map (``Clone`` among them, which the branch and clone doors own) is refused whatever FGA
    says.

    Raises:
        InvalidInputError: ``operation`` is not one a stock commit may stage here.
    """
    if operation not in _STAGED_OPERATION_RELATION:
        raise InvalidInputError(f"a {operation} cannot be committed through CreateTableVersion")
    relation = _STAGED_OPERATION_RELATION[operation]
    if relation is None or (operation == "Overwrite" and creates):
        return
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    if operation == "Overwrite":
        table_uuid = await run_in_threadpool(table_claims.uuid_at, table_claims.claim_store(settings), location, segments)
        guard = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "table", table_uuid)
        require_not_protected(guard or {}, kind="table", obj_id=canonical, force=False)
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    await _require(client, user=token.subject, relation=relation, obj=f"table:{canonical}")


async def require_can_promote(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    segments: list[str],
) -> None:
    """Raise 403 unless the caller holds the VALIDATOR-rung ``can_promote`` on the table at ``segments``.

    The gate for #17 model promotion (candidate→blessed): moving the ``blessed`` tag on ``models$<model>`` is
    a promotion, which the FGA model separates from a plain WRITER — ``can_promote`` reduces to ``validator``
    (a writer is NOT a validator), exactly as a tier promotion is gated. The ``/management/v1/model/*``
    routes sit under the router-level ``authorize`` (its 401/503 pre-path checks still apply) but match no
    ``_RESOURCES`` prefix, so ``authorize`` makes no per-object decision there — the
    promote endpoint calls this EXPLICITLY. No FGA-model change is needed: ``table.can_promote: validator``
    already exists and the per-model ``namespace:models → table:models$<model>`` parent link makes a
    ``validator namespace:models`` grant cascade. Fail-closed: 403 on deny, 503 on an OpenFGA
    outage (via ``_require``/``fga.check``). No-op when FGA is off / unwired / unauthenticated."""
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    obj = f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    await _require(client, user=token.subject, relation="can_promote", obj=obj)


async def require_can_get_metadata(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    segments: list[str],
) -> None:
    """Raise 403 unless the caller holds reader-tier ``can_get_metadata`` on the table at ``segments``.

    The read gate for routes whose path matches no ``_RESOURCES`` prefix (so the router-level ``authorize``
    contributes only its 401/503 pre-path checks, no per-object decision) — notably the #17 model describe
    route (``/management/v1/model/*``): reading a model's blessed pointer + metrics is a metadata read
    (reader rung), so a reader may see it while only a validator may promote. Fail-closed like every other
    gate. No-op when FGA is off / unwired / unauthenticated."""
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    obj = f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    await _require(client, user=token.subject, relation="can_get_metadata", obj=obj)


async def require_create_on_parent(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    resource: str,
    segments: list[str],
) -> None:
    """Authorize a create-on-parent op for ``segments`` — check ``can_create_table``/``can_create_namespace``
    on the child's PARENT namespace (the same gate ``authorize`` applies to create/declare/register). Used by
    endpoints that mint a NEW child id outside the create routes — notably ``rename`` into a destination
    namespace: without it, a source-table owner could relocate (plant) their table into a namespace they have
    no create rights on. No-op when FGA is off / unwired / unauthenticated."""
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    check = _create_parent_check(resource, segments, settings)
    if check is not None:  # None => open top-level create (authn already enforced)
        await _require(client, user=token.subject, relation=check[1], obj=check[0])


async def require_relation(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    relation: str,
    obj: str,
) -> None:
    """Gate on one ``relation`` over an explicit FGA object id, through the audited ``_require`` core.

    For handlers whose object is not table/namespace-shaped (e.g. the #3-A ``warehouse:<id>`` control
    plane) — funneling here instead of a bare ``fga.check`` keeps every deny/allow/outage on the #41 audit
    trail. No-op when FGA is off / unwired / unauthenticated (parity with the other ``require_*`` deps)."""
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    await _require(client, user=token.subject, relation=relation, obj=obj)


async def require_tier_authority(client: OpenFgaClient | None, settings: Settings, token: Caller | None, *, project: str | None) -> None:
    """Raise 403 unless the caller may declare or drop a namespace's ``rask.tier`` (ADR 0160).

    That is the namespace's project admin (``can_administer`` on ``project:<project>``), or the estate admin
    (``can_observe_events`` on the root object, the bar ``POST /management/v1/projects`` asks) where no project owns
    the namespace. No-op when FGA is off / unwired / unauthenticated, like every gate here.
    """
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    if project is not None:
        await _require(client, user=token.subject, relation="can_administer", obj=f"project:{project}")
    else:
        await _require(client, user=token.subject, relation="can_observe_events", obj=settings.fga_root_object)


async def require_can_create_warehouse(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    project: str,
) -> None:
    """Gate the #3-A admin control-plane warehouse-create on ``project#can_create_warehouse``.

    The model's highest-privilege action (``project.can_create_warehouse = admin``,
    ``service_kit/governed/auth/model.fga``): provisioning a physical bucket is a project-admin
    operation, not the writer-tier create-on-parent that guards tables/namespaces. Fail-closed like
    every other gate: 403 on deny, 503 on an OpenFGA outage. No-op when FGA is off / unwired /
    unauthenticated (those postures are enforced at the boot/authn layer).

    There is deliberately NO bootstrap exception any more. It existed because a not-yet-existing
    project had no tuples, so its first warehouse was uncreatable — the estate admin got a side door
    (``can_observe_events`` on the root). Decision 1 replaced the implicit mint: the estate admin
    creates the PROJECT explicitly (``POST /management/v1/projects``, which seeds its admin), and
    ``require_project_exists`` refuses a warehouse for an unknown tenant BEFORE this gate runs — so
    by the time we are here, the project exists and has admins, and one door is enough."""
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    await _require(client, user=token.subject, relation="can_create_warehouse", obj=f"project:{project}")


async def seed_warehouse(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    warehouse_id: str,
    project: str,
) -> None:
    """Grant the creator ``owner`` on the new ``warehouse:<id>`` and link it to its ``project:<project>``
    parent, so the concentric cascade (project → warehouse → namespace → table) reaches everything created
    under it. No-op when FGA is off / unauthenticated / unwired.

    NOTE: the warehouse's parent-pointer relation is ``project`` (``define project: [project]`` in
    ``service_kit/governed/auth/model.fga``) — NOT the ``parent`` name that namespaces/tables use. So this writes
    the two tuples directly rather than via :func:`fga.grant_on_create` (whose parent edge is hardcoded to
    ``parent``, a relation the ``warehouse`` type does not define — writing it makes OpenFGA reject the whole
    seed → 503). Idempotent: ``write_tuples`` swallows duplicate-tuple writes on a create retry.

    The project-admin bootstrap tuple this used to optionally carry moved to ``seed_project_admin``,
    written by ``POST /management/v1/projects`` — a tenant's admin is minted with the tenant, not with its
    first warehouse."""
    if not (settings.fga_enabled and token is not None and client is not None):
        return
    obj = f"warehouse:{warehouse_id}"
    # The project edge and the sweep's grant, granted here and not per tier, at the WAREHOUSE because
    # `namespace` and `table` both define `... or ... from parent`: one tuple reaches every tier and every
    # table under it. Written in the SAME call as the creator's grant so there is no window in which the
    # warehouse exists and is unreachable from its project or unmaintained.
    await fga.write_tuples(  # noqa: TID251
        client,
        [
            fga.ClientTuple(user=fga.user_object(token.subject), relation="owner", object=obj),
            *cascade_tuples(settings, warehouse_id=warehouse_id, project=project),
        ],
        actor=token.subject,
        origin="warehouse_create",
    )


def cascade_tuples(settings: Settings, *, warehouse_id: str, project: str) -> list[fga.ClientTuple]:
    """The tuples a warehouse needs to be REACHABLE and MAINTAINED — its project edge, plus the sweep's grant.

    Shared by the create path and the backfill so the two cannot drift: a backfill that writes a different set
    from the one creates write leaves the estate with two populations of warehouse that differ in a way
    nothing reports.

    Granted at the WAREHOUSE, not per tier: `namespace` and `table` both define `maintainer` as
    `... or maintainer from parent`, so one tuple at the container reaches every tier and every table
    under it (`optimize-tuples.md`), and the sweep's reach stays enumerable.

    The creator's own grant is deliberately NOT here. It is the one tuple a backfill must never write
    — re-running the create path over an existing estate to repair it would make whoever ran the
    repair an owner of every tenant.
    """
    obj = f"warehouse:{warehouse_id}"
    return [
        fga.ClientTuple(user=f"project:{project}", relation="project", object=obj),
        # `maintenance` ALONE: the platform's maintenance identity, a maintainer that may also be vended the
        # maintain tier. Maintenance rewrites how a dataset is stored and must not be able to drop it or change
        # what it says, which is exactly the separation `can_maintain` was minted to express.
        *(fga.ClientTuple(user=fga.user_object(subject), relation="maintenance", object=obj) for subject in settings.fga_maintainers),
    ]


async def backfill_cascade_grants(
    client: OpenFgaClient | None,
    settings: Settings,
    *,
    warehouse_id: str,
    project: str,
    actor: str,
) -> int:
    """Repair one EXISTING warehouse's grants: assert :func:`cascade_tuples`. Returns the number of tuples written.

    Why this is needed at all, given creates already seed: the grants are written ONCE, at create, from
    a config value that CHANGES (``LANCE_FGA_MAINTAINERS``), so every warehouse that predates a change is
    missing the new subject.

    Idempotent by the same mechanism the create path relies on: ``write_tuples`` retries one-by-one
    when OpenFGA rejects a batch for containing a duplicate, so a re-run lands only what is missing.
    It inherits fail-closed too — an unreachable OpenFGA raises rather than reporting success, because
    a backfill that silently wrote nothing is indistinguishable from one that had nothing to do.
    """
    if not (settings.fga_enabled and client is not None):
        return 0
    tuples = cascade_tuples(settings, warehouse_id=warehouse_id, project=project)
    await fga.write_tuples(client, tuples, actor=actor, origin="cascade_backfill")  # noqa: TID251
    return len(tuples)
