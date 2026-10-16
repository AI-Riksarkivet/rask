"""Authenticate Dapr-delivered routes (pub/sub subscriptions + input bindings).

Dapr delivers events to the SAME FastAPI app that serves the public HTTP API (``/lineage-events``,
``/medallion-event``, the compaction cron route), so without a check any client that can reach the port
can POST a forged CloudEvent — bypassing ``enforce_author`` and poisoning the authoritative lineage
graph (security audit, prod-blocker). When the pod is annotated ``dapr.io/app-token-secret``, Dapr
injects ``APP_API_TOKEN`` into the app **and** adds a ``dapr-api-token`` header to every request it
delivers. This dependency rejects any delivery whose header doesn't match.

The token is sufficient against a public caller because no public path rides a sidecar: the cluster's
Gateway routes each `/api/*` prefix to the app's own port (ADR 0165), so a request from outside the
cluster never carries a sidecar's ``dapr-api-token``, and the route is only registered when Dapr is enabled.
The ``pubsub.jetstream`` component's ``scopes`` decide which app-ids' sidecars load it, which is not who may
publish: every scoped app can publish every topic on it, and a client that reaches the broker directly needs no sidecar. So the token authenticates the sidecar
that delivered an event and nothing about who wrote it; a door that acts on what an event claims verifies
the producer's signature (``lineage_kit.signing``).

**An unconfigured door REFUSES.** No ``APP_API_TOKEN`` means no caller can be authenticated, and a
guard that cannot authenticate must not admit — ``RASK_ALLOW_UNAUTHENTICATED_DAPR`` is the one way to
run open, and it has to be set by hand.
"""

from __future__ import annotations

import functools
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Annotated, Final

from fastapi import FastAPI, Header, Request
from lance_namespace import PermissionDeniedError, ServiceUnavailableError
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.responses import Response

from service_kit.governed import secret_cache
from service_kit.governed.secret_cache import StoreRead


log = logging.getLogger(__name__)


class DaprDoorSettings(BaseSettings):
    """Everything this module reads from the environment, in ONE declared place (SKG-10).

    It was four bare ``os.environ.get`` calls — three of them naming ``APP_API_TOKEN`` in three
    functions — so the variables this door depends on were discoverable only by grep, and a typo in
    any one of them read as "unconfigured" at a different site than the one that was misspelt. A
    settings class makes the set enumerable and each read one lookup against a declared field.

    Constructed PER READ, never cached. The process environment is the source of truth for both
    values and it is what the estate's tests and an operator manipulate; a module-level instance would
    freeze whatever was set at import time, which for a token this door authenticates against is the
    difference between "the guard is configured" and "the guard was configured when this module first
    loaded". The cost is one small model construction on a sidecar-delivered request.
    """

    # NO `populate_by_name`, deliberately. It would teach the env source a SECOND lookup name per
    # field — the bare one — so `app_token_field` would answer to `$APP_TOKEN_FIELD` as well as the
    # `RASK_APP_TOKEN_FIELD` it declares, which is precisely what
    # `tests/unit/test_settings_env_namespace.py` exists to refuse. Nothing constructs this by field
    # name, so the convenience it buys is unused here.
    model_config = SettingsConfigDict(extra="ignore")

    #: DAPR'S OWN NAME, and it is deliberately unprefixed. daprd injects it from
    #: ``dapr.io/app-token-secret``; renaming it to ``RASK_*`` would simply mean the sidecar sets a
    #: variable nothing reads and this door authenticates against nothing.
    app_api_token: str | None = Field(default=None, alias="APP_API_TOKEN")

    #: Permit an UNCONFIGURED door — never a wrong token. Owner decision 2026-09-15.
    #:
    #: THE LOCAL LOOP IS WHY AN OPEN DOOR EXISTS AT ALL: `make dev-micro` runs these apps with no
    #: sidecar and no secret store, so every Dapr-guarded route would be unreachable without it. What
    #: the flag buys over an implicit skip is that the decision is made by a person and is greppable —
    #: an estate-wide search finds every deployment running open, which an absent variable cannot be.
    #:
    #: Measured 2026-09-15, which is why it is opt-IN rather than opt-out: `rask-annotator` carried no
    #: `APP_API_TOKEN` and its sidecar-only `/dapr/config` answered 200 to a caller presenting none.
    #: Nothing in the render, the chart or a probe distinguished that from a configured door.
    allow_unauthenticated_dapr: bool = Field(default=False, alias="RASK_ALLOW_UNAUTHENTICATED_DAPR")

    #: Resolve the expected token from the Dapr SECRET STORE rather than from the environment.
    #:
    #: The estate rule is that a secret never travels through env — not process env, not a k8s Secret
    #: via `secretKeyRef`, which is what `APP_API_TOKEN` is today for eleven deployments. A pod with a
    #: sidecar already has the sanctioned path in it (`GET /v1.0/secrets/<store>/<key>`), so this is a
    #: per-deployment switch onto it rather than new plumbing.
    #:
    #: A MODE, NOT A FALLBACK CHAIN. On, the store is the STRICT sole source and `APP_API_TOKEN` is not
    #: consulted at all — a chain would mean a store outage silently promoted a stale env value back to
    #: authoritative, which is the failure the rule exists to prevent. Off, env, exactly as before. That
    #: is the same shape the S3 key pair takes (`service_kit.governed.store_credential`).
    app_token_from_store: bool = Field(default=False, alias="RASK_APP_TOKEN_FROM_STORE")

    #: Which bundle holds it. The names match the per-service `*_DAPR_SECRET_STORE` / `*_DAPR_SECRET_KEY`
    #: settings the lance services already carry, so a deployment configures ONE store, not two.
    secret_store: str = Field(default="lance-secrets", alias="RASK_SECRET_STORE")
    secret_key: str = Field(default="lance", alias="RASK_DAPR_SECRET_KEY")
    #: The field within the bundle. Hyphenated because that is how every other field in `secret/lance`
    #: is spelled (`catalog-s3-secret-key`, `dapr-app-token`) — an underscore here would read
    #: as an env var, which is the one thing this value must never be.
    app_token_field: str = Field(default="dapr-app-token", alias="RASK_APP_TOKEN_FIELD")
    #: The token a rotation replaced, accepted beside the current one ([[XC-082]], ADR 0166). daprd reads its
    #: token from `<release>-dapr-app-token` once, when the pod starts, and ESO syncs that Secret from the store
    #: on its own interval, so a sidecar keeps presenting the old token until its pod restarts, and a pod that
    #: restarts before ESO synced still starts with it. Absent or empty outside a rotation.
    app_token_previous_field: str = Field(default="dapr-app-token-previous", alias="RASK_APP_TOKEN_PREVIOUS_FIELD")


class _NoCurrentToken(LookupError):
    """The store answered without the current token: the door is unconfigured, and the answer is never cached."""


def _read_app_tokens(read: StoreRead) -> dict[str, str]:
    """The current and previous tokens from one fetch: a single attempt, because a request is waiting on it.

    Raises:
        SecretStoreUnreadable: The store did not answer.
        _NoCurrentToken: It answered without the current token, as it does before the seed lands.
    """
    from service_kit.governed.secrets import fetch_dapr_secret  # imported here, not at module scope: keeps this module import-light

    current, previous = read.fields
    bundle = fetch_dapr_secret(read.store, read.secret, retries=1)
    if not bundle:
        raise SecretStoreUnreadable(f"secret store {read.store!r} unreadable")
    if not bundle.get(current):
        raise _NoCurrentToken(current)
    return {current: bundle[current], previous: bundle.get(previous, "")}


def expected_app_tokens() -> tuple[str, ...]:
    """The tokens every door on this app accepts, the current one first: from the store, or from env.

    ONE resolver for every door, because a deployment that moves its token to the store must move every
    door with it: one door still reading env while another reads the store is a pod where half the
    credentials are configured and nothing says which half.

    In store mode the tokens are the store's current and previous ones, read through
    `service_kit.governed.secret_cache.STORE_READS`: held for its TTL, so a request costs a dict lookup and a
    token the store replaced or removed stops being accepted once the TTL passes, with no restart. An empty
    tuple means this deployment has no token, which the doors refuse. A store that answers without the
    current token is such a deployment until the seed lands, and is asked again on the next request, since
    a miss is never cached. An unreadable store RAISES rather than answering empty: the absent-vs-unreadable
    split the estate enforces everywhere else.

    Raises:
        SecretStoreUnreadable: The store did not answer.
    """
    door = DaprDoorSettings()
    if not door.app_token_from_store:
        return (door.app_api_token,) if door.app_api_token else ()
    read = StoreRead(store=door.secret_store, secret=door.secret_key, fields=(door.app_token_field, door.app_token_previous_field))
    try:
        tokens = secret_cache.STORE_READS.get(read, functools.partial(_read_app_tokens, read))
    except _NoCurrentToken:
        return ()
    return tuple(token for field in read.fields if (token := tokens[field]))


def missing_app_token_knob() -> str:
    """Name the knob that is not set, for a refusal an operator can act on.

    BOTH doors phrase it through here, because a message that names a variable the pod does not read
    sends the reader to the one place the answer is correctly absent — a store-mode pod told to check
    `APP_API_TOKEN` shows an env with no such row, and the honest conclusion from that is that the
    door is lying. Naming the knob is the contract the service door's own tests pin, and it is only
    worth pinning if the knob named is the real one.
    """
    door = DaprDoorSettings()
    if door.app_token_from_store:
        return f"{door.app_token_field!r} is absent from the Dapr secret store {door.secret_store}/{door.secret_key}"
    return "APP_API_TOKEN is unset"


def refuse_unconfigured_door(*, caller: str | None = None) -> None:
    """Raise unless this deployment has DECLARED that an unauthenticated door is acceptable.

    THE ONE PLACE THE UNSET-TOKEN RULE LIVES, and it is a function because it was two answers. One
    door refusing while another admitted taught a reader of one the wrong rule about the other. Owner ruling
    2026-09-19: an unset token is a refusal everywhere (`docs/adr/0066-four-rulings-on-the-lakehouse-s-unblocked-rows-owner-2026.md`).

    FAILING CLOSED IS NOT SEVERITY, IT IS THE CONTROL EXISTING. A guard with no expected value cannot
    distinguish a legitimate delivery from a forged one, so admitting is the control being absent while
    every probe reports it present. Measured 2026-09-15: an actor host with no token answered 200 to a
    caller presenting none.

    The hatch says "unconfigured is acceptable here", never "wrong tokens are acceptable" — a configured
    door still refuses a forged one regardless of this flag.
    """
    door = DaprDoorSettings()
    if not door.allow_unauthenticated_dapr:
        remedy = "seed that field" if door.app_token_from_store else "wire dapr.io/app-token-secret + APP_API_TOKEN"
        raise PermissionDeniedError(
            f"this Dapr door is not configured: {missing_app_token_knob()}, so no caller can be authenticated. "
            f"Either {remedy}, or set RASK_ALLOW_UNAUTHENTICATED_DAPR to accept an unauthenticated door deliberately."
        )
    log.warning("dapr_door_unauthenticated", extra={"caller": caller or "<unset>"})


def require_dapr_token(
    dapr_api_token: Annotated[str | None, Header()] = None,
    # The INVOKING Dapr app-id, logged when an unconfigured door admits: every route guarded by this
    # dependency is delivered by the app's OWN sidecar, so a service-invocation caller is anomalous.
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> None:
    """FastAPI dependency: reject a sidecar-delivered request whose ``dapr-api-token`` header doesn't
    match the app's ``APP_API_TOKEN`` (set by Dapr from ``dapr.io/app-token-secret``).

    An UNSET ``APP_API_TOKEN`` is a refusal, not a skip: the door cannot authenticate anybody, so it
    admits nobody. ``RASK_ALLOW_UNAUTHENTICATED_DAPR`` reopens it for a deployment that means to run
    open. ``assert_app_token_configured`` remains the earlier, louder version of the same rule — a
    startup error beats a per-request 403 — but only the six services that CALL it get that, which is
    why this door carries the check as well rather than relying on it.

    """
    # A DOMAIN ERROR, not `HTTPException`, so the refusal wears the same RFC 9457 envelope as every
    # other error in these services. The bare form was mapped by NO app, so this one 403 arrived as
    # FastAPI's default `{"detail": ...}` body while its neighbours carried `type`/`title`/`code`.
    #
    # `PermissionDeniedError` rather than `service_kit.exceptions.ForbiddenError`, and that is verified
    # rather than preferred: this module is shared by both planes and they install different handlers.
    # The four lance apps install `install_problem_handlers` ONLY, so a fleet `DomainError` would fall
    # to the catch-all and answer 500 there. A `LanceNamespaceError` is mapped by both — the lance apps
    # directly, the fleet apps since `make_service_app` began installing the same translator.
    try:
        expected = expected_app_tokens()
    except SecretStoreUnreadable as outage:
        # 503, NOT 403. A store that will not answer is an outage, and answering 403 would tell the
        # sidecar its credential was rejected — so it would stop retrying a delivery that is going to
        # start working again. `ServiceUnavailableError` is `lance_namespace`'s, which both planes'
        # problem handlers map; the fleet's own `ServiceUnavailableError` is a different class and is
        # mapped by only one of them.
        raise ServiceUnavailableError(f"cannot authenticate this Dapr delivery: {outage}") from outage
    # FAIL CLOSED WHEN UNCONFIGURED. A guard with no expected value cannot distinguish a
    # legitimate delivery from a forged one, so admitting is not leniency — it is the control being
    # absent while every probe reports it present. Measured 2026-09-15: an actor host with no token
    # answered 200 to a caller presenting none.
    if not expected:
        refuse_unconfigured_door(caller=dapr_caller_app_id)
        return
    # compare_digest: the token is the only guard on these routes, so no timing side-channel; bytes
    # (not str) so a non-ASCII header value is a clean 403, never a TypeError. Every accepted token is
    # compared, a list rather than a short-circuit, so the time taken does not say which one matched.
    presented = (dapr_api_token or "").encode()
    matches = [secrets.compare_digest(presented, token.encode()) for token in expected]
    if not any(matches):
        raise PermissionDeniedError("invalid or missing Dapr app-api-token")


def assert_app_token_configured(*, dapr_enabled: bool) -> None:
    """Fail closed at startup: when Dapr ingest is enabled the delivery route is live and MUST be
    authenticated, so an unset/blank ``APP_API_TOKEN`` is a misconfiguration — not the dev default — and
    the pod must refuse to start rather than silently expose an unauthenticated ingest path (the security
    audit's 'blanked token silently reopens the route' residual). No-op when Dapr ingest is off.

    `require_dapr_token` refuses an unconfigured door on its own, so this is no longer the only thing
    standing between a blank token and an open route — it is the EARLIER and louder version of the same
    answer, and a boot failure beats a per-request 403 on a route whose caller is a sidecar with no
    person behind it.

    In store mode it fetches with the BOOT retry budget rather than the request-path one: this runs in
    a lifespan, where a store still seeding is the expected condition and waiting is the correct
    response. The request path cannot afford that and reads with a single attempt (`expected_app_tokens`)."""
    if not dapr_enabled:
        return
    door = DaprDoorSettings()
    if door.app_token_from_store:
        from service_kit.governed.secrets import fetch_required_secrets  # imported here, not at module scope: keeps this module import-light

        fetch_required_secrets(door.secret_store, door.secret_key, require=door.app_token_field)
        return
    if not door.app_api_token:
        raise RuntimeError(
            "APP_API_TOKEN must be set when Dapr ingest is enabled — the delivery route would otherwise be "
            "unauthenticated. Wire dapr.io/app-token-secret + the APP_API_TOKEN env, or set "
            "RASK_APP_TOKEN_FROM_STORE to take it from the Dapr secret store instead."
        )


# ── the store behind the app token ──────────────────────────────────────────────────


class SecretStoreUnreadable(RuntimeError):
    """The credential bundle could not be READ — distinct from a genuinely absent field.

    Callers map this to their 503 problem type. Conflating it with "no credential provisioned"
    would turn a secret-store outage into the same 401 as a misconfigured subject — the
    absent-vs-unreadable rule the estate already enforces in the state plane."""


#: What `dapr.ext.fastapi.DaprActor` mounts at the ROOT of an app — outside `settings.api_prefix`, and
#: therefore outside every dependency the domain routers declare.
ACTOR_PATH_PREFIXES: Final = ("/actors", "/dapr/config")


def guard_actor_routes(app: FastAPI) -> None:
    """Require the sidecar's own token on `DaprActor`'s callback surface.

    **The hole this closes.** `DaprActor(app)` root-mounts
    `PUT /actors/{actor_type}/{actor_id}/method/{method}`, and an actor is typically the service's
    PRIVATE store — keyed by an id that is rarely a secret. The domain routers' doors (OIDC, FGA,
    plane-readiness) hang on the ROUTER under `settings.api_prefix`; these routes are mounted at the
    root and inherit none of them. So anything able to reach the pod reads any actor's state BY NAME:

        curl -XPUT :8850/actors/InboxActor/YWxpY2U/method/Page -d '{"state":"all","limit":100}'

    `YWxpY2U` is `base64url("alice")`. Where the id derives from a subject, it is readable off lineage
    author facets or a project listing — it was never meant to carry access control.

    `require_dapr_token` is the right door rather than a stand-in: these paths are BY CONSTRUCTION
    sidecar-delivered — daprd calls them back presenting `dapr-api-token`. It proves "arrived via
    Dapr", never "trusted caller".

    A middleware rather than a router dependency because the SDK adds these routes to the app itself:
    there is no `include_router` call to hang `dependencies=` on, and rewriting a mounted route's
    dependant after the fact is far more fragile than matching a path prefix.

    Call it immediately after constructing `DaprActor(app)`. It is a no-op for every other path, so a
    service that mounts no actors can call it harmlessly — but there is no reason to.
    """
    from fastapi.responses import JSONResponse  # imported here, not at module scope: keeps this module import-light
    from starlette.concurrency import run_in_threadpool
    from starlette.middleware.base import BaseHTTPMiddleware

    from service_kit.lakehouse.ns_errors import problem_detail

    async def _guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path.startswith(ACTOR_PATH_PREFIXES):
            try:
                # IN THE THREADPOOL, as FastAPI runs the sync dependency elsewhere: a token whose TTL passed is
                # read from the sidecar, and that read must not stall the event loop every other request shares.
                await run_in_threadpool(
                    require_dapr_token,
                    dapr_api_token=request.headers.get("dapr-api-token"),
                    dapr_caller_app_id=request.headers.get("dapr-caller-app-id"),
                )
            except PermissionDeniedError as refusal:
                # RENDERED HERE, because middleware cannot reach the app's exception handlers: a
                # `BaseHTTPMiddleware` raise happens outside `ExceptionMiddleware`, so an unrendered
                # domain error would answer 500 instead of 403. `problem_detail` is the SAME translator
                # the handler uses, so the refusal a caller sees is byte-identical whether it came from
                # the dependency or from here — which is the point of moving off `HTTPException` at all.
                status, body = problem_detail(refusal)
                return JSONResponse(body, status_code=status, media_type="application/problem+json")
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=_guard)
