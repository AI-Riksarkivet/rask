"""OpenFGA authorization dependency.

Router-level guard: when FGA is enabled it maps the request (resource + operation) to a
``can_*`` ACTION relation defined in the model (``service_kit/governed/auth/model.fga``) and checks the
authenticated caller against OpenFGA. A no-op when FGA is disabled.

The op -> ``can_*`` mapping below is the ONLY policy logic in the app; the privilege math
(which rung each ``can_*`` reduces to, and how owner/writer/reader cascade through
``parent``) lives in the model — so it is testable in ``model.fga.yaml``. Mapping:

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

A table or namespace is asked about as the object its grants are held by, ``table:<uuid>`` or ``namespace:<uuid>``
(ADR 0173 §8, [[LH-524]]): every door is addressed by name, and the name is resolved once per request
(``catalog.api.dependencies.Resolver``), so the door acts on the object it authorized.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from functools import lru_cache, partial
from typing import Annotated, Any, Final, Literal

from fastapi import Query, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    InternalError,
    InvalidInputError,
    InvalidTableStateError,
    LanceNamespace,
    NamespaceExistsRequest,
    NamespaceNotEmptyError,
    NamespaceNotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableNotFoundError,
    UnauthenticatedError,
    UnsupportedOperationError,
)
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict

from catalog.api.dependencies import FgaClientDep, Resolver, ResolverDep, SettingsDep
from catalog.api.security import Caller, CurrentToken
from catalog.core import run_context, tier_policy
from catalog.core.config import Settings
from catalog.core.identifiers import MAX_NAMESPACE_DEPTH, parse_identifier, rename_destination, require_names, transaction_table
from catalog.services import dataplane, native, object_uuids, table_claims, warehouses
from catalog.services.table_claims import ResolvedTable
from service_kit.governed import fga
from service_kit.governed.audit import AuditAction, AuditOutcome, audit
from service_kit.governed.machine_identity import ServicePrincipal
from service_kit.lakehouse import object_records, protection, trash
from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lakehouse.subjects import is_person_subject


log = logging.getLogger(__name__)

# Guarded resource prefixes, in match order. Each but ``transaction`` maps to its own dedicated FGA type in the model
# (``service_kit/governed/auth/model.fga``). A transaction is no object of its own: its id names the table it belongs
# to, and both its doors are authorized on that table (``_authorize_transaction``, [[LH-221]]).
#: `classification` is the odd one and belongs here anyway: it is a governance VOCABULARY rather than a
#: data object — no rows, no parent warehouse, and the path segment IS the label ([[LH-055]]). It
#: reaches this map because its grant/revoke doors must be authorized like every other, through
#: `_authorize_grant` reading the rung out of the body.
_RESOURCES: tuple[str, ...] = ("namespace", "table", "materialized_view", "transaction", "classification")
_FGA_TYPE: dict[str, str] = {
    "namespace": "namespace",
    "table": "table",
    "materialized_view": "materialized_view",
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
# ``explain_plan`` and ``analyze_plan`` read data too: analyze RUNS the query and reports its output row count, so a
# filter it is handed answers a yes/no about the values and a binary search over them recovers any value; explain
# names the columns, filters and plan a reader would run. Both take the rung their query would.
_DATA_READ_ACTIONS = frozenset({"query", "count_rows", "credentials", "blobs", "changes", "explain_plan", "analyze_plan"})
# `tasks` (#75) reports what is SCHEDULED against this object — a pending undrop deadline today.
# Reader tier: it discloses no data and no principals, and an owner must be able to see the
# deadline they are racing.
# `history` (GET /v1/table/{id}/history) is the commit log — WHAT changed and WHEN, never who. Its own
# docstring already argued the rung ("the same rung as describe/list-versions") and the endpoint checks
# `can_get_metadata` itself; what was missing is this line, so the ROUTER demanded `can_write_data`
# first and that weaker second check was unreachable for anyone who had not already cleared the higher
# bar. Measured on the estate 2026-09-09: one GET logged `can_write_data ALLOW` then
# `can_get_metadata ALLOW`, in that order.
_META_READ_ACTIONS = frozenset({"describe", "exists", "list", "stats", "version", "tasks", "history"})

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
# ``rename`` is owner-tier: it RETIRES the name every reader and pipeline addresses the table by, and moves the
# table (and, into another namespace, where it inherits from), so it is the owner's act on the source. Its grants
# stay on its uuid (ADR 0173 §8).
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
        # DELETING a branch ends its own version sequence and makes the files only it references reclaimable: history
        # reclaim, which protection does not guard (ADR 0150), so this rung is the door's whole gate. Absent from this
        # map it fell through to the
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
    "table": {
        **dict.fromkeys(
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
            ),
            "can_write_data",
        ),
        # A writer's or a maintainer's: the staged operation then asks its own rung (`require_staged_operation`), so a
        # compaction job commits as a maintainer and holds no writer rung.
        "version/create": "can_create_version",
    },
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


def _object(fga_type: str, id_segments: list[str] | str) -> str:
    """``<type>:<canonical-id>`` for an object addressed by its id.

    The FGA object of a type no name is minted a uuid for (a transaction's opaque id, a classification label, a
    materialized view), and the NAME of a table or namespace in an audit record or a refusal. A table's or namespace's
    grants are held by its uuid (:func:`_named`), never by this.
    """
    return f"{fga_type}:{fga.canonical_object_id(id_segments)}"


#: The types whose objects are keyed by the uuid the catalog mints (ADR 0173 §8, [[LH-524]]).
_UUID_KEYED: Final = frozenset({"table", "namespace"})


#: The doors that act on an object the trash may hold under its name, which are addressed by that name (ADR 0173 §9).
_TRASH_DOORS: Final = frozenset({"undrop", "tasks"})


def _named(fga_type: str, segments: Sequence[str], *, in_trash: bool = False) -> tier_policy.Named | None:
    """``fga_type`` ``segments`` as a name its grants' object is resolved from, or ``None`` for a type no uuid keys."""
    if fga_type == "table":
        return tier_policy.Named(kind="table", segments=tuple(segments), in_trash=in_trash)
    if fga_type == "namespace":
        return tier_policy.Named(kind="namespace", segments=tuple(segments), in_trash=in_trash)
    return None


def _rung(relation: str, fga_type: str, segments: Sequence[str], *, suffix: str = "") -> tier_policy.Rung:
    """``relation`` on ``fga_type`` ``segments``: on the name a table or namespace is resolved from, else on the id's object.

    A trash door's name also resolves to the object the trash holds under it (:data:`_TRASH_DOORS`).
    """
    named = _named(fga_type, segments, in_trash=suffix in _TRASH_DOORS)
    return tier_policy.Rung(relation=relation, on=named if named is not None else _object(fga_type, list(segments)))


def _label(rung: tier_policy.Rung) -> str:
    """The object ``rung`` is asked on, by name: what a refusal and the audit of an absent object say.

    One spelling for a table that is there and one that is not, so a refusal does not tell a caller which names exist.
    """
    if isinstance(rung.on, str):
        return rung.on
    return _object(rung.on.kind, list(rung.on.segments))


async def _resolve(resolver: Resolver, rung: tier_policy.Rung) -> str | None:
    """The FGA object ``rung`` is asked on: what its name resolves to, ``None`` when nothing has that name.

    A trash door's name with no live object resolves to the one trashed object it names; several are asked one by one
    (:func:`_require_trashed`).
    """
    if isinstance(rung.on, str):
        return rung.on
    obj = await resolver.object(rung.on.kind, list(rung.on.segments))
    if obj is None and rung.on.in_trash:
        trashed = await resolver.trashed(rung.on.kind, list(rung.on.segments))
        if len(trashed) == 1 and (uuid := trash.object_uuid(trashed[0])) is not None:
            return fga.object_of(rung.on.kind, uuid)
    return obj


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
    audit(AuditAction.AUTHZ, AuditOutcome.FAILURE, subject=user, resource=obj, reason="undeclared_door", suffix=suffix)


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


def _create_parent_rung(resource: str, id_segments: list[str], settings: Settings) -> tier_policy.Rung | None:
    """The ``can_create_*`` rung a create-on-parent op asks on the child's parent, or ``None`` to allow.

    Nested child -> its parent namespace, by name (its uuid is what the grants are held by). Top-level child -> the
    default WAREHOUSE object only when ``fga_lock_root_create`` is set; otherwise ``None`` (open self-serve top-level
    create — preserves the default behaviour). The relation is named after the child being created
    (``can_create_namespace`` / ``can_create_materialized_view`` / ``can_create_table``), all of which reduce to the
    writer rung on the parent namespace.

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
    if len(id_segments) > 1:
        return tier_policy.Rung(relation=relation, on=tier_policy.Named(kind="namespace", segments=tuple(id_segments[:-1])))
    if settings.fga_lock_root_create:
        return tier_policy.Rung(relation=relation, on=settings.fga_default_warehouse_object)
    return None


async def _require(client: OpenFgaClient, *, user: str, relation: str, obj: str, label: str | None = None) -> None:
    """Check one ``relation`` on ``obj``, audit the decision, and raise 403 on denial.

    ``label`` is what the refusal names the object as, its name for a table or namespace, so a refusal reads the same
    whether the name holds an object or not (:func:`_require_rung`).
    """
    try:
        allowed = await fga.check(client, user=user, relation=relation, obj=obj)
    except ServiceUnavailableError:  # authz layer down during an access attempt — audit, then fail closed
        audit(AuditAction.AUTHZ, AuditOutcome.FAILURE, relation=relation, subject=user, resource=obj, reason="authz_unavailable")
        raise
    audit(
        AuditAction.AUTHZ, AuditOutcome.ALLOW if allowed else AuditOutcome.DENY, relation=relation, subject=user, resource=obj
    )  # #41 audit every authz decision
    if not allowed:
        log.info("access_denied", extra={"sub": user, "relation": relation, "object": obj})
        raise PermissionDeniedError(f"{relation} required on {label or obj}")


def _refuse_absent(*, user: str, relation: str, label: str) -> PermissionDeniedError:
    """The refusal of ``relation`` on a name that holds no object, audited by the name: no tuple can grant it."""
    audit(AuditAction.AUTHZ, AuditOutcome.DENY, relation=relation, subject=user, resource=label, reason="no_such_object")
    log.info("access_denied", extra={"sub": user, "relation": relation, "object": label})
    return PermissionDeniedError(f"{relation} required on {label}")


async def _require_rung(client: OpenFgaClient, resolver: Resolver, *, user: str, rung: tier_policy.Rung) -> str:
    """Ask ``rung`` on the object its name resolves to, refuse it as :func:`_require` does, and return that object.

    A name that holds no object is refused alike: no tuple can name it, and the refusal names the name, as it does for
    an object that is there.
    """
    label = _label(rung)
    if not isinstance(rung.on, str) and rung.on.in_trash:
        segments = list(rung.on.segments)
        trashed = await resolver.trashed(rung.on.kind, segments)
        if trashed:
            return await _require_trashed(client, resolver, user=user, relation=rung.relation, named=rung.on, label=label, trashed=trashed)
        resolver.admit_trashed(rung.on.kind, segments, [])
    obj = await _resolve(resolver, rung)
    if obj is None:
        raise _refuse_absent(user=user, relation=rung.relation, label=label)
    await _require(client, user=user, relation=rung.relation, obj=obj, label=label)
    return obj


async def _require_trashed(
    client: OpenFgaClient,
    resolver: Resolver,
    *,
    user: str,
    relation: str,
    named: tier_policy.Named,
    label: str,
    trashed: list[dict[str, Any]],
) -> str:
    """Ask ``relation`` on every trashed object ``named`` leads to, admit those the caller may act on, and refuse if none.

    A trash door is addressed by a name several dropped objects may have held (ADR 0173 §9, [[LH-525]]), and each is
    its own object with its own grants. The door then acts only on what was admitted, so its answer, the list of
    uuids it offers when several remain, names nothing its caller holds no grant on. Each decision is audited.
    """
    objects = {fga.object_of(named.kind, uuid): record for record in trashed if (uuid := trash.object_uuid(record)) is not None}
    allowed = await fga.batch_check(client, user=user, relation=relation, objects=sorted(objects))
    for obj in sorted(objects):
        audit(AuditAction.AUTHZ, AuditOutcome.ALLOW if allowed.get(obj, False) else AuditOutcome.DENY, relation=relation, subject=user, resource=obj)
    admitted = {obj: record for obj, record in objects.items() if allowed.get(obj, False)}
    resolver.admit_trashed(named.kind, list(named.segments), list(admitted.values()))
    if not admitted:
        log.info("access_denied", extra={"sub": user, "relation": relation, "object": label})
        raise PermissionDeniedError(f"{relation} required on {label}")
    return next(iter(admitted))


async def _require_any(client: OpenFgaClient, *, user: str, doors: list[tuple[str, str]], label: str | None = None) -> None:
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
            audit(AuditAction.AUTHZ, AuditOutcome.FAILURE, relation=relation, subject=user, resource=obj, reason="authz_unavailable")
            raise
        if allowed:
            audit(AuditAction.AUTHZ, AuditOutcome.ALLOW, relation=relation, subject=user, resource=obj)
            return
    for relation, obj in doors:  # reaching here means every door was probed and every one denied
        audit(AuditAction.AUTHZ, AuditOutcome.DENY, relation=relation, subject=user, resource=obj)
    wanted = " or ".join(f"{relation} on {label or obj}" for relation, obj in doors)
    log.info("access_denied", extra={"sub": user, "relation": "any_of", "object": wanted})
    raise PermissionDeniedError(f"one of [{wanted}] required")


#: The transaction doors `_authorize_transaction` serves.
_TRANSACTION_SUFFIXES: Final = frozenset({"describe", "alter"})


async def _authorize_transaction(client: OpenFgaClient, resolver: Resolver, segments: list[str], suffix: str, *, token: Caller) -> None:
    """Authorize a transaction door on the table its id names (``<table id>$<transaction>``, [[LH-221]]).

    Describing a transaction reads the table's metadata (``can_get_metadata``) and altering one would write the table
    (``can_write_data``), asked on the object the table's name resolves to, as every table door asks. A describe naming
    an absent table answers the spec's not-found to a reader of its namespace, as every read door does.

    A recoverable drop's transaction is ``<table id>$<the trashed table's uuid>`` (ADR 0146): its describe is asked on
    that trashed table's own grants, which another table holding the name since does not share.

    Raises:
        InvalidInputError: The id names no table.
        PermissionDeniedError: The caller does not hold the door's rung on the table.
        TableNotFoundError: A reader of the namespace described a transaction of a table that does not exist.
    """
    table = transaction_table(segments)
    relation = "can_get_metadata" if suffix == "describe" else "can_write_data"
    if suffix == "describe" and (dropped := await resolver.trashed_as_transaction(segments)):
        named = tier_policy.Named(kind="table", segments=tuple(table), in_trash=True)
        await _require_trashed(client, resolver, user=token.subject, relation=relation, named=named, label=fga.canonical_object_id(segments), trashed=dropped)
        return
    try:
        await _require_rung(
            client, resolver, user=token.subject, rung=tier_policy.Rung(relation=relation, on=tier_policy.Named(kind="table", segments=tuple(table)))
        )
    except PermissionDeniedError:
        await _absent_to_a_reader_of_the_parent(client, resolver, token=token, fga_type="table", denied=relation, segments=table)
        raise


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
    resolver: Resolver,
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
    action = "can_revoke_grant" if revoking else f"can_grant_{relation}"
    # Shape-checked against the GRANT actions for a revoke too: the set of rungs that may be taken away is the set
    # that may be handed out. Only the AUTHORITY differs.
    if f"can_grant_{relation}" not in _grant_actions(fga_type):
        raise PermissionDeniedError(f"{relation!r} is not a grantable rung on {fga_type}")
    obj = await _require_rung(client, resolver, user=user, rung=_rung(action, fga_type, segments))
    if not revoking:
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
            audit(AuditAction.AUTHZ, AuditOutcome.FAILURE, relation="can_grant_self", subject=user, resource=obj, reason="authz_unavailable")
            raise
        if not inside:
            return
    # The caller is the grantee. Allowed only when it changes nothing they do not already have.
    try:
        already = await fga.check(client, user=subject, relation=relation, obj=obj, qualify=False)
    except ServiceUnavailableError:
        audit(AuditAction.AUTHZ, AuditOutcome.FAILURE, relation="can_grant_self", subject=user, resource=obj, reason="authz_unavailable")
        raise
    if already:
        return
    audit(AuditAction.AUTHZ, AuditOutcome.DENY, relation="can_grant_self", subject=user, resource=obj, grantee=resolved, granted_relation=relation)
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


def _string_list(value: object) -> list[str] | None:
    """``value`` when it is an identifier's segments (a list of strings), else ``None``."""
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) and all(isinstance(item, str) for item in value) else None


async def _json_body(request: Request) -> object:
    """The request's JSON body, or ``None`` when it holds none: the door's own validation answers a body it cannot read."""
    try:
        return await request.json()
    except ValueError:
        return None


def _batch_ids(body: Mapping[str, object]) -> Iterator[tuple[str | None, list[str]]]:
    """Each table id a batch's body names, with the operation that names it: ``None`` for an entry of ``entries``, else
    the operation's key in ``operations`` (``declare_table``, ``deregister_table``, ...). An item that is not an object,
    or an id that is not a list of strings, names nothing here; the door's own model refuses it."""
    entries = body.get("entries")
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and (segments := _string_list(entry.get("id"))) is not None:
            yield None, segments
    operations = body.get("operations")
    for operation in operations if isinstance(operations, list) else []:
        if not isinstance(operation, dict):
            continue
        for kind, sub in operation.items():
            if isinstance(sub, dict) and (segments := _string_list(sub.get("id"))) is not None:
                yield str(kind), segments


async def _authorize_batch(request: Request, client: OpenFgaClient, resolver: Resolver, settings: Settings, *, user: str) -> None:
    """Authorize body-keyed batch routes (no ``{id}`` path param); tables named in the body.

    version/commit/delete on an existing table -> ``can_write_data`` on the table; a
    ``declare_table`` operation is create-on-parent -> ``can_create_table`` on the parent
    namespace. One batch_check per relation, on the objects the body's names resolve to; a name
    that holds no object is refused with them. ``request.json()`` is cached by Starlette, so the
    downstream endpoint re-parses the same body.

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
    rungs: set[tier_policy.Rung] = set()
    for kind, segments in _batch_ids(body):
        if kind == "declare_table":  # creating a table -> can_create_table on its parent
            if (parent := _create_parent_rung("table", segments, settings)) is not None:
                rungs.add(parent)
        elif kind in _BATCH_OWNER_OPS:  # destructive -> owner tier (no writer back-door)
            rungs.add(_rung(_BATCH_OWNER_OPS[kind], "table", segments))
        else:  # an entry, or version/commit/append on an existing table -> can_write_data on it
            rungs.add(_rung("can_write_data", "table", segments))

    denied: list[str] = []
    # ONE round trip per DISTINCT relation, not per object (catalog-api-12): `operations` is client-supplied and
    # unbounded, so a per-object loop cost N sequential authorization round trips before any work was authorized.
    for relation in sorted({rung.relation for rung in rungs}):
        asked = sorted((rung for rung in rungs if rung.relation == relation), key=lambda rung: _label(rung))
        objects = {_label(rung): await _resolve(resolver, rung) for rung in asked}
        present = sorted({obj for obj in objects.values() if obj is not None})
        allowed = await fga.batch_check(client, user=user, relation=relation, objects=present) if present else {}
        for label, obj in objects.items():  # #41 audit every batch authz decision (allow AND deny), like the single-route gate
            granted = obj is not None and allowed.get(obj, False)
            audit(AuditAction.AUTHZ, AuditOutcome.ALLOW if granted else AuditOutcome.DENY, relation=relation, subject=user, resource=obj or label)
            if not granted:
                denied.append(label)
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


def _renamed_to(body: object, source: list[str]) -> list[str] | None:
    """The identifier a rename's body moves ``source`` to, by the door's own rule (``identifiers.rename_destination``), or
    ``None`` when the body names no table name, which the door's own model refuses."""
    if not isinstance(body, dict):
        return None
    name = body.get("new_table_name")
    if not isinstance(name, str):
        return None
    return rename_destination(source, _string_list(body.get("new_namespace_id")), name)


async def _rename_destination(request: Request, segments: list[str]) -> tuple[str, ...]:
    """The table a rename lands in. A body naming no destination leaves the source standing in; the door answers the 400."""
    destination = _renamed_to(await _json_body(request), segments)
    return tuple(segments if destination is None else destination)


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
    segments = parse_identifier(object_id)
    if not segments or suffix in _GRANT_SUFFIXES:
        return None
    fga_type = _FGA_TYPE[resource]
    obj = _object(fga_type, segments)
    run = run_context.from_headers(request.headers) if suffix in tier_policy.STAMPING_DOORS else None
    if suffix in _CREATE_ON_PARENT_SUFFIXES.get(resource, frozenset()):
        return tier_policy.Write(op=suffix, obj=obj, rung=_create_parent_rung(resource, segments, settings), table=tuple(segments), run=run)
    if suffix == "credentials":
        # A vend is a read at its default tier and a WRITE at a writing one: the credential is how a
        # direct-to-storage writer writes, so it is judged on the rung its tier opens.
        writing = VEND_TIER_RUNGS.get(request.query_params.get("tier", "read"))
        if writing is None:
            return None
        return tier_policy.Write(op=suffix, obj=obj, rung=_rung(writing, fga_type, segments), table=tuple(segments))
    try:
        relation = _action_relation(fga_type, suffix)
    except InternalError:
        return None
    if relation in _READ_RELATIONS:
        return None
    table = await _rename_destination(request, segments) if suffix == "rename" else tuple(segments)
    moved_from = tuple(segments) if suffix == "rename" else ()
    return tier_policy.Write(
        op=suffix,
        obj=obj,
        rung=_rung(relation, fga_type, segments, suffix=suffix),
        table=table,
        run=run,
        moved_from=moved_from,
    )


async def require_declared_inputs(
    settings: Settings, token: Caller | None, injected: OpenFgaClient | None, resolver: Resolver, run: run_context.RunContext
) -> dict[str, run_context.RunInput]:
    """Each input ``run`` declares, by the uuid of the table it names now, once each names a table the committer may read
    (ADR 0183 §2).

    The name is resolved through ``resolver``, the request's one resolution of every name, and asked ``can_read_data``
    on that uuid with FGA on. A name holding no table and a table the committer may not read are refused alike, so the
    refusal tells no committer which tables exist. A commit's provenance is then a fact the catalog checked, and an
    erasure names what it declared by uuid (:mod:`catalog.services.table_inputs`).

    Raises:
        InvalidInputError: An input names no table, or one the committer may not read.
        UnauthenticatedError: FGA is on and nobody is signed in.
    """
    check = _rung_check(settings, token, injected, resolver)
    if check is not None:
        for entry in run.inputs:
            try:
                await check(tier_policy.Rung(relation="can_read_data", on=tier_policy.Named(kind="table", segments=tuple(entry.table))))
            except (PermissionDeniedError, object_uuids.UnidentifiedObjectError) as exc:
                raise _unreadable_input(entry) from exc
    return await declared_inputs(resolver, run)


async def declared_inputs(resolver: Resolver, run: run_context.RunContext | None) -> dict[str, run_context.RunInput]:
    """Each input ``run`` declares, by the uuid of the table its name resolves to through ``resolver``; none for no run.

    A door reads here what the tier judge already judged (:func:`require_declared_inputs`), from the request's one
    resolution, so the uuids it records are the ones authorized.

    A table that records no uuid holds no grant ([[LH-534]]), so it is refused as one the committer may not read.

    Raises:
        InvalidInputError: An input names no table, or one no uuid identifies.
    """
    declared: dict[str, run_context.RunInput] = {}
    for entry in run.inputs if run is not None else ():
        try:
            resolved = await resolver.table(list(entry.table))
            if resolved.location is None:
                raise _unreadable_input(entry)
            declared.setdefault(resolved.require_uuid(), entry)
        except (PermissionDeniedError, NamespaceNotFoundError, TableNotFoundError, object_uuids.UnidentifiedObjectError) as exc:
            raise _unreadable_input(entry) from exc
    return declared


def _unreadable_input(entry: run_context.RunInput) -> InvalidInputError:
    return InvalidInputError(
        f"commit refused: the input {fga.canonical_object_id(entry.table)!r} its run declares ({run_context.INPUTS_PROPERTY}) names no table "
        "the committer may read (can_read_data); a run declares each table it read, by its name now"
    )


def _rung_check(settings: Settings, token: Caller | None, injected: OpenFgaClient | None, resolver: Resolver) -> tier_policy.RungCheck | None:
    """The tier judge's rung check, asked through :func:`_require_rung`; ``None`` with FGA off, when every rung allows.

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
        try:
            await _require_rung(client, resolver, user=user, rung=rung)
        except object_uuids.UnidentifiedObjectError:
            # Every door but the deregister keeps answering code 18 for an object no uuid identifies, and the
            # deregister is a judged write door, so this is the one place that door's rung is asked.
            if rung.relation != _UNIDENTIFIED_TABLE_RUNG:
                raise
            await _require_estate_admin_for_unidentified(client, user=user, root=settings.fga_root_object, rung=rung)

    return check


#: The rung of the one door that reaches a table no uuid identifies: its deregister, which detaches the row and deletes no
#: byte ([[LH-534]]).
_UNIDENTIFIED_TABLE_RUNG: Final = "can_deregister"


async def _require_estate_admin_for_unidentified(client: OpenFgaClient, *, user: str, root: str, rung: tier_policy.Rung) -> None:
    """Admit the estate admin to deregister a table no uuid identifies, and refuse anyone else as a name holding nothing is refused.

    Every grant is held by an object's uuid (ADR 0173 §8), so such a table holds none, and its deregister asks the rung
    every estate-wide door asks, ``can_observe_events`` on the estate root ([[LH-534]]). The decision is audited on the
    table by its name, with the root it was decided on; a refusal names the door's own rung on the name, so it tells no
    caller that the name holds a table.
    """
    label = _label(rung)
    try:
        allowed = await fga.check(client, user=user, relation="can_observe_events", obj=root)
    except ServiceUnavailableError:  # authz layer down during an access attempt — audit, then fail closed
        audit(AuditAction.AUTHZ, AuditOutcome.FAILURE, relation="can_observe_events", subject=user, resource=label, decided_on=root, reason="authz_unavailable")
        raise
    audit(
        AuditAction.AUTHZ,
        AuditOutcome.ALLOW if allowed else AuditOutcome.DENY,
        relation="can_observe_events",
        subject=user,
        resource=label,
        decided_on=root,
        reason="unidentified_table",
    )
    if not allowed:
        log.info("access_denied", extra={"sub": user, "relation": rung.relation, "object": label})
        raise PermissionDeniedError(f"{rung.relation} required on {label}")


async def _judge(
    request: Request, settings: Settings, token: Caller | None, injected: OpenFgaClient | None, resolver: Resolver, write: tier_policy.Write
) -> None:
    """Hand ``write`` to the tier judge, with the rung asked through :func:`_require_rung` when FGA is on."""
    check = _rung_check(settings, token, injected, resolver)
    # Imported HERE rather than at module scope: `dependencies` imports this module for its guards.
    from catalog.api.dependencies import namespace_for_top_ns

    async def namespace(top_ns: str) -> LanceNamespace:
        return await namespace_for_top_ns(request, settings, top_ns)

    await tier_policy.judge(
        write,
        caller=token.subject if token is not None else None,
        check=check,
        inputs=partial(require_declared_inputs, settings, token, injected, resolver),
        namespace=namespace,
    )


async def judge_staged_run(
    settings: Settings,
    token: Caller | None,
    injected: OpenFgaClient | None,
    resolver: Resolver,
    *,
    ns: LanceNamespace,
    segments: Sequence[str],
    run: run_context.RunContext | None,
    adds_data: bool,
) -> dict[str, run_context.RunInput]:
    """Judge the run a stock client's staged commit to table ``segments`` names (ADR 0157), and answer each input it
    declares by the uuid of the table it names (:func:`require_declared_inputs`).

    Only a commit that ``adds_data`` must name a run on a derived tier, whose tier is then read through ``ns``, the
    connection the door resolved for the table; any declared input is judged either way.

    Raises:
        InvalidInputError: The table is silver or gold and the commit names no run or no input, or an input names no
            table the committer may read.
    """
    tier = await tier_policy.tier_of(ns, segments[:-1]) if adds_data and len(segments) > 1 else None
    names = f"the staged manifest's transaction properties {run_context.RUN_ID_PROPERTY} and {run_context.INPUTS_PROPERTY}"
    declared: dict[str, run_context.RunInput] = {}

    async def inputs(named: run_context.RunContext) -> None:
        declared.update(await require_declared_inputs(settings, token, injected, resolver, named))

    await tier_policy.judge_run(tier, run, inputs=inputs, names=names)
    return declared


async def _granted_vend_tier(client: OpenFgaClient, subject: str, obj: str) -> Literal["read", "write"] | None:
    """The tier ``subject``'s rungs on ``obj`` open at a spec door, each rung that decided it audited ALLOW."""
    writing = VEND_TIER_RUNGS["write"]
    if await fga.check(client, user=subject, relation=writing, obj=obj):
        audit(AuditAction.AUTHZ, AuditOutcome.ALLOW, relation=writing, subject=subject, resource=obj)
        return "write"
    if not await fga.check(client, user=subject, relation="can_read_data", obj=obj):
        return None
    audit(AuditAction.AUTHZ, AuditOutcome.ALLOW, relation="can_read_data", subject=subject, resource=obj)
    if not await fga.check(client, user=subject, relation="can_maintain", obj=obj):
        return "read"
    audit(AuditAction.AUTHZ, AuditOutcome.ALLOW, relation="can_maintain", subject=subject, resource=obj)
    return "write"


async def spec_vend_tier(
    settings: Settings, token: Caller | None, injected: OpenFgaClient | None, ns: LanceNamespace, segments: Sequence[str], *, table_uuid: str, door: str
) -> Literal["read", "write"] | None:
    """The highest tier a spec door (``describe``, ``declare``) vends the caller on table ``segments`` (ADR 0156 C3).

    ``table_uuid`` is the uuid the door resolved the name to (or minted, for a declare), whose object the grants are
    held by. ``can_write_data`` opens the write tier, and that is a write, so the tier judge decides it and audits the
    tier it was taken under, as it does for the management vend door; ``ns`` is the connection the door already
    resolved for the table, which is the one its namespace path's tier is read through. ``can_read_data`` with
    ``can_maintain``, a compaction job's rungs, opens the write tier too: its puts are what a compaction stages, and
    ``version/create`` admits a maintainer's commit for a compaction's two operations alone (ADR 0162, ADR 0174). Those
    puts reach what a writer's do, so a put over a committed data file is as open to it as to a writer: no session
    policy refuses one (``vending.build_session_policy``). ``can_read_data`` alone opens the read tier. None of them, no
    credential: the door still answers, because describing a table is a metadata read. Maintain is never vended here;
    it is the maintenance identity's (LH-473).

    The rungs are probed with ``fga.check``, which writes no DENY: a reader's describe asking whether it may also
    write is not an access attempt. The rung that decides the tier is audited ALLOW, as every authorization
    decision is; whether a credential is then minted is the door's ``vend_credentials`` record. With FGA off
    every rung allows, so the write tier is vended, as the management door vends it.

    Raises:
        ServiceUnavailableError: FGA is on and unwired, or OpenFGA failed: fail closed, as every gate here does.
        UnauthenticatedError: FGA is on and nobody is signed in.
    """
    obj = fga.table_object(table_uuid)
    if settings.fga_enabled:
        client = require_wired(injected)
        if token is None:
            raise UnauthenticatedError("authentication required")
        granted = await _granted_vend_tier(client, token.subject, obj)
        if granted != "write":
            return granted

    async def namespace(_top_ns: str) -> LanceNamespace:
        return ns

    write = tier_policy.Write(op=door, obj=_object("table", list(segments)), rung=None, table=tuple(segments))
    await tier_policy.judge(write, caller=token.subject if token is not None else None, check=None, inputs=_names_no_run, namespace=namespace)
    return "write"


async def _names_no_run(run: run_context.RunContext) -> None:
    """The inputs check of a write that names no run, which the judge never asks."""
    raise InternalError(f"a vend names no run, yet the tier judge asked for the inputs of run {run.run_id!r}")


#: The spec's ``delimiter`` query parameter, declared on every route by the router's shape rung.
DelimiterQuery = Annotated[str | None, Query(description="Identifier separator. Must match the server's, which is returned in the refusal when it does not.")]


async def require_request_names(request: Request, delimiter: DelimiterQuery = None) -> None:
    """The router's shape rung: refuse, 400, a request whose identifiers no door can read (ADR 0173 §4, [[LH-526]]).

    Every door judges a request in one order: who is asking (401), whether the request is well formed (400, here), and
    whether the caller may make it (403, :func:`authorize`). A malformed request is the caller's mistake whatever they
    hold, and a 403 would send them to ask for a grant that no grant can give. Two shapes are judged:

    - ``delimiter``, how the caller split its identifier. A request naming none uses ``$``
      (``lance_docs/ns_catalog/spec.yaml:2314-2318``), the one the catalog splits on (``CATALOG_DELIMITER``), so a
      request naming another is refused rather than split wrongly;
    - every identifier a door is asked to give an object, which must be made of names (``identifiers.require_names``):
      the path id of a door that makes a table, namespace or view, a rename's destination and a batch's ids. Any other
      door is addressed by a name that holds an object or not, and answers so.

    Router-level, never per route: a per-route check is the one the next route added forgets.

    Raises:
        InvalidInputError: ``delimiter`` is not ``$``, or an identifier names nothing or holds a segment that is not a name.
    """
    if delimiter is not None and delimiter != CATALOG_DELIMITER:
        raise InvalidInputError(
            f"this catalog splits identifiers with {CATALOG_DELIMITER!r}, not {delimiter!r}. "
            f"Send the identifier separated by {CATALOG_DELIMITER!r}, or omit `delimiter`."
        )
    for segments in await _identifiers_given(request):
        if not segments:
            raise InvalidInputError("an object is given a name: an empty identifier names the root namespace, which no door makes")
        require_names(segments)


async def _identifiers_given(request: Request) -> list[list[str]]:
    """The identifiers ``request`` asks a door to give an object: a making door's path id, a rename's destination, a batch's ids."""
    path: str = request.scope["path"]
    if path.rstrip("/") in _BATCH_PATHS:
        body = await _json_body(request)
        return [segments for _kind, segments in _batch_ids(body)] if isinstance(body, dict) else []
    object_id = request.path_params.get("id")
    resource = _resource_for(path)
    if object_id is None or resource is None:
        return []
    suffix = _suffix(path, resource, object_id)
    if suffix in _CREATE_ON_PARENT_SUFFIXES.get(resource, frozenset()):
        return [parse_identifier(object_id)]
    if resource == "table" and suffix == "rename":
        destination = _renamed_to(await _json_body(request), parse_identifier(object_id))
        return [] if destination is None else [destination]
    return []


async def authorize(request: Request, settings: SettingsDep, token: CurrentToken, injected: FgaClientDep, resolver: ResolverDep) -> None:
    """Enforce the caller's permission for this route: the tier judge decides every write door, with FGA on
    or off, and OpenFGA every other door (a no-op when FGA is off).

    A table or namespace is asked about as the object its name resolves to, ``table:<uuid>`` or ``namespace:<uuid>``
    (ADR 0173 §8, [[LH-524]]), through ``resolver``, which the door then reads the same resolution from. ``resolver``
    is the last parameter so an unauthenticated caller is refused before any name is resolved.
    """
    if (write := await _write_door(request, settings)) is not None:
        await _judge(request, settings, token, injected, resolver, write)
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
            await _authorize_batch(request, client, resolver, settings, user=token.subject)
        # Other id-less routes (collection lists, health) need only authentication.
        return

    resource = _resource_for(path)
    if resource is None:
        return  # not a guarded resource path — authentication already enforced
    suffix = _suffix(path, resource, object_id)
    # Canonicalize the id ONCE, the same way the endpoints + grant path do (parse_identifier),
    # so the check object and the seeded object agree byte-for-byte even for edge forms
    # (e.g. the delimiter-only root id). Passing the raw URL string here would diverge.
    segments = parse_identifier(object_id)

    # Create-on-parent: child does not exist yet → authorize the parent.
    if suffix in _CREATE_ON_PARENT_SUFFIXES.get(resource, frozenset()):
        if (parent := _create_parent_rung(resource, segments, settings)) is not None:  # None => open top-level create (authn already enforced)
            await _require_rung(client, resolver, user=token.subject, rung=parent)
        return

    # A transaction is authorized on the table its id names, not on an object of its own.
    if resource == "transaction":
        if suffix not in _TRANSACTION_SUFFIXES:
            _record_undeclared(path, "transaction", suffix, user=token.subject, obj=_object("transaction", segments))
            raise _undeclared_door("transaction", suffix)
        await _authorize_transaction(client, resolver, segments, suffix, token=token)
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
    # ONLY THE PER-ITEM READS RETURN. `list` and `table/list` authorize every item they return on the object it
    # resolves to — the route opens and the ITEMS are checked, because a
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
    # Grant/revoke authorize on the RUNG BEING HANDED OUT, which lives in the body — so, like the
    # batch routes above, this one cannot be answered from the path alone.
    if suffix in _GRANT_SUFFIXES:
        await _authorize_grant(request, client, resolver, user=token.subject, fga_type=fga_type, segments=segments, revoking=suffix == "access/revoke")
        return
    # A CLASSIFICATION IS A LABEL, NOT A DATASET ([[LH-055]]). Its router carries exactly three routes
    # and the two grant ones returned above, so only `access/my-permissions` reaches here. It resolves
    # HERE rather than in `_action_relation`, whose vocabulary names relations this type does not define,
    # and OpenFGA answers an undefined relation with a 400 that fails closed to 503 — an outage wearing a
    # permission error's clothes.
    if fga_type == "classification":
        obj = _object(fga_type, segments)
        if suffix != "access/my-permissions":
            _record_undeclared(path, fga_type, suffix, user=token.subject, obj=obj)
            raise _undeclared_door(fga_type, suffix)
        await _require(client, user=token.subject, relation="can_get_metadata", obj=obj)
        return
    try:
        relation = _action_relation(fga_type, suffix)
    except InternalError:
        _record_undeclared(path, fga_type, suffix, user=token.subject, obj=_object(fga_type, segments))
        raise
    rung = _rung(relation, fga_type, segments, suffix=suffix)
    if (alternative := _ALTERNATIVE_RUNGS.get((fga_type, suffix))) is not None:
        obj = await _resolve(resolver, rung)
        if obj is None:
            raise _refuse_absent(user=token.subject, relation=relation, label=_label(rung))
        await _require_any(client, user=token.subject, doors=[(relation, obj), (alternative, obj)], label=_label(rung))
        return
    try:
        await _require_rung(client, resolver, user=token.subject, rung=rung)
    except PermissionDeniedError:
        # A READ door refusing an object that is not there answers the spec's 404 instead, but only to
        # a caller who already holds the parent's read rung. Owner ruling 2026-09-11.
        await _absent_to_a_reader_of_the_parent(client, resolver, token=token, fga_type=fga_type, denied=relation, segments=segments)
        raise


async def _absent_to_a_reader_of_the_parent(
    client: OpenFgaClient,
    resolver: Resolver,
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
      rung the answer is today's 403 — so this cannot be used to walk the id space from outside the
      hierarchy.
    * ONLY AN ABSENCE CONVERTS. An object that EXISTS and is forbidden still answers 403. Converting
      that would tell an unauthorized caller which names are real — the oracle this estate refuses —
      and it is the property worth more than the feature.

    A PARENT THAT IS GONE TOO IS READ AT THE NEAREST ANCESTOR THAT STANDS ([[LH-536]]). A cascade leaves a child whose
    parent went with it; a reader of the nearest standing ancestor asking about it is told no more than asking about
    that ancestor's absent child would tell them. A top-level name has no ancestor to read, and is never asked of its
    warehouse: upward visibility (``can_get_metadata from child``) makes a reader of one namespace a reader of its
    warehouse, so a warehouse's readers would learn which of every tenant's names exist from 403 against 404. What a
    RECOVERABLE drop leaves is answered by its trash: the drop keeps its objects' grants for the undrop, so a caller
    holding ``can_get_metadata`` on an object trashed at the name or an ancestor's, on the chain hanging off what
    stands, already sees it through the trash doors and is answered the absence rather than a refusal that hides it. A
    trash it cannot read leaves the 403.

    Whether the name holds an object is the resolution ``authorize`` already read, so the answer is the one the door
    would act on. The request never reaches the endpoint either way: this raises, and its caller re-raises. Nothing
    is read, and no grant is implied by the 404.
    """
    if denied not in _READ_RELATIONS or fga_type not in _UUID_KEYED:
        return
    if await resolver.object(fga_type, segments) is not None:
        return
    try:
        standing, ancestor = await _nearest_standing_ancestor(resolver, segments)
        # The trash is listed only when no standing ancestor admits the caller: it is one read of every record.
        if not await _reads_any(client, user=token.subject, objects=[ancestor] if ancestor is not None else []) and not await _reads_any(
            client, user=token.subject, objects=await _trashed_below(resolver, fga_type, segments, standing=standing)
        ):
            return
    except ServiceUnavailableError:
        return  # the authz layer is down; the 403 the caller already has is the fail-closed answer
    except (OSError, ValueError):
        log.warning("absent_object_trash_unreadable", extra={"sub": token.subject, "object": fga.canonical_object_id(segments)}, exc_info=True)
        return
    ident = fga.canonical_object_id(segments)
    absent = TableNotFoundError if fga_type == "table" else NamespaceNotFoundError
    raise absent(f"{fga_type} {ident!r} does not exist")


async def _nearest_standing_ancestor(resolver: Resolver, segments: list[str]) -> tuple[int, str | None]:
    """The depth and object of the nearest ancestor namespace of ``segments`` that stands, ``(0, None)`` where none does."""
    for depth in range(len(segments) - 1, 0, -1):
        if (ancestor := await resolver.object("namespace", segments[:depth])) is not None:
            return depth, ancestor
    return 0, None


async def _trashed_below(resolver: Resolver, fga_type: str, segments: list[str], *, standing: int) -> list[str]:
    """The objects trashed at ``segments`` or an ancestor's name deeper than the ``standing`` one, on the chain that hangs
    off what stands.

    Each depth admits a record only where its namespace at the drop is the standing ancestor or a record admitted one
    depth up: a grant kept on a trashed ``tree$kids`` says nothing about what a ``tree`` created since holds.
    """
    parents = {await resolver.namespace_uuid(segments[:standing])} if standing else None
    objects: list[str] = []
    for depth in range(standing + 1, len(segments) + 1):
        kind = fga_type if depth == len(segments) else "namespace"
        records = await resolver.trashed(kind, segments[:depth])
        admitted = {
            uuid for record in records if (uuid := trash.object_uuid(record)) is not None and (parents is None or record.get("namespace_uuid") in parents)
        }
        objects += [fga.object_of(kind, uuid) for uuid in sorted(admitted)]
        parents = admitted
    return objects


async def _reads_any(client: OpenFgaClient, *, user: str, objects: list[str]) -> bool:
    """Whether ``user`` holds ``can_get_metadata`` on any of ``objects``."""
    if not objects:
        return False
    allowed = await fga.batch_check(client, user=user, relation="can_get_metadata", objects=objects)
    return any(allowed.get(obj, False) for obj in objects)


#: How many candidates one batch check of a listing authorizes before the page is judged full.
LISTING_BATCH: Final = 100


async def authorized_page(
    client: OpenFgaClient,
    resolver: Resolver,
    *,
    user: str,
    relation: str,
    kind: Literal["table", "namespace"],
    candidates: Sequence[tuple[str, Sequence[str]]],
    page_token: str | None,
    limit: int | None,
) -> tuple[list[str], str | None]:
    """The page of ``candidates`` the caller holds ``relation`` on, and the page token after it ([[LH-223]]).

    Each candidate is the name the listing answers (sorted) and the segments of the object it names.

    A listing authorizes its own candidates, batch by batch, on the objects their names resolve to, and fills its page
    from them. It never asks OpenFGA which objects the caller reaches across the estate: that answer stops at the
    server's ceiling (1,000 objects), so a caller reaching more than that elsewhere would see a small namespace listed
    short. The cursor is the last name served, as :func:`catalog.api.pagination.paginate` keeps it; a ``None`` token
    means every candidate was judged.

    A candidate that records no uuid holds no grant, so it is answered to no caller, and is left out rather than fail the
    listing for every caller ([[LH-534]]).
    """
    remaining = [(name, list(segments)) for name, segments in candidates if not page_token or name > page_token]
    page: list[str] = []
    for start in range(0, len(remaining), LISTING_BATCH):
        batch = remaining[start : start + LISTING_BATCH]
        objects = await asyncio.gather(*(_listed_object(resolver, kind, name, segments) for name, segments in batch))
        present = sorted({obj for obj in objects if obj is not None})
        allowed = await fga.batch_check(client, user=user, relation=relation, objects=present) if present else {}
        for offset, ((name, _), obj) in enumerate(zip(batch, objects, strict=True)):
            if obj is None or not allowed.get(obj, False):
                continue
            page.append(name)
            if limit is not None and len(page) == limit:
                more = start + offset + 1 < len(remaining)
                return page, name if more else None
    return page, None


async def _listed_object(resolver: Resolver, kind: Literal["table", "namespace"], name: str, segments: list[str]) -> str | None:
    """The FGA object a listing's candidate ``name`` resolves to; ``None`` for one that names nothing or records no uuid."""
    try:
        return await resolver.object(kind, segments)
    except object_uuids.UnidentifiedObjectError as unidentified:
        log.error("listing_left_out_unidentified_object", extra={"kind": kind, "object_name": name, "location": unidentified.location})
        return None


# --------------------------------------------------------------------------- #
# Hierarchy enforcement — the estate's containment rule, checked BEFORE the write.
# --------------------------------------------------------------------------- #


def require_parent(resource: str, segments: list[str]) -> None:
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
    if fga.parent_namespace_id(segments) is not None:
        return
    ident = fga.canonical_object_id(segments)
    raise InvalidInputError(
        f"table '{ident}' has no namespace to belong to. A table must live inside a namespace "
        f"(project > warehouse > namespace > table), so its identifier needs at least one "
        f"namespace segment before the table name — e.g. '<namespace>{CATALOG_DELIMITER}{ident}'. "
        "Create the namespace first if it does not exist."
    )


async def require_parent_exists(ns: LanceNamespace, resource: str, segments: list[str]) -> None:
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
    require_parent(resource, segments)
    parent = segments[:-1]
    if not parent:
        return
    try:
        await run_in_threadpool(native.call, ns, "namespace_exists", NamespaceExistsRequest(id=parent))
    except UnsupportedOperationError:
        return
    except NamespaceNotFoundError as exc:
        ident = fga.canonical_object_id(segments)
        parent_id = fga.canonical_object_id(parent)
        raise NamespaceNotFoundError(
            f"cannot create '{ident}': its parent namespace '{parent_id}' does not exist. "
            f"Create it first (POST /v1/namespace/{parent_id}/create, or the warehouse-scoped door "
            "for a top-level namespace) — a table whose namespace is absent is invisible to every "
            "cascade and to every parent-scoped grant."
        ) from exc


def require_namespace_depth(segments: list[str]) -> None:
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
            f"namespace {fga.canonical_object_id(segments)!r} nests {len(segments)} levels deep; the maximum is "
            f"{MAX_NAMESPACE_DEPTH - 1}. Deeper trees exceed what the authorization model can resolve, "
            f"which takes metadata reads down for the whole warehouse rather than denying just this one."
        )


def require_warehouse_scoped(segments: list[str], *, warehouses_enabled: bool) -> None:
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
    ident = fga.canonical_object_id(segments)
    raise InvalidInputError(
        f"top-level namespace '{ident}' must belong to a warehouse. A namespace is logical "
        f"separation inside a tenant (project > warehouse > namespace > table), and an unbound "
        f"namespace resolves to the shared default bucket instead of the tenant's own. "
        f"Create it through its warehouse — POST /management/v1/warehouses/{{warehouse_id}}/namespaces with "
        f"name '{ident}' — or nest it under an existing namespace "
        f"('<parent>{CATALOG_DELIMITER}{ident}')."
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

    def fga_object(self) -> str:
        """The FGA object its grants are held by: ``table:<uuid>`` or ``namespace:<uuid>`` ([[LH-524]])."""
        return fga.object_of(self.kind, self.uuid)


async def identify(ns: LanceNamespace, settings: Settings, kind: Literal["table", "namespace"], segments: list[str]) -> GovernedObject:
    """The object ``segments`` names now, with its uuid.

    Raises:
        TableNotFoundError: No such table.
        NamespaceNotFoundError: No such namespace.
        UnidentifiedObjectError: The object carries no uuid, which no door leaves.
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
    require_not_protected(guard or {}, kind="table", obj_id=fga.canonical_object_id(table.segments), force=False)


async def require_namespace_unprotected(settings: Settings, namespace: GovernedObject, *, force: bool) -> None:
    """Refuse a door that removes ``namespace`` while its protection record stands, unless ``force``.

    Raises:
        NamespaceNotEmptyError: The namespace is protected.
    """
    if force:
        return
    guard = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "namespace", namespace.uuid)
    require_not_protected(guard or {}, kind="namespace", obj_id=fga.canonical_object_id(namespace.segments), force=False)


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


def owned_object(resource: str, uuid: str) -> str:
    """The FGA object a just-created ``resource`` with ``uuid`` is seeded on ([[LH-524]])."""
    return fga.object_of("table" if resource == "table" else "namespace", uuid)


async def parent_object_of(resolver: Resolver, settings: Settings, resource: str, segments: list[str]) -> str | None:
    """The object a just-created child's ``parent`` edge names: its parent namespace's uuid object, the default warehouse
    for a top-level namespace, or ``None`` for a top-level table (``table.parent`` accepts only a namespace).

    ``fga_default_warehouse_object``, never ``fga_root_object``: the model types ``namespace#parent`` as ``[warehouse,
    namespace]``, and every rung the namespace inherits ``from parent`` has to be defined on the parent's type.

    Raises:
        NamespaceNotFoundError: The parent namespace is gone; the create door checked it existed before its write.
    """
    if len(segments) > 1:
        parent = await resolver.object("namespace", segments[:-1])
        if parent is None:
            raise NamespaceNotFoundError(f"namespace not found: {segments[:-1]}")
        return parent
    return settings.fga_default_warehouse_object if resource == "namespace" else None


async def seed_ownership(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    resolver: Resolver,
    *,
    resource: str,
    segments: list[str],
    uuid: str,
    parent_object: str | None = None,
    may_grant_owner: bool = True,
) -> None:
    """Grant the creator ``owner`` + the ``parent`` edge on a just-created object, ``table:<uuid>`` or ``namespace:<uuid>``.

    The object is keyed by the uuid the create minted (ADR 0173 §8, [[LH-524]]), so no tuple an earlier object at the
    same name left can reach it: a table dropped and created again under that name is another uuid.

    ``may_grant_owner=False`` writes the EDGE ALONE. The caller uses it when this request did not
    create the object — an ExistOk that kept an existing table — where granting ``owner`` would seize
    another caller's table, but the structural edge is still required and is safe: it names where the
    table lives, it is idempotent, and it confers nothing by itself.

    No-op when FGA is off, the request is unauthenticated, or the client is unwired — so
    create endpoints call it unconditionally with one line. This is the ONE place the
    post-create seed policy lives.

    ``parent_object`` overrides the derived parent (:func:`parent_object_of`) for the one door whose hierarchy is not
    positional: a warehouse-scoped namespace hangs off ``warehouse:<id>``, not off the shared catalog root its
    segments would imply.
    """
    if not (settings.fga_enabled and token is not None and client is not None):
        return
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
    # THE PRINCIPAL'S TYPE IS THE DISCRIMINATOR, never a name pattern: a verifier builds a `ServicePrincipal`
    # only for a machine it proved, a service account or a Dex client signed in for itself (ADR 0181), so the
    # question is answered by how the caller was proved, not by what its subject is called — a name test
    # drifts the moment a service is renamed.
    await fga.grant_on_create(  # noqa: TID251
        client,
        grant_owner=may_grant_owner and not isinstance(token, ServicePrincipal),
        user_sub=token.subject,
        obj=owned_object(resource, uuid),
        actor=token.subject,
        origin="create",
        parent_object=parent_object or await parent_object_of(resolver, settings, resource, segments),
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


async def require_grants_may_follow(client: OpenFgaClient | None, settings: Settings, *, obj: str, source: list[str], destination: list[str]) -> None:
    """Refuse a rename whose grants the grant door would refuse on ``destination`` ([[LH-215]]).

    A table's grants are held by its uuid (``obj``) and stay on it through a rename, so a rename into another tenant's
    namespace takes every grant along. Each is held to the grant door's own rule (:func:`refuse_a_cross_tenant_role_grant`):
    a role one tenant owns is not granted a rung on another tenant's object by renaming the object across. Asked BEFORE
    the native rename, so a refusal leaves the table where it was. A destination under the same top-level namespace has
    the source's tenant, and nothing is read.
    """
    if not (settings.fga_enabled and client is not None) or source[:1] == destination[:1]:
        return
    for held in await fga.read_object_tuples(client, obj):
        if held.user and held.relation != "parent":
            await refuse_a_cross_tenant_role_grant(client, held.user, fga_type="table", segments=destination, settings=settings)


async def move_parent_edge(client: OpenFgaClient | None, settings: Settings, token: Caller | None, *, obj: str, source: str, destination: str) -> None:
    """Move ``obj``'s ``parent`` edge, and its inverse, from namespace ``source`` to ``destination``: what a rename into
    another namespace changes in OpenFGA, and all it changes ([[LH-524]]).

    Every grant stays on ``obj``, the table's uuid. Only where the table inherits from moves, as it would for a table
    created there. The new edge is written before the old one goes, so the table is never left with no parent; a failed
    removal leaves it reachable from both until a reconcile, which inherits more, never less, and is logged.
    """
    if not (settings.fga_enabled and token is not None and client is not None) or source == destination:
        return
    await fga.grant_on_create(client, grant_owner=False, user_sub=token.subject, obj=obj, actor=token.subject, origin="rename", parent_object=destination)  # noqa: TID251
    stale = [fga.ClientTuple(user=source, relation="parent", object=obj), fga.ClientTuple(user=obj, relation="child", object=source)]
    try:
        await fga.delete_tuples(client, stale, actor=token.subject, origin="rename")  # noqa: TID251
    except Exception as exc:  # noqa: BLE001 — the rename has committed; a second parent edge inherits more, never less
        log.warning("rename_parent_edge_not_removed", extra={"object": obj, "parent": source, "error": str(exc)})


async def revoke_ownership(client: OpenFgaClient | None, settings: Settings, *, obj: str, token: Caller | None) -> None:
    """Delete every FGA tuple on ``obj``, a just-dropped object (the revoke counterpart of :func:`seed_ownership`).

    No-op when FGA is off or the client is unwired — so lifecycle endpoints call it with one line.
    Removes the owner grant, the ``parent`` edge, AND any later reader/writer/validator grants. Needs no token: the
    mutation was already authorized by :func:`authorize`, and the cleanup deletes every subject's tuple regardless of
    who.

    Revoke runs AFTER the (irreversible) native mutation, so on an OpenFGA outage it fails closed (503)
    with the tuples left stale until reconciled — the mutation itself already committed. The object is a uuid no
    other object will hold, so a stale tuple grants nothing on anything created later.
    """
    if not (settings.fga_enabled and client is not None):
        return
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
    root_uuid: str | None,
    removed: Sequence[GovernedObject],
    token: Caller | None,
) -> AsyncIterator[int]:
    """End what the namespace ``root_uuid`` and ``removed``, which a lifecycle delete removed, still hold, children first.

    Each object's protection and maintenance-policy records go, keyed by its uuid, then its grants. Yields each
    object's revoked count as its revoke lands, so a door reporting what it revoked counts every object revoked before a
    failure. Run ONLY ONCE EVERY OBJECT IN THE SUBTREE IS GONE ([[LH-495]]): a grant revoked before its object is
    removed strands the object, alive and with nobody left who may read, drop or recover it through a door.

    THE GRANTS GO LAST, BECAUSE THEY ARE WHAT FINDS AN OBJECT AGAIN. Every object created under a namespace leaves a
    ``child`` edge on it (``fga.hierarchy_edge_tuples``), and only that object's own revoke removes the edge. A
    re-issue lists only what still stands, so an edge still standing is what names an object a failure stopped before
    its end, and this walk ends it then. The edge names the object by its uuid ([[LH-524]]), which is the key its
    records are kept under, so an object found only by its edge loses its records as well as its grants. Each edge is
    followed before the object holding it is revoked. ``removed`` (the catalog's own listing) covers an object created
    before the edges existed; with FGA off nothing names an object once it is gone, and only the listed ones are ended.
    Every clear is idempotent, so a protection record is cleared without asking whether one exists: an object a forced
    attempt left unfinished may be re-issued without ``force``. A root whose uuid nothing records any more
    (``root_uuid`` is ``None``) has its children ended from ``removed`` alone.

    AN OBJECT IN THE TRASH KEEPS ITS RECORDS AND GRANTS. A recoverable drop keeps them for its undrop (#75), and a
    trashed table is undropped into its namespace once that namespace is created again, so they go only when its
    trash record does: at the trash purge, or with its warehouse (``catalog.services.warehouses.trash_owned_by``).
    """
    so = settings.storage_options()
    seen: set[str] = set()
    roots = [fga.namespace_object(root_uuid)] if root_uuid is not None else []
    in_trash: set[str] | None = None

    async def trashed(obj: str) -> bool:
        # Read once per walk, and only when an edge names an object the listing did not: a `child` edge names its
        # object by uuid, and the trash record names its object by the uuid its claim or its properties keep.
        nonlocal in_trash
        if in_trash is None:
            in_trash = await run_in_threadpool(_trashed_objects, settings)
        return obj in in_trash

    async def end(obj: str) -> AsyncIterator[int]:
        seen.add(obj)
        if settings.fga_enabled and client is not None and obj.startswith("namespace:"):
            for held in await fga.read_object_tuples(client, obj):
                if held.relation == "child" and held.user and held.user not in seen and not await trashed(held.user):
                    async for count in end(held.user):
                        yield count
        kind, _, uuid = obj.partition(":")
        await run_in_threadpool(object_records.end_object_records, settings.registry_root, so, kind, uuid)
        yield await revoke_removed_object(client, settings, obj, token=token)

    children_first = sorted(removed, key=lambda item: (item.kind == "namespace", -len(item.segments)))
    for obj in [*(item.fga_object() for item in children_first), *roots]:
        if obj not in seen:
            async for count in end(obj):
                yield count


async def orphaned_children(client: OpenFgaClient | None, settings: Settings, container: str, *, ended: set[str]) -> list[str]:
    """The uuids of the namespaces ``container``'s ``child`` edges name that ``ended`` does not, outside the trash.

    A lifecycle delete re-issued after a failure finds a namespace an earlier attempt removed by nothing but its
    container's edge: no listing names it and its uuid is recorded nowhere else ([[LH-524]]).
    """
    if not (settings.fga_enabled and client is not None):
        return []
    in_trash = await run_in_threadpool(_trashed_objects, settings)
    return sorted(
        held.user.removeprefix("namespace:")
        for held in await fga.read_object_tuples(client, container)
        if held.relation == "child" and held.user and held.user.startswith("namespace:") and held.user not in ended and held.user not in in_trash
    )


def _trashed_objects(settings: Settings) -> set[str]:
    """The FGA objects of every table and namespace in the trash, by the uuid each record keeps."""
    objects: set[str] = set()
    for record in trash.list_all(settings.registry_root, settings.storage_options()):
        if (uuid := trash.object_uuid(record)) is not None:
            objects.add(fga.object_of(str(record.get("kind") or "table"), uuid))
    return objects


async def seed_ownership_or_compensate(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    resolver: Resolver,
    *,
    resource: str,
    segments: list[str],
    uuid: str,
    undo: Callable[[], Awaitable[None]] | None,
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

    TWO ORDERING FACTS CARRY THIS:

    1. The revoke and the undo are INDEPENDENT best-effort steps, not one try block. The revoke goes through
       ``read_object_tuples`` — an OpenFGA call. So in the exact scenario the compensation exists for,
       a down OpenFGA, a revoke that raised would stop the native undo. The undo needs no FGA, so nothing
       FGA-shaped may stand in front of it.
    2. The revoke is attempted FIRST anyway, when it can run. A grant can commit server-side while its
       response is lost, and the uuid it names then holds tuples no object does, which drift detection reports.

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
            client, settings, token, resolver, resource=resource, segments=segments, uuid=uuid, parent_object=parent_object, may_grant_owner=may_grant_owner
        )
    except Exception:
        if undo is not None:
            await compensate_create(client, settings, token, resource=resource, segments=segments, uuid=uuid, undo=undo, reason="grant_failed")
        raise


async def compensate_create(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    resource: str,
    segments: list[str],
    uuid: str,
    undo: Callable[[], Awaitable[None]],
    reason: str,
) -> None:
    """Take back a create whose request is about to fail: revoke the ownership it seeded, then run ``undo``.

    The two steps are independent and best effort, revoke first, as :func:`seed_ownership_or_compensate` explains;
    nothing here raises, because the caller re-raises the error that made it compensate.
    """
    name = fga.canonical_object_id(segments)
    try:
        await revoke_ownership(client, settings, obj=owned_object(resource, uuid), token=token)
    except Exception as revoke_exc:
        # Expected when OpenFGA is the thing that is down. Logged, never fatal — the undo below
        # is what actually makes the retry converge, and it must not be skipped for this.
        log.warning("compensation_revoke_failed", extra={"object": name, "resource": resource, "error": str(revoke_exc)})
    try:
        await undo()
        log.warning("create_compensated", extra={"object": name, "resource": resource, "reason": reason})
    except Exception as undo_exc:
        # Both halves failed: the object is stranded and only the structural reconcile can
        # reach it. Loud on purpose — this is the state an operator has to be told about.
        log.error("create_compensation_failed", extra={"object": name, "resource": resource, "error": str(undo_exc)})


async def require_can_drop_table(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    table: ResolvedTable,
) -> None:
    """Raise 403 unless the caller holds owner-tier ``can_drop`` on ``table``, the table its name resolved to.

    The gate for a destructive create ``mode=Overwrite`` against an EXISTING table: Overwrite is spec-defined
    as drop-then-recreate (spec.yaml CreateTableRequest.mode), and it replaces the tip's schema and rows, so it
    must clear the same owner-tier bar a real ``drop_table`` does — NOT the writer-tier ``can_create_table`` on
    the parent that ``authorize`` already applied for a create. Without this, a mere namespace writer could
    replace another user's table. No-op when FGA is off / unwired / unauthenticated (the create path enforces
    those elsewhere)."""
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    label = _object("table", table.segments)
    await _require(client, user=token.subject, relation="can_drop", obj=fga.table_object(table.require_uuid()), label=label)


#: The rung a staged commit's operation needs at ``version/create``, which the router opens to a writer or a maintainer
#: (``can_create_version``, [[LH-471]]). Each is the rung the catalog's own door for that operation asks, so staging an
#: operation never clears a lower bar than doing it through the door: the writer operations are the writer rung, restore
#: is ``restore``'s owner rung, a compaction's two commits (the ``ReserveFragments`` and the ``Rewrite`` after it) are the
#: maintainer rung, and an Overwrite of a table that already has a version is drop-then-recreate
#: (``require_can_drop_table``). A table's first version is the writer rung whatever it is staged as
#: (:func:`require_staged_operation`).
_STAGED_OPERATION_RELATION: Final[dict[str, str]] = {
    **dict.fromkeys(("Append", "Delete", "Update", "Merge", "Project", "CreateIndex", "DataReplacement", "DataOverlay", "UpdateConfig"), "can_write_data"),
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

    ``creates`` is true for the table's first version, which writes the rows of a table only declared so far: it asks
    the writer rung, as every write does, since the router admits a maintainer too, and no protection record, since
    none holds a table with no version. An Overwrite of a table with a version is drop-then-recreate, so the
    protection record refuses it as it refuses a drop, with FGA on or off; ``force`` has no channel on the spec door.
    The rung check is a no-op when FGA is off / unwired / unauthenticated, as the other ``require_*`` gates are; an
    operation outside the map (``Clone`` among them, which the branch and clone doors own) is refused whatever FGA
    says.

    Raises:
        InvalidInputError: ``operation`` is not one a stock commit may stage here.
    """
    if operation not in _STAGED_OPERATION_RELATION:
        raise InvalidInputError(f"a {operation} cannot be committed through CreateTableVersion")
    relation = "can_write_data" if creates else _STAGED_OPERATION_RELATION[operation]
    replaces = operation == "Overwrite" and not creates
    canonical = fga.canonical_object_id(segments)
    governed = settings.fga_enabled and client is not None and token is not None
    if not replaces and not governed:
        return
    table_uuid = await run_in_threadpool(table_claims.uuid_at, table_claims.claim_store(settings), location, segments)
    if replaces:
        guard = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "table", table_uuid)
        require_not_protected(guard or {}, kind="table", obj_id=canonical, force=False)
    if not governed or client is None or token is None:
        return
    await _require(client, user=token.subject, relation=relation, obj=fga.table_object(table_uuid), label=f"table:{canonical}")


async def require_create_on_parent(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    resolver: Resolver,
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
    if (parent := _create_parent_rung(resource, segments, settings)) is not None:  # None => open top-level create (authn already enforced)
        await _require_rung(client, resolver, user=token.subject, rung=parent)


async def require_relation(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    relation: str,
    obj: str,
    label: str | None = None,
) -> None:
    """Gate on one ``relation`` over an explicit FGA object id, through the audited ``_require`` core.

    For handlers whose object is not table/namespace-shaped (e.g. the #3-A ``warehouse:<id>`` control
    plane) — funneling here instead of a bare ``fga.check`` keeps every deny/allow/outage on the #41 audit
    trail. ``label`` names the object in a refusal: a table's or namespace's name, never the uuid it is held by. No-op when
    FGA is off / unwired / unauthenticated (parity with the other ``require_*`` deps)."""
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    await _require(client, user=token.subject, relation=relation, obj=obj, label=label)


async def require_classifier_to_copy_a_label(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    table: table_claims.DoorTable,
    so: dict[str, str],
    branch: str | None,
    expressions: Sequence[str],
) -> None:
    """Refuse an ``add_columns`` or ``update`` expression that reads a classified column unless the caller holds
    ``can_classify`` on the table ([[LH-207]], owner 2026-10-26).

    Such an expression copies the values into a column with no label (measured on pylance 13.0.0:
    ``add_columns({"leak": "secret"})`` copies them unlabelled), which a writer could then vend raw. This narrows the
    copy rather than closing it: a writer reads the column too, so a copy made outside the catalog is LH-288's.
    No-op when FGA is off / unwired / unauthenticated, like every gate here.
    """
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    dataset = await run_in_threadpool(partial(table.open, so, branch=branch))
    try:
        read = await run_in_threadpool(dataplane.expressions_reading_a_label, dataset, list(expressions))
    except OSError:
        # Labels that cannot be read are not "no labels": the copy is judged as reading one.
        log.warning("copy_labels_unreadable", extra={"sub": token.subject}, exc_info=True)
        read = ["<labels unreadable>"]
    if read:
        log.info("copy_of_a_label_asks_can_classify", extra={"sub": token.subject, "expressions": read})
        await _require(
            client,
            user=token.subject,
            relation="can_classify",
            obj=fga.table_object(table.resolved.require_uuid()),
            label=fga.canonical_object_id(table.segments),
        )


async def require_classifier_to_set_labels(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    obj: str,
    label: str,
    labels: Mapping[str, Mapping[str, str]],
) -> None:
    """Refuse fields arriving with ``rask.*`` metadata unless the caller holds ``can_classify`` on ``obj`` and ``can_apply``
    on each value, as a label set later through ``update_field_metadata`` is asked ([[LH-207]]).

    A label decides how a table's bytes leave the store, so a create may not set one its caller could not set after.
    ``obj`` is the table, or for a table not made yet its namespace, whose ``classifier`` its tables inherit. No-op when
    there are no labels or FGA is off / unwired / unauthenticated, like every gate here.
    """
    if not labels or not (settings.fga_enabled and client is not None and token is not None):
        return
    await _require(client, user=token.subject, relation="can_classify", obj=obj, label=label)
    for value in sorted({value for keys in labels.values() for value in keys.values()}):
        await _require(client, user=token.subject, relation="can_apply", obj=f"classification:{value}")


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
