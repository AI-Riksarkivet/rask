"""Fine-grained authorization via OpenFGA (Zanzibar-style relationship checks).

The authorization model (``service_kit/governed/auth/model.fga`` / ``model.json``) defines TEN types —
``user``, ``team`` (with ``team#member`` as a group subject), ``role`` (``role#assignee``), ``project``,
``warehouse`` (the S3-bucket root), a self-nesting ``namespace``, ``table``, ``materialized_view`` and
``transaction`` (both hanging off namespace/warehouse and inheriting the same rungs via ``parent``), and
``annotation_project`` (the annotator's own rung, tenanted by ``project``). This said "nine" and listed
nine while the model declared ten — the count is worth keeping honest because it is the first thing a
reader checks this docstring against.
Privileges are concentric (``owner`` ⊇ ``writer`` ⊇ ``reader``, plus a separate ``validator`` rung gating
``can_promote``) and cascade DOWN the hierarchy via ``parent`` (team → project → warehouse → namespace →
nested namespace → table / materialized_view / transaction). Every API operation is checked against a
``can_*`` ACTION relation — the model owns the op→privilege map, the app just names the action (see
``services/catalog/api/fga_deps.py``). Tuples persist in the OpenFGA datastore (Postgres in the deployed
stack; SQLite for the auth-e2e).

Resilience: ``check`` / ``batch_check`` / ``list_objects`` / ``list_users`` / ``expand`` and the
post-create grant writes go through a bounded retry with exponential backoff + jitter (tenacity). Only
*transient* failures are retried — OpenFGA 429/5xx AND the dominant outage modes raised
by the aiohttp transport (connection refused / DNS / read timeout, i.e.
``aiohttp.ClientError`` / ``TimeoutError`` / ``OSError``). A definitive ``allowed=false``
is never retried. The per-request timeout is carried by the client itself
(``ClientConfiguration.timeout_millisec``, set in ``make_client``) and the SDK's own
retry layer is disabled there so tenacity is the single authoritative one. When retries
are exhausted (OpenFGA outage) we **fail closed** and raise ``ServiceUnavailableError``
so the request surfaces as a 503 rather than a 500 — and never as a silent allow/deny.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiohttp
from lance_namespace import ServiceUnavailableError
from openfga_sdk import ClientConfiguration, OpenFgaClient
from openfga_sdk.client.models import (
    ClientBatchCheckItem,
    ClientBatchCheckRequest,
    ClientCheckRequest,
    ClientExpandRequest,
    ClientListObjectsRequest,
    ClientTuple,
    ClientWriteRequest,
)
from openfga_sdk.client.models.list_users_request import ClientListUsersRequest
from openfga_sdk.client.models.read_changes_request import ClientReadChangesRequest
from openfga_sdk.configuration import RetryParams
from openfga_sdk.exceptions import ApiException
from openfga_sdk.models.create_store_request import CreateStoreRequest
from openfga_sdk.models.fga_object import FgaObject
from openfga_sdk.models.read_request_tuple_key import ReadRequestTupleKey

# Re-exported deliberately, in the `X as X` form so ruff does not prune it as unused: a caller building
# a CONDITIONAL tuple needs this type, and importing it straight from the SDK would spread openfga_sdk
# imports back into the services this module exists to keep SDK-free (`ClientTuple` is re-exported for
# the same reason). The plain `import X` form was removed by `ruff --fix` once — it cannot see that the
# only consumer reaches it as `fga.RelationshipCondition` from another module.
from openfga_sdk.models.relationship_condition import RelationshipCondition as RelationshipCondition
from openfga_sdk.models.user_type_filter import UserTypeFilter
from openfga_sdk.models.write_authorization_model_request import WriteAuthorizationModelRequest
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential_jitter,
)

from service_kit.governed.audit import SUCCESS, audit


log = logging.getLogger(__name__)

_MODEL_PATH = Path(__file__).resolve().parent / "auth" / "model.json"
_MODEL_DSL_PATH = _MODEL_PATH.with_name("model.fga")

# Default resilience knobs. The real values are sourced from Settings and passed
# into make_client / check / grant_on_create; these mirror the config defaults so
# the module is usable (e.g. in tests) without a Settings instance.
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.2
DEFAULT_RETRY_MAX_BACKOFF_SECONDS = 2.0
# Overall wall-clock budget for one logical call's retries, so a flapping OpenFGA
# (whose own backoff can be long) cannot pin a request worker for minutes.
DEFAULT_RETRY_OVERALL_BUDGET_SECONDS = 15.0

# The async (aiohttp) OpenFGA client raises a down/unreachable server as a raw transport
# error — NOT openfga_sdk.ApiException — so these must be retried AND caught by the
# fail-closed handlers, otherwise the single most likely outage escapes as a 500.
# (asyncio.TimeoutError is builtin TimeoutError on 3.11+; ClientConnectorError ⊂ OSError.)
_TRANSIENT_NETWORK: tuple[type[BaseException], ...] = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    OSError,
)
# Every exception class the read/write paths fail CLOSED on (→ 503), never propagate.
_FAIL_CLOSED: tuple[type[BaseException], ...] = (ApiException, *_TRANSIENT_NETWORK)

# OpenFGA surfaces a duplicate-tuple write as a 400 ValidationException whose body
# mentions that the tuple already exists. We treat that as success (idempotent
# grant), since the desired post-condition — the tuple is present — already holds.
_DUPLICATE_WRITE_MARKERS = ("already exists", "write_failed_due_to_invalid_input")

# The symmetric case on the revoke path: deleting an already-absent tuple (a concurrent revoke)
# surfaces as the same 400 validation error. Treat it as success — the tuple being gone IS the goal.
_MISSING_DELETE_MARKERS = ("cannot delete", "does not exist", "write_failed_due_to_invalid_input")

# Reading every tuple on one object (revoke-on-delete) is paginated; bound the page loop so a
# pathological store / continuation-token bug can't spin forever. One object's grants are few, so
# this ceiling is never reached in practice.
_MAX_REVOKE_PAGES = 100

# Parent types that declare a `child` relation in the model, and therefore take the inverse edge
# `grant_on_create` writes for upward visibility. Deliberately a whitelist keyed on the PARENT's
# type: `project` is absent because `annotation_project` hangs off it by `tenant` and the model
# gives `project` no `child`, and a `child` tuple on a type that does not define it is accepted by
# OpenFGA and read by no rule — a silent no-op, the same failure mode as writing `parent` where the
# model says `tenant`. Adding a `child` relation to a type in `model.fga` means adding it here too.
_CHILD_EDGE_PARENT_TYPES = frozenset({"warehouse", "namespace"})


def hierarchy_edge_tuples(*, child_object: str, parent_object: str, parent_relation: str = "parent") -> list[ClientTuple]:
    """BOTH directions of one parent→child link, so a caller cannot write half of it.

    OpenFGA cannot walk a tuple backwards, so `can_get_metadata: reader or can_get_metadata from
    child` needs the inverse edge STORED. `grant_on_create` has always written both; every other
    writer has written only `parent`, and each one of those is an object whose upward visibility is
    silently dead — the grantee can read the table and cannot see the namespace or warehouse that
    contains it, so their own breadcrumb 403s and every list above it comes back empty.

    That is why this exists as a function rather than as a rule people are expected to remember: the
    C1 rollout shipped with `medallion.services.train` and `scripts/seed_medallion_fga.sh` still
    writing the forward edge alone, and nothing catches it — a one-directional link is a perfectly
    valid tuple that simply never resolves.

    The inverse is written only when the PARENT's type declares `child` (:data:`_CHILD_EDGE_PARENT_TYPES`):
    a `child` tuple on a type that does not is accepted by the API and read by no rule, which is the
    same silent no-op as writing `parent` where the model says `tenant`.

    Idempotent by construction — `write_tuples` swallows a duplicate — so it is safe over an estate
    that already has some of these, and `revoke_object_tuples` already reconstructs and removes the
    inverse on drop, so nothing is orphaned by adding them.
    """
    tuples = [ClientTuple(user=parent_object, relation=parent_relation, object=child_object)]
    if parent_object.split(":", 1)[0] in _CHILD_EDGE_PARENT_TYPES:
        tuples.append(ClientTuple(user=child_object, relation="child", object=parent_object))
    return tuples


def load_model() -> dict[str, Any]:
    """Load the authorization model JSON shipped with the app."""
    return json.loads(_MODEL_PATH.read_text())


def load_model_dsl() -> str:
    """The checked-in authorization model DSL text (``model.fga``) — the human-readable twin of the
    compiled ``model.json`` the app loads (the two are kept in sync; ``model.fga.yaml`` proves the
    behaviour). Served read-only by the estate-admin ``GET /v1/access/model`` surface."""
    return _MODEL_DSL_PATH.read_text()


# --------------------------------------------------------------------------- #
# Object-id canonicalisation
# --------------------------------------------------------------------------- #


def canonical_object_id(id_segments: list[str] | str, *, delimiter: str) -> str:
    """Return the delimited OpenFGA object id for a resource.

    The seed (grant) path and the check path MUST agree byte-for-byte on the id
    they use, otherwise a grant lands on ``table:a$b`` while the check looks up
    ``table:a/b`` and silently denies. Pass either the already-joined id string
    (returned unchanged) or the list of path segments (joined with ``delimiter``).
    """
    if isinstance(id_segments, str):
        return id_segments
    return delimiter.join(id_segments)


def parent_namespace_id(table_id: list[str] | str, *, delimiter: str) -> str | None:
    """Derive a table's parent namespace id from its id (all segments but the last).

    Returns ``None`` for a single-segment / root-level table (no parent namespace
    to inherit from). Accepts segments or an already-joined id string so callers
    can pass whichever they hold; the result is the canonical delimited id. Empty
    segments are dropped first, so a delimiter-only / blank id (the root, e.g. ``"$"``)
    collapses to ``None`` and agrees with ``parse_identifier`` — keeping the grant and
    check paths byte-for-byte aligned on the root id.
    """
    raw = table_id.split(delimiter) if isinstance(table_id, str) else table_id
    segments = [s for s in raw if s]
    if len(segments) <= 1:
        return None
    return canonical_object_id(segments[:-1], delimiter=delimiter)


def parent_object(resource: str, id_segments: list[str] | str, *, delimiter: str, root_object: str) -> str | None:
    """The FGA parent object a just-created child links to (its ``parent`` tuple), or ``None``.

    The whole concentric cascade (owner/writer/reader inherited via ``parent``) hangs off
    this edge, so every created object that *has* a parent must write it:

    - Nested child (``a$b``)  -> ``namespace:<a>`` (its parent namespace).
    - Top-level **namespace** -> ``root_object`` (the ``warehouse:`` bucket root), so warehouse-level
      grants cascade into the medallion stages ``bronze``/``silver``/``gold``.
    - Top-level **table**     -> ``None``: ``table.parent`` only accepts ``namespace`` in the
      model, so a namespace-less table cannot link to the warehouse (owner grant only).
    """
    parent_id = parent_namespace_id(id_segments, delimiter=delimiter)
    if parent_id is not None:
        return f"namespace:{parent_id}"
    return root_object if resource == "namespace" else None


# --------------------------------------------------------------------------- #
# Resilience helpers
# --------------------------------------------------------------------------- #


def _is_transient(exc: BaseException) -> bool:
    """True for failures worth retrying: transport errors or OpenFGA 429/5xx.

    The aiohttp transport raises a down/unreachable server as a raw network error
    (``aiohttp.ClientError`` / ``TimeoutError`` / ``OSError``) — the dominant outage
    mode — so those are retried. OpenFGA HTTP errors arrive as ``ApiException``; only
    429/5xx (and the SDK's status==0 prep failures) are transient. Definitive 4xx
    client errors (other than 429) are permanent and never retried.
    """
    if isinstance(exc, _TRANSIENT_NETWORK):
        return True
    if not isinstance(exc, ApiException):
        return False
    if exc.status in (0, None):
        return True  # request-prep failure surfaced by the SDK
    return bool(exc.is_retryable())


def _log_retry(retry_state: RetryCallState) -> None:
    """Log each retry so a flapping OpenFGA is visible (silent retries hide outages)."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    log.warning(
        "openfga_call_retry",
        extra={
            "attempt": retry_state.attempt_number,
            "exception_type": type(exc).__name__ if exc else None,
            "status": getattr(exc, "status", None),
        },
    )


def _retrying(
    attempts: int,
    backoff: float,
    max_backoff: float,
    overall_budget: float = DEFAULT_RETRY_OVERALL_BUDGET_SECONDS,
):
    """Build a tenacity ``@retry`` decorator over transient OpenFGA failures.

    Retries both OpenFGA ``ApiException`` 429/5xx and raw transport errors (a down
    server), and is bounded by BOTH an attempt count and an overall wall-clock budget
    so a flapping OpenFGA cannot pin a worker. tenacity is the single retry layer — the
    SDK's own retries are disabled in :func:`make_client`.
    """
    return retry(
        retry=retry_if_exception(_is_transient),
        stop=stop_after_attempt(max(1, attempts)) | stop_after_delay(overall_budget),
        wait=wait_exponential_jitter(initial=backoff, max=max_backoff),
        before_sleep=_log_retry,
        reraise=True,
    )


# --------------------------------------------------------------------------- #
# Provisioning / client construction
# --------------------------------------------------------------------------- #


async def provision(api_url: str, *, store_name: str = "lance-catalog") -> tuple[str, str]:
    """Idempotently ensure the catalog store + model exist; return ``(store_id, model_id)``.

    REUSES an existing store of the same name instead of minting a fresh one on every
    unpinned boot — otherwise a restart strands every tuple written against the previous
    store (creators silently lose access). The model is (re)written each time so
    ``model.json`` edits take effect; tuples live on the store and survive new model
    versions. For dev / e2e; in production pin ``LANCE_FGA_STORE_ID`` + ``LANCE_FGA_MODEL_ID``.
    """
    model = load_model()
    async with OpenFgaClient(ClientConfiguration(api_url=api_url)) as client:
        stores = await client.list_stores()
        existing = [s for s in (stores.stores or []) if s.name == store_name]
        if existing:
            store_id = max(existing, key=lambda s: s.created_at).id
        else:
            created = await client.create_store(CreateStoreRequest(name=store_name))
            store_id = created.id
    async with OpenFgaClient(ClientConfiguration(api_url=api_url, store_id=store_id)) as client:
        written = await client.write_authorization_model(
            WriteAuthorizationModelRequest(
                schema_version=model["schema_version"],
                type_definitions=model["type_definitions"],
                # The WHOLE model, conditions included. Dropping this key while relations reference
                # a condition makes OpenFGA 400 the write ("condition … is undefined"), the client
                # never builds, and every FGA-enabled service fail-closes 503 — the entire plane
                # down from one silently omitted field (found live, 2026-08-03).
                conditions=model.get("conditions"),
            )
        )
    return store_id, written.authorization_model_id


async def resolve(api_url: str, *, store_name: str = "lance-catalog") -> tuple[str, str] | None:
    """Find an EXISTING store + its current model by name. Read-only. ``None`` if either is absent.

    THE HALF OF :func:`provision` THAT IS NOT A WRITE, split out because conflating them cost the
    ingest plane its entire user-bearer door.

    A service that must not own the estate's permissions must not WRITE an authorization model —
    ingest is a data writer, and a writer minting a model makes it the source of truth for everyone
    else's access. That principle is right. What it does not justify is refusing to LOOK UP the store
    every other service is already using: reading is not authoring.

    Ingest applied the principle to both halves and therefore had nothing to fall back on when
    unpinned. The chart's default posture is ``auth.fgaStoreId: ""`` — resolve-by-name at boot, because
    a store id is a per-cluster ULID and cannot be a committed chart default — so ingest 503'd out of
    the box on every dev and e2e cluster while catalog, lineage and medallion came up fine. The
    symptom reaches a user as ``{"message":"Internal Error"}`` from a form submit; the cause is a
    startup warning nobody reads.

    Returning ``None`` rather than provisioning is the point: if the store does not exist yet, the
    estate has not been bootstrapped, and the caller fails closed. This function can never create a
    store, never write a model, and never change what anyone is allowed to do.

    The model is the store's CURRENT one — ``read_authorization_models`` answers newest-first — which
    is the same version :func:`provision` just wrote for the services that do bootstrap. Pinning
    ``*_FGA_MODEL_ID`` remains the production posture; this is the dev/e2e path made survivable.
    """
    async with OpenFgaClient(ClientConfiguration(api_url=api_url)) as client:
        stores = await client.list_stores()
        existing = [s for s in (stores.stores or []) if s.name == store_name]
        if not existing:
            return None
        store_id = max(existing, key=lambda s: s.created_at).id
    async with OpenFgaClient(ClientConfiguration(api_url=api_url, store_id=store_id)) as client:
        models = await client.read_authorization_models()
        found = models.authorization_models or []
        if not found:
            return None
        return store_id, found[0].id


def make_client(
    api_url: str,
    store_id: str,
    model_id: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> OpenFgaClient:
    """Build an OpenFGA client pinned to a store + authorization model.

    ``timeout_seconds`` becomes the client-wide request timeout
    (``ClientConfiguration.timeout_millisec``) so an unresponsive OpenFGA fails
    fast — and is then retried by ``check`` / ``grant_on_create`` — instead of
    hanging the request indefinitely.

    The SDK's built-in retry layer is disabled (``RetryParams(max_retry=0)``) so it
    does not stack on tenacity (which would fan one logical check into ~12 HTTP calls
    with two uncoordinated backoffs). tenacity is the single authoritative retry layer.
    """
    return OpenFgaClient(
        ClientConfiguration(
            api_url=api_url,
            store_id=store_id,
            authorization_model_id=model_id,
            timeout_millisec=int(timeout_seconds * 1000),
            retry_params=RetryParams(max_retry=0),
        )
    )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def condition_context(context: dict[str, Any] | None = None) -> dict[str, Any]:
    """The context a CONDITION is evaluated against, with the clock supplied by default.

    ONE helper for the whole estate, because the failure it prevents is silent. The model's
    ``non_expired_grant`` takes ``current_time`` from the CALLER (the tuple carries only
    ``grant_time`` + ``grant_duration``), and OpenFGA cannot evaluate a CEL expression whose
    parameters are missing — so a read that omits the clock does not get "no opinion", it gets a
    **DENY**, and a perfectly valid time-boxed grant reads as already expired. Every read below
    therefore defaults the clock in rather than trusting each call site to remember it, which is
    exactly the class of bug F2 records: the wrappers could not even accept a context, so no caller
    could have passed one.

    An explicit value WINS on every key — an audit or access-simulation that asks "was this allowed
    last Tuesday" passes its own ``current_time`` and gets it. Keys the matched condition does not
    declare are simply unused, so defaulting the clock is safe for unconditional tuples too.

    RFC 3339 with an explicit offset is what the CEL ``timestamp`` type parses; a naive
    ``datetime.now()`` would serialise without one and fail the parse at the server.
    """
    return {"current_time": datetime.now(UTC).isoformat(), **(context or {})}


async def check(
    client: OpenFgaClient,
    *,
    user: str,
    relation: str,
    obj: str,
    qualify: bool = True,
    contextual_tuples: list[ClientTuple] | None = None,
    context: dict[str, Any] | None = None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> bool:
    """Return whether ``user:<user>`` has ``relation`` on ``obj`` (e.g. ``table:db1$t``).

    ``context`` supplies the runtime values a CONDITION evaluates against — for the model's
    ``non_expired_grant`` that is ``current_time``. The split is deliberate: the tuple carries the
    window (``grant_time`` + ``grant_duration``, written once at grant time) and the caller carries the
    clock. Omitting it against a conditional tuple is not "no opinion", it is a DENY — OpenFGA cannot
    evaluate the CEL expression without its parameters — so a caller that forgets the context sees a
    time-boxed grant as already expired.

    ``contextual_tuples`` evaluates the check AS IF those tuples existed, without writing anything.
    That is the whole of "assert before you grant": ask whether a proposed grant would actually produce
    the access someone wants — and, just as often, what ELSE it would unlock — while the store is
    untouched. A concentric model makes this necessary rather than nice: a grant three levels up
    cascades to every child, and reading the DSL to predict that is exactly the reasoning humans get
    wrong. Contextual tuples are per-request and never persisted.

    Callers pass a BARE subject id (the token's ``sub``) and this prepends ``user:``. Pass
    ``qualify=False`` when ``user`` is ALREADY a full subject — a ``type:id`` user or a ``type:id#rel``
    userset — so it is sent verbatim (the access-simulator probes arbitrary subjects, incl. usersets;
    double-prefixing them to ``user:user:…`` would make every Check falsely deny — review 2026-07-20).

    Transient OpenFGA failures (network/timeout, 429, 5xx) are retried with
    backoff; a definitive ``allowed=false`` is a normal result and is NOT retried.
    On exhausted retries / outage we fail closed with ``ServiceUnavailableError``.
    The per-request timeout is carried by ``client`` (see ``make_client``).
    """
    subject = f"user:{user}" if qualify else user

    @_retrying(retry_attempts, retry_backoff_seconds, retry_max_backoff_seconds)
    async def _do_check() -> bool:
        response = await client.check(
            ClientCheckRequest(
                user=subject,
                relation=relation,
                object=obj,
                contextual_tuples=contextual_tuples,
                context=condition_context(context),
            )
        )
        return bool(response.allowed)

    try:
        return await _do_check()
    except _FAIL_CLOSED as exc:
        log.error("openfga_check_unavailable", extra={"relation": relation, "object": obj}, exc_info=True)
        raise ServiceUnavailableError("authorization service unavailable") from exc


async def batch_check(
    client: OpenFgaClient,
    *,
    user: str,
    relation: str,
    objects: list[str],
    context: dict[str, Any] | None = None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> dict[str, bool]:
    """Return ``{object: allowed}`` for one user across many objects in one round-trip.

    ``context`` is per-item and defaulted through :func:`condition_context`, so a batch spanning
    time-boxed grants answers on the same clock as a single :func:`check` would. Without it every
    conditional tuple in the batch would come back denied.

    A read-only/idempotent authorization path, so it gets the same bounded retry +
    fail-closed treatment as :func:`check` (transient OpenFGA/transport faults retried;
    outage → ``ServiceUnavailableError`` → 503).
    """
    items = [ClientBatchCheckItem(user=f"user:{user}", relation=relation, object=o, context=condition_context(context)) for o in objects]

    @_retrying(retry_attempts, retry_backoff_seconds, retry_max_backoff_seconds)
    async def _do_batch_check() -> dict[str, bool]:
        response = await client.batch_check(ClientBatchCheckRequest(checks=items))
        return {r.request.object: bool(r.allowed) for r in response.result}

    try:
        return await _do_batch_check()
    except _FAIL_CLOSED as exc:
        log.error("openfga_batch_check_unavailable", extra={"relation": relation}, exc_info=True)
        raise ServiceUnavailableError("authorization service unavailable") from exc


async def list_objects(
    client: OpenFgaClient,
    *,
    user: str,
    relation: str,
    object_type: str,
    qualify: bool = True,
    context: dict[str, Any] | None = None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> list[str]:
    """Return the objects of ``object_type`` the user has ``relation`` on (e.g. ``table:…``).

    Callers pass a BARE subject id (the token's ``sub``) and this prepends ``user:``. Pass
    ``qualify=False`` when ``user`` is ALREADY a full subject — a ``type:id`` user or a ``type:id#rel``
    userset — exactly as :func:`check` does. Without that hatch the estate-admin explorer could only ask
    "what can this *user* reach", never "what can ``team:eng#member`` reach", and in THIS model the
    resource rungs deliberately refuse ``team#member`` directly (a team reaches data through a role), so
    the userset question is the one that explains a real grant.

    ``context`` is defaulted through :func:`condition_context`. It matters MORE here than on a single
    check: a listing that omits the clock silently drops every object reachable only through a
    time-boxed grant, and a short list reads as a correct answer rather than an error.

    Read-only/idempotent, so it gets the same bounded retry + fail-closed treatment as
    :func:`check`.
    """
    subject = f"user:{user}" if qualify else user

    @_retrying(retry_attempts, retry_backoff_seconds, retry_max_backoff_seconds)
    async def _do_list_objects() -> list[str]:
        response = await client.list_objects(ClientListObjectsRequest(user=subject, relation=relation, type=object_type, context=condition_context(context)))
        return list(response.objects)

    try:
        return await _do_list_objects()
    except _FAIL_CLOSED as exc:
        log.error(
            "openfga_list_objects_unavailable",
            extra={"relation": relation, "object_type": object_type},
            exc_info=True,
        )
        raise ServiceUnavailableError("authorization service unavailable") from exc


#: OpenFGA's default ``listUsersMaxResults`` — ListUsers has no pagination, so a result this large
#: is likely the server's silent truncation point, not the true grantee count.
LIST_USERS_SERVER_CAP = 1000


async def list_users(
    client: OpenFgaClient,
    *,
    relation: str,
    obj: str,
    user_type: str = "user",
    user_relation: str | None = None,
    qualified: bool = False,
    context: dict[str, Any] | None = None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> list[str]:
    """The ``user:`` subjects holding ``relation`` on ``obj`` (e.g. every reader of ``table:db1$t``),
    sorted — the access-review primitive (#51), the inverse of :func:`list_objects`.

    ``user_type`` (+ optional ``user_relation``) selects WHICH subject kind to enumerate — OpenFGA takes
    exactly ONE filter per call, so ``user_type="role", user_relation="assignee"`` answers "which roles
    hold this", a different question from "which users". The default keeps the review contract every
    existing caller relies on: bare ``user:`` ids.

    ``qualified=True`` returns FULL subjects (``user:alice``, ``team:eng#member``, ``user:*``) instead of
    bare ids. Off by default because the access-review callers built their output around bare ids; the
    estate explorer needs the qualified form, since a bare ``eng`` is ambiguous once the filter is not
    ``user``.

    ListUsers *expands* the model (role assignees, team members, the parent cascade), so the answer is
    effective access, not just direct tuples — exactly what a reviewer needs. A public ``user:*``
    wildcard surfaces as ``"*"`` (the model doesn't write one today, but a review must never hide it).
    Read-only/idempotent, so it gets the same bounded retry + fail-closed treatment as :func:`check`
    (outage → ``ServiceUnavailableError`` → 503 — never an empty list that reads as "nobody").

    ``context`` is defaulted through :func:`condition_context`, so an access review counts the
    holders of a live time-boxed grant instead of omitting them — the same silent-undercount hazard
    as :func:`list_objects`, pointed the other way.

    Caveat: the ListUsers API has no pagination, and the server truncates silently at its
    ``listUsersMaxResults`` / ``listUsersDeadline`` limits (OpenFGA defaults: 1000 subjects / 3s) —
    so a result at or past the cap is logged as possibly truncated; an under-reported review must
    never pass unnoticed.
    """
    obj_type, _, obj_id = obj.partition(":")

    @_retrying(retry_attempts, retry_backoff_seconds, retry_max_backoff_seconds)
    async def _do_list_users() -> list[str]:
        response = await client.list_users(
            ClientListUsersRequest(
                object=FgaObject(type=obj_type, id=obj_id),
                relation=relation,
                user_filters=[UserTypeFilter(type=user_type, relation=user_relation)],
                context=condition_context(context),
            )
        )
        subjects: list[str] = []
        for user in response.users or []:
            # The three arms of ListUsers' User union. A userset arm only ever arrives when the filter
            # ASKS for one (user_relation set); dropping it silently — as this did before — hid exactly
            # the subjects this model routes access through.
            if (obj_arm := getattr(user, "object", None)) is not None:
                subjects.append(f"{obj_arm.type}:{obj_arm.id}" if qualified else obj_arm.id)
            elif (userset_arm := getattr(user, "userset", None)) is not None:
                subjects.append(f"{userset_arm.type}:{userset_arm.id}#{userset_arm.relation}" if qualified else userset_arm.id)
            elif (wildcard_arm := getattr(user, "wildcard", None)) is not None:
                subjects.append(f"{wildcard_arm.type}:*" if qualified else "*")
        if len(subjects) >= LIST_USERS_SERVER_CAP:
            log.warning(
                "openfga_list_users_possibly_truncated",
                extra={"relation": relation, "object": obj, "subjects": len(subjects)},
            )
        return sorted(subjects)

    try:
        return await _do_list_users()
    except _FAIL_CLOSED as exc:
        log.error("openfga_list_users_unavailable", extra={"relation": relation, "object": obj}, exc_info=True)
        raise ServiceUnavailableError("authorization service unavailable") from exc


#: Depth ceiling for the Expand tree walk. OpenFGA expands ONE relation's rewrite expression (it does
#: not recurse through `tuple_to_userset` into the parent's own tree), so real depth is the nesting of
#: `or`/`and`/`but not` in the DSL — 3 in this model. The cap only stops a malformed/cyclic response
#: from spinning the normaliser; hitting it is logged, never silently truncated into a valid-looking tree.
_MAX_EXPAND_DEPTH = 32


def _expand_node(node: Any, depth: int = 0) -> dict[str, Any]:
    """Normalise one SDK ``Node`` into plain JSON-able dicts, verbatim in structure.

    The SDK's tree is a graph of model objects whose ``to_dict`` drags along OpenAPI machinery, so the
    admin API would leak SDK shape into a frozen contract. This keeps the OpenFGA vocabulary
    (``union`` / ``intersection`` / ``difference`` / ``leaf``) intact — the caller wants the DERIVATION,
    and flattening it here would throw away exactly the operator that explains a surprising grant.
    """
    if depth >= _MAX_EXPAND_DEPTH:
        log.warning("openfga_expand_depth_capped", extra={"depth": depth, "node": getattr(node, "name", None)})
        return {"name": getattr(node, "name", None), "truncated": True}

    out: dict[str, Any] = {"name": getattr(node, "name", None)}

    if (leaf := getattr(node, "leaf", None)) is not None:
        computed = getattr(leaf, "computed", None)
        ttu = getattr(leaf, "tuple_to_userset", None)
        users = getattr(leaf, "users", None)
        out["leaf"] = {
            # `users` is the terminal set — the actual subjects, incl. a `user:*` wildcard verbatim.
            "users": list(getattr(users, "users", None) or []) if users is not None else None,
            "computed": getattr(computed, "userset", None) if computed is not None else None,
            "tuple_to_userset": (
                {
                    "tupleset": getattr(ttu, "tupleset", None),
                    "computed": [c.userset for c in (getattr(ttu, "computed", None) or []) if getattr(c, "userset", None)],
                }
                if ttu is not None
                else None
            ),
        }
    if (union := getattr(node, "union", None)) is not None:
        out["union"] = [_expand_node(n, depth + 1) for n in (getattr(union, "nodes", None) or [])]
    if (intersection := getattr(node, "intersection", None)) is not None:
        out["intersection"] = [_expand_node(n, depth + 1) for n in (getattr(intersection, "nodes", None) or [])]
    if (difference := getattr(node, "difference", None)) is not None:
        base = getattr(difference, "base", None)
        subtract = getattr(difference, "subtract", None)
        out["difference"] = {
            "base": _expand_node(base, depth + 1) if base is not None else None,
            "subtract": _expand_node(subtract, depth + 1) if subtract is not None else None,
        }
    return out


async def expand(
    client: OpenFgaClient,
    *,
    relation: str,
    obj: str,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> dict[str, Any]:
    """The userset TREE for ``relation`` on ``obj`` — how the grant is composed, not whether it holds.

    This is the one primitive that answers *why*. :func:`check` returns a bare boolean and
    :func:`list_users` returns the effective subject set; neither says which rung or which parent hop
    produced the answer, so a permission derived through three hops is indistinguishable from a direct
    grant — and the two have very different blast radii when revoked.

    Returns the tree normalised to plain dicts (see :func:`_expand_node`), rooted at the relation:
    ``{"name": "table:db1$t#reader", "union": [{"leaf": {"users": [...]}}, {"leaf": {"computed": ...}}, …]}``.
    An empty/absent tree comes back as ``{}`` rather than ``None``, so callers branch on emptiness, not
    on a null that also means "unavailable".

    Note Expand resolves ONE level: a ``tuple_to_userset`` leaf names the tupleset and the computed
    relation, it does not recurse into the parent object's own tree. Walking the full chain is the
    caller's job (one Expand per hop) — which is deliberate: the model owns the shape, not this wrapper.

    Read-only/idempotent, so it gets the same bounded retry + fail-closed treatment as :func:`check`
    (outage → ``ServiceUnavailableError`` → 503, never an empty tree that reads as "derived from nothing").
    """

    @_retrying(retry_attempts, retry_backoff_seconds, retry_max_backoff_seconds)
    async def _do_expand() -> dict[str, Any]:
        response = await client.expand(ClientExpandRequest(relation=relation, object=obj))
        tree = getattr(response, "tree", None)
        root = getattr(tree, "root", None) if tree is not None else None
        return _expand_node(root) if root is not None else {}

    try:
        return await _do_expand()
    except _FAIL_CLOSED as exc:
        log.error("openfga_expand_unavailable", extra={"relation": relation, "object": obj}, exc_info=True)
        raise ServiceUnavailableError("authorization service unavailable") from exc


#: How many hops :func:`expand_tree` will follow. The model's deepest real chain is
#: team → project → warehouse → namespace → nested namespace → table, i.e. 5 hops, so this covers it with
#: no room for a pathological store to turn one request into an unbounded fan-out.
MAX_EXPAND_TREE_DEPTH = 6


def _relation_of(userset: str) -> str:
    """The relation half of an OpenFGA userset reference.

    Expand answers with QUALIFIED usersets — `"table:db1$t#reader"`, not `"reader"` — in both the
    `computed` leaf and each `tupleToUserset.computed` entry. Everything downstream wants the bare
    relation, and passing the qualified form on is a malformed request rather than a wrong answer, so
    it surfaces as a 500 from the server and a fail-closed 503 here. Bare input passes through, because
    the shape is not guaranteed across versions and a stricter reader would trade one bug for another.
    """
    return userset.rpartition("#")[2] or userset


def _object_of(userset: str, fallback: str) -> str:
    """The object half of a qualified userset, or ``fallback`` when it carries none.

    A `computed` leaf is by definition a rung on the SAME object, so the fallback is the normal path;
    the split exists so a qualified reference is never silently re-anchored to the wrong object.
    """
    obj = userset.rpartition("#")[0]
    return obj or fallback


async def expand_tree(
    client: OpenFgaClient,
    *,
    relation: str,
    obj: str,
    max_depth: int = 1,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> dict[str, Any]:
    """:func:`expand`, followed recursively — the FULL derivation chain, not one level of it.

    A single Expand stops at the edge: a ``tuple_to_userset`` leaf says "``owner`` from whatever
    ``parent`` points at" without saying what that is or who holds it there. That is the exact hop where
    a concentric model's answer lives, so a one-level tree cannot explain a cascaded grant — it can only
    say that a cascade exists. This follows both continuing leaf kinds:

    * ``computed`` (a same-object rung — ``owner`` implying ``writer``) → expand that rung on this object.
    * ``tuple_to_userset`` (``X from Y``) → read the tupleset's tuples off this object to find the target
      objects, then expand each computed relation on each target.

    Each resolved child is attached to its leaf as ``expanded``, so the shape stays the OpenFGA tree with
    the hops filled in rather than a flattened path that has already thrown the operators away.

    ``max_depth`` counts hops (1 = a plain Expand, the faithful primitive), clamped to
    :data:`MAX_EXPAND_TREE_DEPTH`. Both terminations are marked in the tree rather than silently
    dropped, because a derivation that stopped early must never read as one that ended:

    * ``leaf.continues`` — the budget ran out AT this leaf. Decided BEFORE any round trip, so a depth-1
      call costs exactly one Expand while still telling a UI where "expand further" is offerable. This
      is the only way the walk stops on budget; there is deliberately no half-taken hop.
    * ``cycle`` — this ``object#relation`` was already visited. ``namespace`` self-nests under
      ``namespace``, so a loop is reachable through misconfigured tuples, not only through a bug.

    (``truncated`` may still appear, from :func:`_expand_node` — that is the *normaliser's* structural
    depth cap on one malformed response, a different failure from this walk's hop budget.)

    Read-only/idempotent throughout — every leg inherits the bounded retry + fail-closed behaviour of
    :func:`expand` and :func:`read_object_tuples`, so a partial tree is never returned in place of a 503.
    """
    depth_budget = max(1, min(max_depth, MAX_EXPAND_TREE_DEPTH))
    seen: set[str] = set()

    async def _walk(rel: str, target: str, depth: int) -> dict[str, Any]:
        key = f"{target}#{rel}"
        if key in seen:
            return {"name": key, "cycle": True}
        seen.add(key)
        node = await expand(
            client,
            relation=rel,
            obj=target,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            retry_max_backoff_seconds=retry_max_backoff_seconds,
        )
        await _resolve(node, target, depth)
        return node

    async def _resolve(node: dict[str, Any], target: str, depth: int) -> None:
        """Walk the normalised tree in place, expanding every leaf that continues somewhere."""
        for branch in ("union", "intersection"):
            for child in node.get(branch) or []:
                await _resolve(child, target, depth)
        if (difference := node.get("difference")) is not None:
            for side in ("base", "subtract"):
                if (child := difference.get(side)) is not None:
                    await _resolve(child, target, depth)

        leaf = node.get("leaf")
        if not leaf:
            return
        if depth >= depth_budget:
            # Out of budget: mark that this leaf CONTINUES and stop — before spending a tuple read
            # whose only product would be a truncation stub. The flag is what lets a UI offer "expand
            # further" instead of drawing a dead end that is not one.
            if leaf.get("computed") is not None or leaf.get("tuple_to_userset") is not None:
                leaf["continues"] = True
            return
        if (computed := leaf.get("computed")) is not None:
            # `computed.userset` comes back as a FULL `object#relation` ("table:db1$t#reader"), not a
            # bare relation name. Feeding the whole string back as a relation makes the next Expand
            # malformed and OpenFGA answers 500 — which this module dutifully fails closed into a 503,
            # so the symptom was "the derivation is unavailable" on every relation that resolves
            # through a rung, i.e. all of the interesting ones. Verified against the live store:
            # expand(can_read_data, table:bronze$pages) → {"computed":{"userset":"table:bronze$pages#reader"}}.
            leaf["expanded"] = [await _walk(_relation_of(computed), _object_of(computed, target), depth + 1)]
            return
        if (ttu := leaf.get("tuple_to_userset")) is not None:
            # `tupleset` arrives as "<object>#<relation>"; the relation half is the edge to follow, and
            # the tuples holding it name the parent objects (`table:x#parent@namespace:db1` → namespace:db1).
            tupleset_relation = str(ttu.get("tupleset") or "").rpartition("#")[2]
            if not tupleset_relation:
                return
            edges = await read_object_tuples(
                client,
                target,
                retry_attempts=retry_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
                retry_max_backoff_seconds=retry_max_backoff_seconds,
            )
            parents = [t.user for t in edges if t.relation == tupleset_relation]
            expanded: list[dict[str, Any]] = []
            for parent in parents:
                for computed_relation in ttu.get("computed") or []:
                    # Same normalisation as the `computed` leaf above: these arrive qualified too.
                    expanded.append(await _walk(_relation_of(computed_relation), parent, depth + 1))
            leaf["expanded"] = expanded

    return await _walk(relation, obj, 1)


async def read_changes(
    client: OpenFgaClient,
    *,
    object_type: str,
    page_size: int | None = None,
    continuation_token: str | None = None,
    start_time: str | None = None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> tuple[list[dict[str, Any]], str | None]:
    """The tuple CHANGELOG for one object type — every write and delete, in order, with timestamps.

    This is the only "when" the estate has. A Read shows what is true NOW; it cannot say when a grant
    landed, and it cannot show a grant that was later revoked. The changelog shows both, which makes it
    the difference between an access review and an access snapshot.

    What it deliberately does NOT carry is WHO. An OpenFGA change record is
    ``{tuple_key, operation, timestamp}`` and nothing else — the store has no notion of an actor, and
    no amount of reading it will produce one. The acting principal exists only in this estate's own
    audit stream (``access_tuple_write`` / ``access_tuple_delete``, which record ``subject``), and
    joining the two is a correlation by (resource, relation, grantee, time) rather than a lookup.
    Callers must not present a timestamp as attribution.

    ``object_type`` is REQUIRED by the API — the changelog is filterable by type and by nothing else,
    so "the history of THIS tuple" is a scan of its type's changes, not a query. Bounded by the caller.

    **Termination is the caller's, and it is not the token.** ReadChanges returns the same non-empty
    continuation token forever once the stream is exhausted, because it is a resumable cursor rather
    than an end-of-list sentinel. Loop on the PAGE, never the token::

        changes, token = await read_changes(client, object_type="table")
        while changes:
            ...
            changes, token = await read_changes(client, object_type="table", continuation_token=token)

    Read-only/idempotent, so it gets the same bounded retry + fail-closed treatment as :func:`check`:
    an outage is a 503, never an empty history that reads as "nothing ever happened here".
    """

    @_retrying(retry_attempts, retry_backoff_seconds, retry_max_backoff_seconds)
    async def _do_read_changes() -> tuple[list[dict[str, Any]], str | None]:
        # Built INSIDE the retry closure for the same reason read_tuples does it: the SDK pops
        # pagination out of the options dict it is handed, so a shared dict loses it on attempt two.
        options: dict[str, int | str | dict[str, int | str]] = {}
        if page_size is not None:
            options["page_size"] = page_size
        if continuation_token:
            options["continuation_token"] = continuation_token
        response = await client.read_changes(
            ClientReadChangesRequest(type=object_type, start_time=start_time),
            options=options or None,
        )
        changes: list[dict[str, Any]] = []
        for c in response.changes or []:
            key = c.tuple_key
            stamp = getattr(c, "timestamp", None)
            changes.append(
                {
                    "user": key.user,
                    "relation": key.relation,
                    "object": key.object,
                    # "TUPLE_OPERATION_WRITE" / "…_DELETE" → "write" / "delete".
                    "operation": str(c.operation).rsplit("_", 1)[-1].lower(),
                    "timestamp": stamp.isoformat() if hasattr(stamp, "isoformat") else (str(stamp) if stamp else None),
                }
            )
        # The token is returned RAW, and callers must stop on an empty page rather than on a falsy
        # token. OpenFGA's ReadChanges keeps handing back the SAME non-empty continuation token once the
        # stream is exhausted — it is a resumable cursor, not an end-of-list sentinel — so the usual
        # `while token:` loop never terminates. Normalising it to None here would be worse: it would
        # discard a cursor a caller may legitimately persist to resume later.
        return changes, response.continuation_token

    try:
        return await _do_read_changes()
    except _FAIL_CLOSED as exc:
        log.error("openfga_read_changes_unavailable", extra={"object_type": object_type}, exc_info=True)
        raise ServiceUnavailableError("authorization service unavailable") from exc


async def read_object_tuples(
    client: OpenFgaClient,
    obj: str,
    *,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> list[ClientTuple]:
    """Return EVERY tuple whose object is ``obj`` (e.g. all grants on ``table:db1$t``).

    Paginated under the hood so the COMPLETE set comes back, not just the first page — the
    revoke-on-delete caller must see every stale grant to remove it. Read-only/idempotent, so it
    gets the same bounded retry + fail-closed treatment as :func:`check` (outage → 503).
    """

    @_retrying(retry_attempts, retry_backoff_seconds, retry_max_backoff_seconds)
    async def _do_read() -> list[ClientTuple]:
        collected: list[ClientTuple] = []
        token: str | None = None
        for _ in range(_MAX_REVOKE_PAGES):
            options = {"continuation_token": token} if token else None
            response = await client.read(ReadRequestTupleKey(object=obj), options=options)
            for t in response.tuples or []:
                key = t.key
                collected.append(ClientTuple(user=key.user, relation=key.relation, object=key.object, condition=getattr(key, "condition", None)))
            token = response.continuation_token or None
            if not token:
                break
        else:
            # Hit the page ceiling with a token still outstanding → a PARTIAL set. Surface it (a partial
            # revoke must not look complete); for one object this ceiling is unreachable in practice.
            log.warning("openfga_read_truncated", extra={"object": obj, "max_pages": _MAX_REVOKE_PAGES})
        return collected

    try:
        return await _do_read()
    except _FAIL_CLOSED as exc:
        log.error("openfga_read_unavailable", extra={"object": obj}, exc_info=True)
        raise ServiceUnavailableError("authorization service unavailable") from exc


async def read_tuples(
    client: OpenFgaClient,
    *,
    user: str | None = None,
    obj: str | None = None,
    page_size: int | None = None,
    continuation_token: str | None = None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> tuple[list[ClientTuple], str | None]:
    """One page of the OpenFGA Read API — the DIRECT tuples matching the filter, plus the next-page token.

    Unlike :func:`list_users`, Read never expands the model: the answer is the raw stored tuples (the
    estate-admin tuple browser's primitive), paginated by the server. ``obj`` is either a full object
    (``table:db1$t``) or a bare-type scan (``table:``); OpenFGA REQUIRES at least the object type whenever
    a tuple filter is sent, so a user-only filter is rejected server-side — callers enforce that
    precondition up front (the admin API maps it to a 400). No filter at all reads the whole store.
    Read-only/idempotent, so it gets the same bounded retry + fail-closed treatment as :func:`check`
    (outage → ``ServiceUnavailableError`` → 503, never an empty page that reads as "no grants").
    """
    tuple_key = ReadRequestTupleKey(user=user, object=obj)  # the SDK omits it when every field is None

    @_retrying(retry_attempts, retry_backoff_seconds, retry_max_backoff_seconds)
    async def _do_read_page() -> tuple[list[ClientTuple], str | None]:
        # Built INSIDE the retry closure: the SDK pops page_size/continuation_token out of the options
        # dict it is handed, so a shared dict would silently lose the pagination on the second attempt.
        options: dict[str, int | str | dict[str, int | str]] = {}
        if page_size is not None:
            options["page_size"] = page_size
        if continuation_token:
            options["continuation_token"] = continuation_token
        response = await client.read(tuple_key, options=options or None)
        # The condition rides the tuple back out. Dropping it (as this did) makes a time-boxed grant
        # read as permanent everywhere downstream — the revoke-on-delete path would then try to delete
        # a tuple it has mis-described, and the UI would show an expiring grant as forever.
        page = [
            ClientTuple(
                user=t.key.user,
                relation=t.key.relation,
                object=t.key.object,
                condition=getattr(t.key, "condition", None),
            )
            for t in response.tuples or []
        ]
        return page, response.continuation_token or None

    try:
        return await _do_read_page()
    except _FAIL_CLOSED as exc:
        log.error("openfga_read_unavailable", extra={"object": obj, "user": user}, exc_info=True)
        raise ServiceUnavailableError("authorization service unavailable") from exc


# --------------------------------------------------------------------------- #
# Writes (post-create grants) + deletes (revoke-on-delete)
# --------------------------------------------------------------------------- #


def _is_duplicate_write(exc: ApiException) -> bool:
    """True if a write failed only because the tuple already exists (idempotent grant)."""
    if exc.status is not None and exc.status != 400:
        return False
    body = (str(exc) + " " + (exc.error_message or "")).lower()
    return any(marker in body for marker in _DUPLICATE_WRITE_MARKERS)


#: Where a tuple write came from, recorded on every audit row so provenance can say HOW a grant
#: happened and not merely who. `create` dwarfs the rest in a real estate — it is the post-registration
#: seed — and telling it apart from a deliberate admin grant is most of the value.
#:
#: The tenant-lifecycle origins (`project_create`, `warehouse_create`, `lifecycle_delete`) name the
#: control-plane doors that mint and retire a tenant's authz: an audit row can then say a grant was born
#: WITH its project rather than added to it later, and a revoke was a deletion rather than an admin's
#: choice. `warehouse_bootstrap` is gone with the exception it named — a project's first warehouse no
#: longer mints its admin (that moved to `POST /v1/projects`, `open_hierarchy_lifecycle.md` Decision 1).
TupleOrigin = Literal[
    "admin_api",
    "grant_api",
    "create",
    "project_create",
    "warehouse_create",
    "lifecycle_delete",
    "train",
    "annotator",
]


def _audit_tuples(action: str, tuples: list[ClientTuple], actor: str, origin: TupleOrigin) -> None:
    """One audit row PER TUPLE, emitted from inside the write path.

    This lives here rather than at the call sites because coverage by convention does not hold: of the
    eight tuple-write sites in this repo, only two ever emitted a tuple-level row, and the ones that did
    not include ``grant_on_create`` — which fires on every table and namespace registration and is
    therefore where most tuples in a real store come from. The consequence was that "who granted this?"
    had no answer for the majority of the graph, while looking like it did for the admin-API minority.

    Emitting from the library makes the coverage structural: a new write site cannot reach OpenFGA
    without passing through here, and ``actor`` is required, so it cannot compile without naming one.
    """
    for t in tuples:
        audit(action, SUCCESS, subject=actor, resource=t.object, grantee=t.user, relation=t.relation, origin=origin)


async def write_tuples(
    client: OpenFgaClient,
    tuples: list[ClientTuple],
    *,
    actor: str,
    origin: TupleOrigin,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> None:
    """Persist relationship tuples (the single write path after a successful mutation).

    Idempotent **per tuple**, which is not the same thing as per call. An OpenFGA ``Write`` is one
    transaction: if a single tuple in the batch already exists, the server rejects the ENTIRE write with
    ``write_failed_due_to_invalid_input`` and nothing lands. Swallowing that as "the post-condition already
    holds" is true only for a one-tuple batch — for a batch it silently drops every sibling grant, and the
    caller is told the grant succeeded. That is how a warehouse creator lost ``owner`` on their own
    warehouse: :func:`~catalog.api.fga_deps.seed_warehouse` batches the owner grant, the project edge and
    (on tenant bootstrap) ``admin`` on the project — and the seed scripts had already written that last
    tuple, so the whole batch was rejected, silently, and the next call 403'd with
    ``can_create_namespace required``. Verified against a live OpenFGA 2026-07-26: writing [existing, new]
    leaves only `existing` behind.

    So a duplicate rejection falls back to writing the tuples ONE AT A TIME, where "already exists" really
    does mean the post-condition holds for that tuple and its siblings still land. The fast path is
    unchanged (one transactional write); the fallback only runs on the rejection. This is the same hazard
    :func:`delete_tuples` was already written to avoid — the lesson simply never crossed to the write side.

    Transient failures are retried; on outage we fail closed with ``ServiceUnavailableError`` so the caller
    does not believe a grant succeeded when it did not.
    """

    @_retrying(retry_attempts, retry_backoff_seconds, retry_max_backoff_seconds)
    async def _do_write(batch: list[ClientTuple]) -> None:
        await client.write(ClientWriteRequest(writes=batch))

    async def _write_one_by_one() -> None:
        for item in tuples:
            try:
                await _do_write([item])
            except _FAIL_CLOSED as exc:
                if isinstance(exc, ApiException) and _is_duplicate_write(exc):
                    log.debug(
                        "openfga_write_duplicate_skipped",
                        extra={"object": item.object, "relation": item.relation},
                    )
                    continue
                log.error(
                    "openfga_write_unavailable",
                    extra={"object": item.object, "relation": item.relation},
                    exc_info=True,
                )
                raise ServiceUnavailableError("authorization service unavailable") from exc

    try:
        await _do_write(tuples)
    except _FAIL_CLOSED as exc:
        if isinstance(exc, ApiException) and _is_duplicate_write(exc):
            if len(tuples) > 1:
                # One duplicate rejected the batch; the siblings are still unwritten. Land them.
                log.info("openfga_write_batch_duplicate_retrying_singly", extra={"tuples": len(tuples)})
                await _write_one_by_one()
                _audit_tuples("access_tuple_write", tuples, actor, origin)
                return
            log.debug("openfga_write_duplicate_skipped")
            # A duplicate IS the post-condition holding, so it is audited like any other success —
            # otherwise re-running a seed silently erases the provenance of tuples that are present.
            _audit_tuples("access_tuple_write", tuples, actor, origin)
            return
        log.error("openfga_write_unavailable", exc_info=True)
        raise ServiceUnavailableError("authorization service unavailable") from exc
    _audit_tuples("access_tuple_write", tuples, actor, origin)


async def grant_on_create(
    client: OpenFgaClient,
    *,
    user_sub: str,
    resource: str,
    obj_id: str,
    actor: str,
    origin: TupleOrigin,
    parent_object: str | None = None,
    parent_relation: str = "parent",
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> None:
    """Grant the creator ownership of a just-created object, in a single write.

    Always writes ``user:<sub> owner <resource>:<obj_id>`` so the creator can
    manage what they made. When ``parent_object`` is given it *also* writes
    ``<resource>:<obj_id> parent <parent_object>`` so the object inherits
    reader/writer/owner down the hierarchy (per the model). Compute it with
    :func:`parent_object` — both nested tables AND nested/top-level namespaces get
    their parent edge, which is what makes a catalog- or layer-level grant cascade
    (the medallion case). A root-level table (no namespace parent) gets the owner
    grant only.

    ``parent_relation`` names the edge on the CHILD that points at the parent — ``parent`` for every
    governed type, ``tenant`` for ``annotation_project``. Get it wrong and the write succeeds while
    the inheritance it was meant to establish silently does not exist.

    ``obj_id`` must be the canonical, delimited id (see ``canonical_object_id``)
    so this grant and the later authorization ``check`` agree byte-for-byte.

    Idempotent (safe to call again on retry of a create): writing existing tuples
    is swallowed. On OpenFGA outage it fails closed with ``ServiceUnavailableError``.
    """
    obj = f"{resource}:{obj_id}"
    tuples = [ClientTuple(user=f"user:{user_sub}", relation="owner", object=obj)]
    if parent_object:
        # The edge's RELATION NAME is a property of the model, not a constant. Every governed type
        # spells it `parent`, but `annotation_project` spells it `tenant` — writing `parent` there
        # produces a tuple OpenFGA accepts and no rule ever reads, so `admin from tenant` and
        # `member from tenant` never resolve and the object looks unowned by everyone.
        # …and the INVERSE edge with it, in the SAME batch, so the two directions cannot drift.
        # `hierarchy_edge_tuples` owns that pairing for every writer — see its docstring for what
        # happens to an object that gets only the forward half.
        tuples.extend(hierarchy_edge_tuples(child_object=obj, parent_object=parent_object, parent_relation=parent_relation))
    await write_tuples(
        client,
        tuples,
        actor=actor,
        origin=origin,
        retry_attempts=retry_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        retry_max_backoff_seconds=retry_max_backoff_seconds,
    )


def _is_missing_delete(exc: ApiException) -> bool:
    """True if a delete failed only because the tuple was already absent (idempotent revoke)."""
    if exc.status is not None and exc.status != 400:
        return False
    body = (str(exc) + " " + (exc.error_message or "")).lower()
    return any(marker in body for marker in _MISSING_DELETE_MARKERS)


async def delete_tuples(
    client: OpenFgaClient,
    tuples: list[ClientTuple],
    *,
    actor: str,
    origin: TupleOrigin,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> None:
    """Delete relationship tuples (the revoke counterpart of :func:`write_tuples`).

    Deletes each tuple in its OWN write — NOT one batch. OpenFGA's Write is transactional (all-or-nothing),
    so a single concurrently-absent tuple (the race our read-then-delete opens) in a batched delete would
    400 the WHOLE call and leave EVERY grant in place while looking like success. Per-tuple, one absent
    tuple can't block the rest, and an absent tuple is idempotent success (the post-condition — gone —
    already holds). Transient failures are retried; on outage we fail closed with ``ServiceUnavailableError``
    so the caller does not believe a revoke succeeded when it did not (stale grants would silently linger).
    """
    if not tuples:
        return

    @_retrying(retry_attempts, retry_backoff_seconds, retry_max_backoff_seconds)
    async def _delete_one(t: ClientTuple) -> None:
        await client.write(ClientWriteRequest(deletes=[t]))

    for t in tuples:
        try:
            await _delete_one(t)
        except _FAIL_CLOSED as exc:
            if isinstance(exc, ApiException) and _is_missing_delete(exc):
                log.debug("openfga_delete_absent_skipped")
                continue
            log.error("openfga_delete_unavailable", exc_info=True)
            raise ServiceUnavailableError("authorization service unavailable") from exc
    _audit_tuples("access_tuple_delete", tuples, actor, origin)


async def revoke_object_tuples(
    client: OpenFgaClient,
    obj: str,
    *,
    actor: str,
    origin: TupleOrigin,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> list[ClientTuple]:
    """Delete EVERY tuple on ``obj`` — the revoke-on-delete cleanup. Returns WHAT was removed.

    **The tuples, not a count, and the difference is who gets told.** Every destructive door in the
    estate ends here — a project delete, a warehouse delete, a cascading namespace drop, an overwrite
    create — and each revokes every grant on the object. Returning an integer meant the estate knew
    that fourteen grants vanished and not whose, so the subjects who could no longer see their own
    data learned it by hitting a 403 mid-task: the failure `grant_revoked` exists to prevent. The
    tuples were already read in order to be deleted, so handing them back costs nothing and is what
    lets a caller name the affected principals. Callers wanting the old number take ``len(...)``.

    A dropped/renamed object's grants (owner, the parent edge, AND any later reader/writer/
    validator grants) must not linger: if the id is later reused, those stale tuples would
    silently re-grant the old subjects on the NEW object (stale-grant privilege bleed). Reads the
    full tuple set, then deletes it. No-op (0) when the object has none.

    Scope: revokes the object's OWN tuples. A CASCADING namespace drop (``DropNamespaceRequest``
    ``behavior=Cascade``) also removes child tables/namespaces — the ``drop_namespace`` endpoint enumerates
    those descendants BEFORE the drop and calls this once per child, so their grants are revoked too (the
    default directory backend rejects Cascade outright, so that path only engages on a backend that
    supports it).
    """
    tuples = await read_object_tuples(
        client,
        obj,
        retry_attempts=retry_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        retry_max_backoff_seconds=retry_max_backoff_seconds,
    )
    if not tuples:
        return []
    # The inverse `child` edge is the ONE tuple about this object that a read by object cannot see:
    # `grant_on_create` stores it as `<obj> child <parent>`, so its OBJECT is the parent and it
    # survives a revoke that only reads `object == obj`. Left behind it is a phantom child — the
    # access graph renders an edge to an object that no longer exists, and the drop looks incomplete.
    # The parent is recoverable from what was just read: the `parent` edge is `<parent> parent <obj>`,
    # so its USER is the parent object. Deleting an absent tuple is swallowed by `delete_tuples`, so
    # this is safe for objects created before the edge existed and for types that never take one.
    doomed = list(tuples)
    parent = next((t.user for t in tuples if t.relation == "parent" and t.user), None)
    if parent and parent.split(":", 1)[0] in _CHILD_EDGE_PARENT_TYPES:
        doomed.append(ClientTuple(user=obj, relation="child", object=parent))
    await delete_tuples(
        client,
        doomed,
        actor=actor,
        origin=origin,
        retry_attempts=retry_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        retry_max_backoff_seconds=retry_max_backoff_seconds,
    )
    return doomed
