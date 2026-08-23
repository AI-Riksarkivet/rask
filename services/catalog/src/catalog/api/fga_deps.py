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

Create-on-parent: creating a child authorizes the parent (the child has no id yet); a
top-level child gates on the catalog root only when ``fga_lock_root_create`` is set,
else it is open self-serve. Batch routes read the (Starlette-cached) body and check each
named table. Fail-closed twice over: an unwired client raises 503 here, and
``fga.check``/``fga.batch_check`` raise 503 on an OpenFGA outage rather than allowing.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import lru_cache

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    InvalidInputError,
    LanceNamespace,
    NamespaceAlreadyExistsError,
    NamespaceExistsRequest,
    NamespaceNotEmptyError,
    NamespaceNotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableAlreadyExistsError,
    TableNotFoundError,
    UnauthenticatedError,
    UnsupportedOperationError,
)
from openfga_sdk import OpenFgaClient

from catalog.api.dependencies import SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.identifiers import MAX_NAMESPACE_DEPTH, parse_identifier
from catalog.services import native
from service_kit.governed import fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, audit
from service_kit.governed.oidc import IDToken
from service_kit.lakehouse import trash


log = logging.getLogger(__name__)

# Guarded resource prefixes, in match order. Each maps to its own dedicated FGA type in the model
# (``service_kit/governed/auth/model.fga``): ``materialized_view`` and ``transaction`` are no longer aliased
# to ``table``. A transaction is authorized PARENT-SCOPED against its enclosing namespace (see
# ``_authorize_transaction``) — the old ``table:<txn-id>`` alias targeted an object nothing ever seeds,
# so every transaction op always-denied.
_RESOURCES: tuple[str, ...] = ("namespace", "table", "materialized_view", "transaction")
_FGA_TYPE: dict[str, str] = {
    "namespace": "namespace",
    "table": "table",
    "materialized_view": "materialized_view",
    "transaction": "transaction",
}

# Read-only trailing actions (reader rung). ``query``/``count_rows`` read DATA; the rest
# read metadata — split so list_objects(can_read_data) is meaningful (both are reader).
# ``credentials`` (vending) is a DATA read at minimum — the router guard requires can_read_data; the
# endpoint additionally requires can_write_data for a write-tier vend.
# ``blobs`` (GET /v1/table/{id}/blobs — the credential-less blob serving path) returns raw payload
# bytes, so it is a DATA read exactly like ``query``.
_DATA_READ_ACTIONS = frozenset({"query", "count_rows", "credentials", "blobs"})
# `tasks` (#75) reports what is SCHEDULED against this object — a pending undrop deadline today.
# Reader tier: it discloses no data and no principals, and an owner must be able to see the
# deadline they are racing. Unmapped it would fall to WRITER, which is how the live audit found it.
_META_READ_ACTIONS = frozenset({"describe", "exists", "list", "stats", "explain_plan", "analyze_plan", "version", "tasks"})

# Full route suffixes that create a NEW child -> authorize the parent (create-on-parent).
_CREATE_ON_PARENT_SUFFIXES: dict[str, frozenset[str]] = {
    "table": frozenset({"create", "declare", "register"}),
    "namespace": frozenset({"create"}),
    "materialized_view": frozenset({"create"}),
}

# Full route suffixes that are lifecycle/destructive/ref-plane on the object itself (owner rung).
# Matched on the FULL suffix so ``index/{name}/drop`` / ``version/delete`` stay writer-tier.
# ``rename`` is owner-tier: it DESTROYS the source id (revokes its tuples) and re-seeds the caller as owner
# of the destination, so a writer-tier gate would let a non-owner rename another user's table and seize sole
# ownership (the same escalation closed for Overwrite via require_can_drop_table). Owner-gate the source.
# ``restore`` REWINDS the table to an older version (destructive); ``branches/create`` / ``tags/create`` /
# ``tags/update`` FORK or re-point the ref-plane — all privileged history ops kept strictly above the
# writer rung (a plain data writer appends/overwrites within the current line only). Their ``*/delete`` /
# ``*/list`` / ``*/version`` siblings fall through to the reader/writer tiers below.
_OWNER_SUFFIX_RELATION: dict[str, dict[str, str]] = {
    "table": {
        "drop": "can_drop",
        "deregister": "can_deregister",
        "rename": "can_drop",
        "restore": "can_restore",
        "branches/create": "can_create_branch",
        "tags/create": "can_create_tag",
        "tags/update": "can_update_tag",
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
        # same owner bar. An unmapped suffix would fall through to writer-tier — never leave it unset.
        "access/check": "can_drop",
        # The SELF-view is the one access route that must NOT clear the owner bar. `access/list` and
        # `access/check` disclose principals; "what may I do here" discloses only the caller to
        # themselves. Owner-gating it would mean only the people who already know the answer could
        # ask — which is precisely why no surface over these primitives ever shipped. Reader tier:
        # you may ask about anything you can see. Mapped EXPLICITLY, because the fall-through for an
        # unmapped table suffix is the writer rung, which would be both wrong and silently wrong.
        "access/my-permissions": "can_get_metadata",
        # NOTE: `access/grant` and `access/revoke` are NOT here. They gated on the owner bar until the
        # grant axis landed, which meant handing out access required owning the data. They are now
        # authorized per-rung from the BODY (`can_grant_<relation>`) by `_authorize_grant`, which
        # intercepts before `_action_relation` is consulted — a mapping here would be dead code that
        # reads like the live rule.
        # #81 authorization graph: one hop of the relationship graph discloses principals + the cascade,
        # the same disclosure as access/list — owner bar.
        "access/graph": "can_drop",
        # #75/#76 on-demand maintenance: previewing/reclaiming version history + compacting are the drop
        # rung, like the retention policy that schedules them (an unmapped suffix would fall to writer).
        "maintenance/preview": "can_drop",
        "maintenance/run": "can_drop",
        "maintenance/compact": "can_drop",
        # #73 deletion protection: arming/disarming the safety on an object is a statement about its
        # DESTRUCTION, so it clears the same owner bar as the drop it guards — a writer must not be
        # able to disarm protection they could never act on. (An unmapped suffix would fall through
        # to writer-tier — never leave it unset.)
        "protection": "can_drop",
        # #75 undrop RESTORES the object into its namespace — the same authority as removing it.
        "undrop": "can_drop",
        # Publication ADVANCES the `published` tag, so it clears the same bar as `tags/update` — the
        # rung that already exists for re-pointing the ref plane. Anything lower would make `publish`
        # a way for a plain data writer to move a tag they are not allowed to move directly, which is
        # the escalation the ref-plane rungs exist to close. The quality gate is orthogonal: it
        # decides whether the DATA may be published, this decides whether the CALLER may.
        "publish": "can_update_tag",
    },
    "namespace": {
        "drop": "can_delete",
        # #96 undrop RESTORES the subtree into the estate — the same authority as removing it (the
        # table door's #75 rule at the namespace rung; unmapped it would fall to writer tier).
        "undrop": "can_delete",
        "protection": "can_delete",
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
        # from `manage_grants`. Mapped explicitly because the fall-through is `can_update_properties`
        # (writer), which would let any writer switch off the control that stops owners widening access.
        "managed-access/set": "can_set_managed_access",
        # The read sits at the READER bar deliberately: this flag is the REASON someone's grant controls
        # are missing, so the person who most needs the answer is exactly the one who may not change it.
        # Gating the read at the write's bar turns a stated policy back into an unexplained absence.
        "managed-access/describe": "can_get_metadata",
        # The self-view sits at the READER bar here too — see the table block for why. Spelled out
        # per type rather than defaulted: an unmapped namespace suffix falls to `can_update_properties`
        # (writer), so omitting this line would let the page ask "what may I do?" only of people who
        # can already write, which is the inverse of the question.
        "access/my-permissions": "can_get_metadata",
        "access/graph": "can_delete",
    },
}

# Grant/revoke are authorized from the BODY, per rung, by `_authorize_grant` — they appear in neither
# block above on purpose. `authorize` intercepts these suffixes before `_action_relation` runs, so a
# mapping here would never be consulted while reading exactly like the live rule.
_GRANT_SUFFIXES = frozenset({"access/grant", "access/revoke"})

# Body-keyed batch routes (no ``{id}`` path param); the tables are named in the body.
_BATCH_PATHS = frozenset({"/v1/table/version/batch-create", "/v1/table/batch-commit"})

# Destructive batch-commit operations require the owner tier — same as their dedicated
# routes — so a writer can't deregister a table by wrapping it in a batch-commit (the
# generic writer batch_check would otherwise pass). The wire model (CommitTableOperation)
# has no drop op, so deregister_table is the only destructive batch kind. Maps op key ->
# can_* relation.
_BATCH_OWNER_OPS: dict[str, str] = {"deregister_table": "can_deregister"}


def _resource_for(path: str) -> str | None:
    """The guarded resource a ``/v1/<resource>/…`` path belongs to, else ``None``."""
    for resource in _RESOURCES:
        if path.startswith(f"/v1/{resource}/"):
            return resource
    return None


def _suffix(path: str, resource: str, object_id: str) -> str:
    """The route part after ``/v1/<resource>/{id}/`` (e.g. ``version/create``)."""
    clean = path.rstrip("/")
    prefix = f"/v1/{resource}/{object_id}/"
    return clean.removeprefix(prefix) if clean.startswith(prefix) else ""


def _object(fga_type: str, id_segments: list[str] | str, delimiter: str) -> str:
    """``<type>:<canonical-id>`` for an object (e.g. ``table:db1$users``)."""
    return f"{fga_type}:{fga.canonical_object_id(id_segments, delimiter=delimiter)}"


def _action_relation(fga_type: str, suffix: str) -> str:
    """The ``can_*`` relation to check for a non-create op, by object type + route suffix.

    Owner-tier matches the FULL suffix; reader-tier matches the trailing segment; everything
    else is writer-tier. Every returned relation exists on ``fga_type`` in the model and
    reduces to the same rung the previous reader/writer/owner code used.
    """
    owner = _OWNER_SUFFIX_RELATION.get(fga_type, {})
    if suffix in owner:
        return owner[suffix]
    action = suffix.rsplit("/", 1)[-1] if suffix else ""
    if fga_type == "namespace":
        if action in _META_READ_ACTIONS or action in _DATA_READ_ACTIONS:
            return "can_get_metadata"
        return "can_update_properties"
    if fga_type == "materialized_view":
        # Only ``create`` (create-on-parent, handled elsewhere) and ``refresh`` reach a view. A read maps
        # to can_read/can_get_metadata; ``refresh`` is the async rematerialize (writer-tier can_refresh).
        if action in _DATA_READ_ACTIONS:
            return "can_read"
        if action in _META_READ_ACTIONS:
            return "can_get_metadata"
        return "can_refresh"
    # table type (transaction is authorized parent-scoped by _authorize_transaction, never here)
    if action in _DATA_READ_ACTIONS:
        return "can_read_data"
    if action in _META_READ_ACTIONS:
        return "can_get_metadata"
    return "can_write_data"


def _create_parent_check(resource: str, id_segments: list[str] | str, settings: Settings) -> tuple[str, str] | None:
    """``(parent_object, can_create_* relation)`` for a create-on-parent op, or ``None`` to allow.

    Nested child -> its parent ``namespace:<parent>``. Top-level child -> the catalog root
    object only when ``fga_lock_root_create`` is set; otherwise ``None`` (open self-serve
    top-level create — preserves the default behaviour). The relation is named after the child
    being created (``can_create_namespace`` / ``can_create_materialized_view`` / ``can_create_table``),
    all of which reduce to the writer rung on the parent namespace.
    """
    relation = {
        "namespace": "can_create_namespace",
        "materialized_view": "can_create_materialized_view",
    }.get(resource, "can_create_table")
    parent_id = fga.parent_namespace_id(id_segments, delimiter=settings.delimiter)
    if parent_id is not None:
        return f"namespace:{parent_id}", relation
    if settings.fga_lock_root_create:
        return settings.fga_root_object, relation
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
    Revoke now goes to ``can_revoke_grant``, which is ``manage_grants`` alone.

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

    The userset form is covered too, and has to be: refusing only a literal ``user:<sub>`` would leave
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

    Reads the grantee the same way :func:`~catalog.api.v1.endpoints.access._access_mutate` does — a
    bare id is a user, a qualified string is passed through — because a guard that resolved it
    DIFFERENTLY from the code that writes the tuple would be guarding a grant nobody makes.

    Order matters: this runs AFTER the ``can_grant_*`` check, so a caller who may not grant the rung
    at all is refused for that reason and never learns whether the self-grant rule would also have
    caught them.
    """
    if not isinstance(grantee, str) or not grantee:
        return  # not a grant this guard can reason about; the endpoint's own validation owns the shape
    subject = f"user:{user}"
    resolved = grantee if ":" in grantee else f"user:{grantee}"
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
    for obj, relation in owner_checks:  # rare; per-object keeps the per-op relation exact
        ok = await fga.check(client, user=user, relation=relation, obj=obj)
        audit(relation, ALLOW if ok else DENY, subject=user, resource=obj)
        if not ok:
            denied.append(obj)
    if denied:
        log.info("access_denied", extra={"sub": user, "objects": sorted(denied)})
        raise PermissionDeniedError(f"permission denied on {', '.join(sorted(denied))}")


async def authorize(request: Request, settings: SettingsDep, token: CurrentToken) -> None:
    """Enforce the caller's OpenFGA permission for this route (no-op when FGA is off)."""
    if not settings.fga_enabled:
        return
    # Fail closed: enabled but unwired authz must never silently allow the request.
    client = getattr(request.app.state, "fga", None)
    if client is None:
        raise ServiceUnavailableError("authorization service is not available")
    if token is None:
        raise UnauthenticatedError("authentication required")

    # The raw ASGI path (what routing actually matched) — NOT request.url.path, which re-parses the
    # decoded path as a URL string and truncates at a decoded '#'/'?' inside an id, collapsing
    # owner-tier suffixes (drop/deregister) to the generic writer tier for exotically-named tables.
    path: str = request.scope["path"]
    object_id = request.path_params.get("id")

    # Body-keyed batch routes have no {id} path param — authorize by reading the body.
    if object_id is None:
        if path.rstrip("/") in _BATCH_PATHS:
            await _authorize_batch(request, client, settings, user=token.sub)
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
            await _require(client, user=token.sub, relation=check[1], obj=check[0])
        return

    # Transactions authorize parent-scoped (against their enclosing namespace), not on a per-txn object.
    if resource == "transaction":
        await _authorize_transaction(client, settings, segments, suffix, user=token.sub)
        return

    fga_type = _FGA_TYPE[resource]
    # Grant/revoke authorize on the RUNG BEING HANDED OUT, which lives in the body — so, like the
    # batch routes above, this one cannot be answered from the path alone.
    if suffix in _GRANT_SUFFIXES:
        await _authorize_grant(request, client, settings, user=token.sub, fga_type=fga_type, segments=segments, revoking=suffix == "access/revoke")
        return
    relation = _action_relation(fga_type, suffix)
    obj = _object(fga_type, segments, settings.delimiter)
    await _require(client, user=token.sub, relation=relation, obj=obj)


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
    (``POST /v1/warehouses/{warehouse_id}/namespaces``) binds the new namespace to a real bucket, and
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
        f"Create it through its warehouse — POST /v1/warehouses/{{warehouse_id}}/namespaces with "
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

    Refusal is ``NamespaceNotEmptyError`` (spec code 3 → HTTP 409) — the spec's own "this container will
    not be deleted right now" error, reused rather than minting a status the client SDKs do not map.
    """
    if force or (record.get("protected") or "false").lower() != "true":
        return
    raise NamespaceNotEmptyError(
        f"{kind} '{obj_id}' is protected against deletion. Pass force=true to override "
        f"(protection only — the same authorization is still required), or clear the flag first."
    )


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
            f"(project > warehouse > namespace > table) — create it first: POST /v1/projects "
            f'with {{"id": "{project}"}}.'
        )


async def seed_project_admin(
    client: OpenFgaClient | None,
    settings: Settings,
    token: IDToken | None,
    *,
    project: str,
) -> bool:
    """Grant the creator ``admin`` on the new ``project:<id>`` — the tuple that makes a tenant
    self-sustaining (every later warehouse/namespace/table grant cascades from it).

    Written by ``POST /v1/projects`` in the same operation as the registry record, which is the whole
    point of Decision 1: existence and permission are minted together and cannot drift. Idempotent —
    ``write_tuples`` swallows duplicate writes on a create retry. Returns True when the tuple was
    actually written, so the caller audits a grant that really happened (never one an FGA-off stack
    silently skipped)."""
    if not (settings.fga_enabled and token is not None and client is not None):
        return False
    await fga.write_tuples(
        client,
        [fga.ClientTuple(user=f"user:{token.sub}", relation="admin", object=f"project:{project}")],
        actor=token.sub,
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
    token: IDToken | None,
    *,
    resource: str,
    segments: list[str],
    parent_object: str | None = None,
) -> None:
    """Grant the creator ``owner`` + the ``parent`` edge on a just-created object.

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
    """
    if not (settings.fga_enabled and token is not None and client is not None):
        return
    await fga.grant_on_create(
        client,
        user_sub=token.sub,
        resource=resource,
        obj_id=fga.canonical_object_id(segments, delimiter=settings.delimiter),
        actor=token.sub,
        origin="create",
        parent_object=parent_object or fga.parent_object(resource, segments, delimiter=settings.delimiter, root_object=settings.fga_root_object),
    )


async def revoke_ownership(
    client: OpenFgaClient | None,
    settings: Settings,
    *,
    resource: str,
    segments: list[str],
    token: IDToken | None,
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
    actor = token.sub if token is not None else "system:catalog"
    removed = await fga.revoke_object_tuples(client, obj, actor=actor, origin="create")
    if removed:
        log.info("fga_tuples_revoked", extra={"object": obj, "removed": len(removed)})


async def seed_ownership_or_compensate(
    client: OpenFgaClient | None,
    settings: Settings,
    token: IDToken | None,
    *,
    resource: str,
    segments: list[str],
    undo: Callable[[], Awaitable[None]] | None,
    parent_object: str | None = None,
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
        await seed_ownership(client, settings, token, resource=resource, segments=segments, parent_object=parent_object)
    except Exception:
        if undo is not None:
            obj = fga.canonical_object_id(segments, delimiter=settings.delimiter)
            try:
                await revoke_ownership(client, settings, resource=resource, segments=segments, token=token)
            except Exception as revoke_exc:
                # Expected when OpenFGA is the thing that is down. Logged, never fatal — the undo below
                # is what actually makes the retry converge, and it must not be skipped for this.
                log.warning("compensation_revoke_failed", extra={"object": obj, "resource": resource, "error": str(revoke_exc)})
            try:
                await undo()
                log.warning("create_compensated", extra={"object": obj, "resource": resource, "reason": "grant_failed"})
            except Exception as undo_exc:
                # Both halves failed: the object is stranded and only the structural reconcile can
                # reach it. Loud on purpose — this is the state an operator has to be told about.
                log.error("create_compensation_failed", extra={"object": obj, "resource": resource, "error": str(undo_exc)})
        raise


async def require_can_drop_table(
    client: OpenFgaClient | None,
    settings: Settings,
    token: IDToken | None,
    *,
    segments: list[str],
) -> None:
    """Raise 403 unless the caller holds owner-tier ``can_drop`` on the table at ``segments``.

    The gate for a destructive create ``mode=Overwrite`` against an EXISTING table: Overwrite is spec-defined
    as drop-then-recreate (lance namespace.md), so it must clear the same owner-tier bar a real ``drop_table``
    does — NOT the writer-tier ``can_create_table`` on the parent that ``authorize`` already applied for a
    create. Without this, a mere namespace writer could overwrite (destroy) another user's table and — via the
    ownership reset that follows — seize sole ownership of it. No-op when FGA is off / unwired /
    unauthenticated (the create path enforces those elsewhere)."""
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    obj = f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    await _require(client, user=token.sub, relation="can_drop", obj=obj)


async def require_can_promote(
    client: OpenFgaClient | None,
    settings: Settings,
    token: IDToken | None,
    *,
    segments: list[str],
) -> None:
    """Raise 403 unless the caller holds the VALIDATOR-rung ``can_promote`` on the table at ``segments``.

    The gate for #17 model promotion (candidate→blessed): moving the ``blessed`` tag on ``models$<model>`` is
    a promotion, which the FGA model separates from a plain WRITER — ``can_promote`` reduces to ``validator``
    (a writer is NOT a validator), exactly as the silver→gold stage promotion is gated. The ``/v1/model/*``
    routes sit under the router-level ``authorize`` (its 401/503 pre-path checks still apply) but match no
    ``_RESOURCES`` prefix, so ``authorize`` makes no per-object decision there — the
    promote endpoint calls this EXPLICITLY. No FGA-model change is needed: ``table.can_promote: validator``
    already exists and the trainer seeds the per-model ``namespace:models → table:models$<model>`` parent
    link, so a ``validator namespace:models`` grant cascades. Fail-closed: 403 on deny, 503 on an OpenFGA
    outage (via ``_require``/``fga.check``). No-op when FGA is off / unwired / unauthenticated."""
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    obj = f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    await _require(client, user=token.sub, relation="can_promote", obj=obj)


async def require_can_get_metadata(
    client: OpenFgaClient | None,
    settings: Settings,
    token: IDToken | None,
    *,
    segments: list[str],
) -> None:
    """Raise 403 unless the caller holds reader-tier ``can_get_metadata`` on the table at ``segments``.

    The read gate for routes whose path matches no ``_RESOURCES`` prefix (so the router-level ``authorize``
    contributes only its 401/503 pre-path checks, no per-object decision) — notably the #17 model describe
    route (``/v1/model/*``): reading a model's blessed pointer + metrics is a metadata read
    (reader rung), so a reader may see it while only a validator may promote. Fail-closed like every other
    gate. No-op when FGA is off / unwired / unauthenticated."""
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    obj = f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    await _require(client, user=token.sub, relation="can_get_metadata", obj=obj)


async def require_create_on_parent(
    client: OpenFgaClient | None,
    settings: Settings,
    token: IDToken | None,
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
        await _require(client, user=token.sub, relation=check[1], obj=check[0])


async def require_relation(
    client: OpenFgaClient | None,
    settings: Settings,
    token: IDToken | None,
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
    await _require(client, user=token.sub, relation=relation, obj=obj)


async def require_can_create_warehouse(
    client: OpenFgaClient | None,
    settings: Settings,
    token: IDToken | None,
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
    creates the PROJECT explicitly (``POST /v1/projects``, which seeds its admin), and
    ``require_project_exists`` refuses a warehouse for an unknown tenant BEFORE this gate runs — so
    by the time we are here, the project exists and has admins, and one door is enough."""
    if not (settings.fga_enabled and client is not None and token is not None):
        return
    await _require(client, user=token.sub, relation="can_create_warehouse", obj=f"project:{project}")


async def seed_warehouse(
    client: OpenFgaClient | None,
    settings: Settings,
    token: IDToken | None,
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
    written by ``POST /v1/projects`` — a tenant's admin is minted with the tenant, not with its
    first warehouse."""
    if not (settings.fga_enabled and token is not None and client is not None):
        return
    obj = f"warehouse:{warehouse_id}"
    # The CASCADE's own identities, granted here and not per tier. `publish` is guarded by
    # `can_update_tag` and the model defines `can_update_tag: owner`, so a mover without this cannot
    # promote — the tenant is created, its tiers are created, rows land, lineage records the run, and
    # the promotion is refused with a 403 in a log nobody is watching.
    #
    # At the WAREHOUSE because `namespace` and `table` both define `owner ... or owner from parent`:
    # one tuple reaches every tier and every table under it, instead of three-plus per tenant that the
    # hierarchy already implies. Written in the SAME call as the creator's grant so there is no window
    # in which the warehouse exists and its cascade cannot publish into it.
    await fga.write_tuples(
        client,
        [
            fga.ClientTuple(user=f"user:{token.sub}", relation="owner", object=obj),
            *cascade_tuples(settings, warehouse_id=warehouse_id, project=project),
        ],
        actor=token.sub,
        origin="warehouse_create",
    )


def cascade_tuples(settings: Settings, *, warehouse_id: str, project: str) -> list[fga.ClientTuple]:
    """The tuples a warehouse needs to be REACHABLE — its project edge, plus the cascade's own owners.

    Shared by the create path and the backfill so the two cannot drift. That is the whole reason it is
    a function: a backfill that writes a different set from the one creates write is worse than no
    backfill, because the estate then has two populations of warehouse that differ in a way nothing
    reports.

    The creator's own grant is deliberately NOT here. It is the one tuple a backfill must never write
    — re-running the create path over an existing estate to repair it would make whoever ran the
    repair an owner of every tenant.
    """
    obj = f"warehouse:{warehouse_id}"
    return [
        fga.ClientTuple(user=f"project:{project}", relation="project", object=obj),
        *(fga.ClientTuple(user=subject, relation="owner", object=obj) for subject in settings.fga_cascade_writers),
    ]


async def backfill_cascade_grants(
    client: OpenFgaClient | None,
    settings: Settings,
    *,
    warehouse_id: str,
    project: str,
    actor: str,
) -> int:
    """Re-assert :func:`cascade_tuples` over a warehouse that ALREADY exists. Returns tuples submitted.

    Why this is needed at all, given creates already seed: the grants are written ONCE, at create, from
    a config value that CHANGES. Adding a mover to `medallion.movers` extends
    ``LANCE_FGA_CASCADE_WRITERS``, and every warehouse that predates the change is missing the new
    subject — so the new mover cannot publish into any existing tenant, and the failure is a 403 at
    promotion time in a log nobody is watching. Measured on this estate: `user:service-silver-to-gold`
    held `owner` on NEITHER warehouse:acme-bucket NOR warehouse:research-bucket, both created before
    the setting existed.

    Idempotent by the same mechanism the create path relies on: ``write_tuples`` retries one-by-one
    when OpenFGA rejects a batch for containing a duplicate, so a re-run lands only what is missing.
    It inherits fail-closed too — an unreachable OpenFGA raises rather than reporting success, because
    a backfill that silently wrote nothing is indistinguishable from one that had nothing to do.
    """
    if not (settings.fga_enabled and client is not None):
        return 0
    tuples = cascade_tuples(settings, warehouse_id=warehouse_id, project=project)
    await fga.write_tuples(client, tuples, actor=actor, origin="cascade_backfill")
    return len(tuples)
