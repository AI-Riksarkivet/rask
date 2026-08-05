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

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    InvalidInputError,
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

from catalog.api.dependencies import SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier
from catalog.services import native
from service_kit.governed import fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, audit
from service_kit.governed.oidc import IDToken


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
        # #72 grant/revoke: MUTATING the ACL is the highest-privilege act on the object — strictly above
        # a writer, gated at the owner bar (same as reviewing it). Without this mapping these would fall
        # through to the writer tier, letting a plain data writer hand out ownership.
        "access/grant": "can_drop",
        "access/revoke": "can_drop",
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
        "access/grant": "can_delete",
        "access/revoke": "can_delete",
        "access/graph": "can_delete",
    },
}

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
) -> None:
    """Grant the creator ``owner`` + the ``parent`` edge on a just-created object.

    No-op when FGA is off, the request is unauthenticated, or the client is unwired — so
    create endpoints call it unconditionally with one line. This is the ONE place the
    post-create seed policy lives (previously copy-pasted across four endpoint modules).
    Ids come from ``fga`` so this grant and the later :func:`authorize` check agree
    byte-for-byte.
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
        parent_object=fga.parent_object(resource, segments, delimiter=settings.delimiter, root_object=settings.fga_root_object),
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
        log.info("fga_tuples_revoked", extra={"object": obj, "removed": removed})


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
    await fga.write_tuples(
        client,
        [
            fga.ClientTuple(user=f"user:{token.sub}", relation="owner", object=obj),
            fga.ClientTuple(user=f"project:{project}", relation="project", object=obj),
        ],
        actor=token.sub,
        origin="warehouse_create",
    )
