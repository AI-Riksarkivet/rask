"""Fine-grained authorization via OpenFGA (Zanzibar-style relationship checks).

The authorization model (``services/common/auth/model.fga`` / ``model.json``) defines nine types —
``user``, ``team`` (with ``team#member`` as a group subject), ``role`` (``role#assignee``), ``project``,
``warehouse`` (the S3-bucket root), a self-nesting ``namespace``, ``table``, plus ``materialized_view`` and
``transaction`` (both hanging off namespace/warehouse and inheriting the same rungs via ``parent``).
Privileges are concentric (``owner`` ⊇ ``writer`` ⊇ ``reader``, plus a separate ``validator`` rung gating
``can_promote``) and cascade DOWN the hierarchy via ``parent`` (team → project → warehouse → namespace →
nested namespace → table / materialized_view / transaction). Every API operation is checked against a
``can_*`` ACTION relation — the model owns the op→privilege map, the app just names the action (see
``services/catalog/api/fga_deps.py``). Tuples persist in the OpenFGA datastore (Postgres in the deployed
stack; SQLite for the auth-e2e).

Resilience: ``check`` / ``batch_check`` / ``list_objects`` and the post-create grant
writes go through a bounded retry with exponential backoff + jitter (tenacity). Only
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
from pathlib import Path
from typing import Any

import aiohttp
from lance_namespace import ServiceUnavailableError
from openfga_sdk import ClientConfiguration, OpenFgaClient
from openfga_sdk.client.models import (
    ClientBatchCheckItem,
    ClientBatchCheckRequest,
    ClientCheckRequest,
    ClientListObjectsRequest,
    ClientTuple,
    ClientWriteRequest,
)
from openfga_sdk.client.models.list_users_request import ClientListUsersRequest
from openfga_sdk.configuration import RetryParams
from openfga_sdk.exceptions import ApiException
from openfga_sdk.models.create_store_request import CreateStoreRequest
from openfga_sdk.models.fga_object import FgaObject
from openfga_sdk.models.read_request_tuple_key import ReadRequestTupleKey
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
            )
        )
    return store_id, written.authorization_model_id


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


async def check(
    client: OpenFgaClient,
    *,
    user: str,
    relation: str,
    obj: str,
    qualify: bool = True,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> bool:
    """Return whether ``user:<user>`` has ``relation`` on ``obj`` (e.g. ``table:db1$t``).

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
        response = await client.check(ClientCheckRequest(user=subject, relation=relation, object=obj))
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
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> dict[str, bool]:
    """Return ``{object: allowed}`` for one user across many objects in one round-trip.

    A read-only/idempotent authorization path, so it gets the same bounded retry +
    fail-closed treatment as :func:`check` (transient OpenFGA/transport faults retried;
    outage → ``ServiceUnavailableError`` → 503).
    """
    items = [ClientBatchCheckItem(user=f"user:{user}", relation=relation, object=o) for o in objects]

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
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> list[str]:
    """Return the objects of ``object_type`` the user has ``relation`` on (e.g. ``table:…``).

    Read-only/idempotent, so it gets the same bounded retry + fail-closed treatment as
    :func:`check`.
    """

    @_retrying(retry_attempts, retry_backoff_seconds, retry_max_backoff_seconds)
    async def _do_list_objects() -> list[str]:
        response = await client.list_objects(ClientListObjectsRequest(user=f"user:{user}", relation=relation, type=object_type))
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
_LIST_USERS_SERVER_CAP = 1000


async def list_users(
    client: OpenFgaClient,
    *,
    relation: str,
    obj: str,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> list[str]:
    """The ``user:`` subjects holding ``relation`` on ``obj`` (e.g. every reader of ``table:db1$t``),
    sorted — the access-review primitive (#51), the inverse of :func:`list_objects`.

    ListUsers *expands* the model (role assignees, team members, the parent cascade), so the answer is
    effective access, not just direct tuples — exactly what a reviewer needs. A public ``user:*``
    wildcard surfaces as ``"*"`` (the model doesn't write one today, but a review must never hide it).
    Read-only/idempotent, so it gets the same bounded retry + fail-closed treatment as :func:`check`
    (outage → ``ServiceUnavailableError`` → 503 — never an empty list that reads as "nobody").

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
                user_filters=[UserTypeFilter(type="user")],
            )
        )
        subjects: list[str] = []
        for user in response.users or []:
            if getattr(user, "object", None) is not None:
                subjects.append(user.object.id)
            elif getattr(user, "wildcard", None) is not None:
                subjects.append("*")
        if len(subjects) >= _LIST_USERS_SERVER_CAP:
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
                collected.append(ClientTuple(user=key.user, relation=key.relation, object=key.object))
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
        page = [ClientTuple(user=t.key.user, relation=t.key.relation, object=t.key.object) for t in response.tuples or []]
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


async def write_tuples(
    client: OpenFgaClient,
    tuples: list[ClientTuple],
    *,
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
                return
            log.debug("openfga_write_duplicate_skipped")
            return
        log.error("openfga_write_unavailable", exc_info=True)
        raise ServiceUnavailableError("authorization service unavailable") from exc


async def grant_on_create(
    client: OpenFgaClient,
    *,
    user_sub: str,
    resource: str,
    obj_id: str,
    parent_object: str | None = None,
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

    ``obj_id`` must be the canonical, delimited id (see ``canonical_object_id``)
    so this grant and the later authorization ``check`` agree byte-for-byte.

    Idempotent (safe to call again on retry of a create): writing existing tuples
    is swallowed. On OpenFGA outage it fails closed with ``ServiceUnavailableError``.
    """
    obj = f"{resource}:{obj_id}"
    tuples = [ClientTuple(user=f"user:{user_sub}", relation="owner", object=obj)]
    if parent_object:
        tuples.append(ClientTuple(user=parent_object, relation="parent", object=obj))
    await write_tuples(
        client,
        tuples,
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


async def revoke_object_tuples(
    client: OpenFgaClient,
    obj: str,
    *,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    retry_max_backoff_seconds: float = DEFAULT_RETRY_MAX_BACKOFF_SECONDS,
) -> int:
    """Delete EVERY tuple on ``obj`` — the revoke-on-delete cleanup. Returns the count removed.

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
        return 0
    await delete_tuples(
        client,
        tuples,
        retry_attempts=retry_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        retry_max_backoff_seconds=retry_max_backoff_seconds,
    )
    return len(tuples)
