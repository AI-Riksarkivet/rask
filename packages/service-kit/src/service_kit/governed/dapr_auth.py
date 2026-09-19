"""Authenticate Dapr-delivered routes (pub/sub subscriptions + input bindings).

Dapr delivers events to the SAME FastAPI app that serves the public HTTP API (``/lineage-events``,
``/medallion-event``, the compaction cron route), so without a check any client that can reach the port
can POST a forged CloudEvent — bypassing ``enforce_author`` and poisoning the authoritative lineage
graph (security audit, prod-blocker). When the pod is annotated ``dapr.io/app-token-secret``, Dapr
injects ``APP_API_TOKEN`` into the app **and** adds a ``dapr-api-token`` header to every request it
delivers. This dependency rejects any delivery whose header doesn't match.

Defense-in-depth (the token is one layer): the ``pubsub.jetstream`` component is **scoped** to the
trusted app-ids (only they can publish to the topic), the gateway **blocks** these routes from external
traffic, and the route is only registered when Dapr is enabled.

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


#: Dapr app-ids whose invocations may NEVER take a service-token path — the PUBLIC front doors.
#:
#: A MEASURED bypass, not a hypothetical. ``dapr.io/app-token-secret`` makes daprd stamp
#: ``dapr-api-token`` on every request it hands the app, and the gateway forwards ``/api/*`` through
#: DAPR SERVICE INVOCATION. So an anonymous public request arrives at a backend already holding a
#: valid service token. Measured against the ingest door: 403 straight to the pod, 403 via Service
#: DNS, **202 through the gateway** — and a browser with no login started a real data-writing run.
#:
#: The token proves "arrived through Dapr", never "the caller is a trusted service". The gateway IS a
#: trusted service; it is just invoking on behalf of someone who is not.
_DEFAULT_PUBLIC_CALLERS = "gateway"


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
    # field — the bare one — so `public_callers` would answer to `$PUBLIC_CALLERS` as well as the
    # `RASK_PUBLIC_CALLERS` it declares, which is precisely what
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
    #: is the same shape `apply_dapr_secrets` already uses for the S3 credential.
    app_token_from_store: bool = Field(default=False, alias="RASK_APP_TOKEN_FROM_STORE")

    #: Which bundle holds it. The names match the per-service `*_DAPR_SECRET_STORE` / `*_DAPR_SECRET_KEY`
    #: settings the lance services already carry, so a deployment configures ONE store, not two.
    secret_store: str = Field(default="lance-secrets", alias="RASK_SECRET_STORE")
    secret_key: str = Field(default="lance", alias="RASK_DAPR_SECRET_KEY")
    #: The field within the bundle. Hyphenated because that is how every other field in `secret/lance`
    #: is spelled (`catalog-s3-secret-key`, `service-token-<identity>`) — an underscore here would read
    #: as an env var, which is the one thing this value must never be.
    app_token_field: str = Field(default="dapr-app-token", alias="RASK_APP_TOKEN_FIELD")

    #: The public front-door app-ids (comma-separated). ONE list for the whole estate — a per-service
    #: setting would let one deployment forget an edge the others know about, and the set is a
    #: property of the topology rather than of any single service.
    public_callers: str = Field(default=_DEFAULT_PUBLIC_CALLERS, alias="RASK_PUBLIC_CALLERS")


def public_callers() -> frozenset[str]:
    """The configured public front-door app-ids, lower-cased — ONE list for the whole estate."""
    return frozenset(caller.strip().lower() for caller in DaprDoorSettings().public_callers.split(",") if caller.strip())


def is_public_caller(caller: str | None) -> bool:
    """True when the invocation came through a front door that faces the public internet.

    An ABSENT header is NOT public, and that is load-bearing rather than lenient: pub/sub delivery,
    input-binding delivery and a direct Service-DNS call all arrive with no ``dapr-caller-app-id``,
    and those are exactly the legitimate paths onto every route this module guards. Treating absence
    as public would break the whole cascade while closing nothing.

    Soundness depends on a client being unable to SET the header. daprd APPENDS its stamp rather than
    replacing a client's, and FastAPI binds the FIRST occurrence, so a forged value wins — measured:
    ``dapr-caller-app-id: medallion`` through the public gateway got a 202 out of a door that had
    already been fixed. The gateway strips these from every inbound request (``_CLIENT_SPOOFABLE``);
    that strip is what makes this check mean anything.
    """
    if not caller:
        return False
    return caller.strip().lower() in public_callers()


def expected_app_token() -> str | None:
    """The token every door on this app authenticates against — from the store, or from env.

    ONE resolver for all three consumers (`require_dapr_token`, `service_principal`,
    `assert_app_token_configured`), because a deployment that moves its token to the store must move
    every door with it: a service door still reading env while the Dapr door reads the store is a pod
    where half the credentials are configured and nothing says which half.

    Cached through `_secret_bundle` (per process, per store+key), so the store is read once and the
    per-request cost is a dict lookup. An unreadable store RAISES rather than answering `None` — the
    absent-vs-unreadable split the estate enforces everywhere else: `None` here would mean "this
    deployment has no token", and a caller would be refused for a reason that is not true.
    """
    door = DaprDoorSettings()
    if not door.app_token_from_store:
        return door.app_api_token
    token = dict(_secret_bundle(door.secret_store, door.secret_key)).get(door.app_token_field) or None
    if token is None:
        # DROP THE CACHE, because a bundle that answered WITHOUT the field is the one failure this
        # cache turns permanent. `_secret_bundle` never caches an exception, so an unreadable store
        # heals by itself — but a store that answers a bundle the seed has not written yet caches a
        # successful miss, and every later request reads it for the life of the process. That is the
        # live ordering: the chart's seed Job and this pod roll in the same `helm upgrade`, so a pod
        # that wins the race would refuse every delivery until somebody restarted it, long after the
        # field was there. Uncached is the correct cost while the field is missing: the state is
        # broken, the refetch is one sidecar hop, and it stops the moment the seed lands.
        _secret_bundle.cache_clear()
    return token


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

    THE ONE PLACE THE UNSET-TOKEN RULE LIVES, and it is a function because it was two answers. Every
    sidecar-delivered door refused; `medallion.api.produce_auth.authorize_produce` returned a
    service-caller result and admitted. Nothing in either said which was intended, so a reader of one
    learned the wrong rule about the other — and the door that opened is the cascade head. Owner ruling
    2026-09-19: an unset token is a refusal everywhere (`docs/DECISIONS.md`).

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
    # The INVOKING Dapr app-id. Every route guarded by this dependency is delivered by the app's OWN
    # sidecar — a pub/sub subscription or an input binding — so a SERVICE-INVOCATION caller is already
    # anomalous, and a PUBLIC front door invoking one is the measured bypass.
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> None:
    """FastAPI dependency: reject a sidecar-delivered request whose ``dapr-api-token`` header doesn't
    match the app's ``APP_API_TOKEN`` (set by Dapr from ``dapr.io/app-token-secret``), and reject ANY
    invocation that arrived through a public front door.

    An UNSET ``APP_API_TOKEN`` is a refusal, not a skip: the door cannot authenticate anybody, so it
    admits nobody. ``RASK_ALLOW_UNAUTHENTICATED_DAPR`` reopens it for a deployment that means to run
    open. ``assert_app_token_configured`` remains the earlier, louder version of the same rule — a
    startup error beats a per-request 403 — but only the six services that CALL it get that, which is
    why this door carries the check as well rather than relying on it.

    The public-caller refusal is deliberately NOT conditional on the token. These routes are
    sidecar-delivery-only by construction, so a front-door invocation of one is never legitimate in
    any environment — including a deployment that has taken the unauthenticated hatch.

    Threading the caller costs nothing at the call sites: this is consumed exclusively as
    ``Depends(...)``, so FastAPI resolves the new header itself and every door it guards is fixed
    without touching a single endpoint signature.
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
    if is_public_caller(dapr_caller_app_id):
        raise PermissionDeniedError(f"{dapr_caller_app_id!r} is a public front door: its Dapr app-token authenticates the proxy, not the caller")
    try:
        expected = expected_app_token()
    except SecretStoreUnreadable as outage:
        # 503, NOT 403. A store that will not answer is an outage, and answering 403 would tell the
        # sidecar its credential was rejected — so it would stop retrying a delivery that is going to
        # start working again. `ServiceUnavailableError` is `lance_namespace`'s, which both planes'
        # problem handlers map; the fleet's own `ServiceUnavailableError` is a different class and is
        # mapped by only one of them.
        raise ServiceUnavailableError(f"cannot authenticate this Dapr delivery: {outage}") from outage
    # FAIL CLOSED WHEN UNCONFIGURED, which is the same answer `service_principal` below gives to the
    # identical condition (`ServiceDoorClosed`). A guard with no expected value cannot distinguish a
    # legitimate delivery from a forged one, so admitting is not leniency — it is the control being
    # absent while every probe reports it present. Measured 2026-09-15: an actor host with no token
    # answered 200 to a caller presenting none.
    if not expected:
        refuse_unconfigured_door(caller=dapr_caller_app_id)
        return
    # compare_digest: the token is the only guard on these routes, so no timing side-channel; bytes
    # (not str) so a non-ASCII header value is a clean 403, never a TypeError.
    if not secrets.compare_digest((dapr_api_token or "").encode(), expected.encode()):
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
    response. The request path cannot afford that and uses `_secret_bundle`'s single attempt."""
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


# ── the SERVICE DOOR: an in-cluster caller authenticating AS a named service ──────────


class ServiceIdentity:
    """A service principal — an in-cluster caller that is not a human.

    `sub` is the bare FGA subject, so every authorization decision downstream reads the same way for
    a service as for a person. That symmetry is the point: a service is bounded by its own rung, not
    exempt from the model.
    """

    __slots__ = ("_sub",)

    def __init__(self, sub: str) -> None:
        self._sub = sub

    @property
    def sub(self) -> str:
        return self._sub

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ServiceIdentity({self._sub!r})"


class ServiceDoorError(Exception):
    """Base of every refusal the shared service door issues.

    THE DOOR DECIDES, THE CALL SITE RENDERS. `service_kit` must not pick the wire shape: both
    consumers answer in RFC 9457 problem+json through `service_kit.lakehouse.ns_errors`, and a raw
    `fastapi.HTTPException` slips past those handlers as a bare ``{"detail": …}`` body — the exact
    contract each of their module docstrings promises not to break. So the door raises these neutral
    types and each call site maps them onto its own error vocabulary. One decision, two renderings,
    never two decisions.
    """


class ServiceDoorClosed(ServiceDoorError):
    """No ``APP_API_TOKEN`` here: the service door does not exist in this deployment.

    THE UNIFIED NO-CREDENTIAL ANSWER IS A REFUSAL THAT NAMES ITSELF (401), NOT A FALL-THROUGH TO
    OIDC. This class used to say the opposite and the two call sites disagreed accordingly —
    lineage refused, the catalog swallowed it and re-asked OIDC. The refusal
    wins on three counts:

      * REACHING THIS DOOR IS DELIBERATE. A caller only gets here by sending BOTH ``dapr-api-token``
        and ``x-lance-service-identity``; the gateway strips both at the edge
        (`gateway/__init__.py` ``_CLIENT_SPOOFABLE``) and the zones' BFF sends them only when there
        is no session. Such a request has asked to be authenticated AS A SERVICE, so the service
        door is the only door it gets — quietly answering with a different one is the surprise.
      * OTHERWISE THE LESS-CONFIGURED DEPLOYMENT IS THE MORE PERMISSIVE ONE. With the door
        configured, a rejected service credential is a 401 with no OIDC second chance; falling
        through would hand that same request a second chance precisely when the door is missing.
      * DIAGNOSTICS. An operator who set the allowlist but forgot ``APP_API_TOKEN`` reads "Missing
        bearer token" under a fall-through and goes hunting in the IdP. This names the missing knob.

    It does NOT re-create the 2026-08-06 outage. That refusal fired on ``dapr-caller-app-id``, which
    the SIDECAR stamps on every request it delivers — so it caught humans who never asked for this
    door. These two headers are the caller's own choice. Nor does it close anything a fall-through
    opened: OIDC admits only a valid bearer either way, so the only caller whose answer changes is
    one holding a valid bearer AND both service headers, and the honest answer to them is "you asked
    for the service door".
    """


class SubjectNotAllowed(ServiceDoorError):
    """The claimed subject is not on this deployment's allowlist — 403 at every call site."""


class CredentialRejected(ServiceDoorError):
    """The presented credential may not be the claimed subject — 401 at every call site.

    Covers all three ways the second question is answered "no": the shared token presented for a
    privileged subject, a privileged subject whose dedicated credential was never provisioned, and a
    plain wrong token. They are one refusal on purpose — a caller must not be able to tell which.
    """


class SecretStoreUnreadable(RuntimeError):
    """The credential bundle could not be READ — distinct from a genuinely absent field.

    Callers map this to their 503 problem type. Conflating it with "no credential provisioned"
    would turn a secret-store outage into the same 401 as a misconfigured subject — the
    absent-vs-unreadable rule the estate already enforces in the state plane."""


@functools.lru_cache(maxsize=8)
def _secret_bundle(store: str, key: str) -> tuple[tuple[str, str], ...]:
    """The secret bundle, fetched once per (store, key) and cached for the process lifetime.

    retries=1, not the boot budget: this is reached from sync REQUEST dependencies (the AnyIO
    threadpool), where 10 exponential-backoff attempts stalled a worker for minutes per cold call
    (the viewer's per-store reads set the precedent). An unreadable store
    RAISES — lru_cache never caches exceptions, so the next request retries — and a successful
    bundle is cached until restart: rotating a dedicated credential means a rollout, the same
    trade the viewer already made."""
    from service_kit.governed.secrets import fetch_dapr_secret  # imported here, not at module scope: keeps this module import-light

    bundle = fetch_dapr_secret(store, key, retries=1)
    if not bundle:
        raise SecretStoreUnreadable(f"secret store {store!r} unreadable — cannot verify a privileged service identity")
    return tuple(bundle.items())


def dedicated_token_from_store(store: str, key: str) -> Callable[[str], str | None]:
    """The estate's ONE resolver for a privileged subject's dedicated credential.

    Lifted from `services/lineage`: the shared `service_principal` grew the
    `dedicated_token=` parameter for exactly this callback, but the catalog never passed one — so
    its privileged door hard-refused every privileged subject with "no dedicated credential
    provisioned" no matter what was seeded — while lineage kept a private fork of the resolver.
    Both halves are closed now: one resolver, one door. Returns ``None`` only when the bundle was READ and
    ``service-token-<identity>`` is genuinely absent; an unreadable store raises
    :class:`SecretStoreUnreadable` (§2.17's absent-vs-unreadable split rides along)."""

    def _resolve(identity: str) -> str | None:
        return dict(_secret_bundle(store, key)).get(f"service-token-{identity}") or None

    return _resolve


def service_principal(
    *,
    token: str | None,
    identity: str | None,
    allowed_subjects: str,
    privileged_subjects: str = "",
    dedicated_token: Callable[[str], str | None] | None = None,
) -> ServiceIdentity:
    """Authenticate an in-cluster service: a valid credential + an ALLOWLISTED subject.

    THE ESTATE'S ONLY SERVICE DOOR. Extracted from `services/lineage` when the catalog needed the
    same door — but for a while lineage kept its own copy alongside, and the two answered the
    no-credential question differently. Two doors with different answers to the
    same question is worse than either answer: whichever one an auditor reads, the other is live.
    There is now one body here and two thin renderings at the call sites.

    The lessons it carries were each paid for once and must not be re-learned per service:

      * TWO questions, not one. The allowlist answers "may this SUBJECT use the door"; the credential
        answers "may THIS CALLER be that subject". Without the second, the identity is a claim the
        door BELIEVES — and with one shared token across an allowlist, any holder can pick the
        highest-privileged name on it.
      * PRIVILEGED subjects need their own credential, and a missing one FAILS CLOSED rather than
        falling back to the shared token. A quiet fallback restores the escalation while looking
        configured, which is worse than not having the control.
      * The allowlist is checked FIRST, so an unknown caller-supplied subject never reaches the
        credential store — a door that looks up arbitrary names is an enumeration oracle.

    Every refusal is a :class:`ServiceDoorError`, never a `fastapi.HTTPException` — see that class.
    `ServiceDoorClosed` (no `APP_API_TOKEN`) is a REFUSAL both call sites render as 401, not a
    fall-through signal; its docstring carries the reasoning.

    `dedicated_token` is not optional in practice — omitting it makes the privileged branch refuse
    every privileged subject no matter what the store holds, which is exactly how §2.8 shipped. It
    stays keyword-defaulted only so a deployment with an empty `privileged_subjects` need not build
    a resolver it will never call.
    """
    expected = expected_app_token()
    if not expected:
        raise ServiceDoorClosed(f"the service door is not configured here: {missing_app_token_knob()}")

    allowed = {s.strip() for s in allowed_subjects.split(",") if s.strip()}
    if not identity or identity not in allowed:
        raise SubjectNotAllowed(f"service identity not allowed: {identity or '<missing>'}")

    privileged = {s.strip() for s in privileged_subjects.split(",") if s.strip()}
    if identity in privileged:
        dedicated = dedicated_token(identity) if dedicated_token else None
        if not dedicated:
            raise CredentialRejected(f"service identity {identity!r} is privileged but has no dedicated credential provisioned")
        if not secrets.compare_digest((token or "").encode(), dedicated.encode()):
            raise CredentialRejected(f"the presented credential may not claim {identity!r}")
        return ServiceIdentity(identity)

    if not secrets.compare_digest((token or "").encode(), expected.encode()):
        raise CredentialRejected("invalid service token")
    return ServiceIdentity(identity)


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
    sidecar-delivered — daprd calls them back presenting `dapr-api-token` — and the same dependency
    refuses a public front door outright even where the token check has been waived. It proves
    "arrived via Dapr", never "trusted caller".

    A middleware rather than a router dependency because the SDK adds these routes to the app itself:
    there is no `include_router` call to hang `dependencies=` on, and rewriting a mounted route's
    dependant after the fact is far more fragile than matching a path prefix.

    Call it immediately after constructing `DaprActor(app)`. It is a no-op for every other path, so a
    service that mounts no actors can call it harmlessly — but there is no reason to.
    """
    from fastapi.responses import JSONResponse  # imported here, not at module scope: keeps this module import-light
    from starlette.middleware.base import BaseHTTPMiddleware

    from service_kit.lakehouse.ns_errors import problem_detail

    async def _guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path.startswith(ACTOR_PATH_PREFIXES):
            try:
                require_dapr_token(
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
