"""Build the OIDC verifier and the FGA client onto `app.state`, once, for any governed service.

WHY THIS EXISTS. Every service with a door carried its own ~30-line copy of this block — twelve doors,
two mechanisms — and a duplicated bootstrap is one a fix cannot travel through. The estate has paid for
that twice in the same shape: the ING-02 blocking-verify fix landed on the ingest copy of a duplicated
auth function and never reached the other copies, and split-horizon discovery was written into five
copies and omitted from the sixth, where every service-to-service test passed anyway because the
service-token path returns before the verifier is touched.

THE POSTURES ARE PARAMETERS, and that is what makes the collapse honest rather than lossy. The copies
were not stylistic variants; they encoded three decisions, and a single-posture helper is why ten of
them survived the first extraction:

  * **`model_role`** (`ModelRole`) — whether this service may CREATE the store and PUBLISH the authorization model.
    **`READER` by default, and the default is the control.** `fga_provision.provision` writes a model the store
    does not hold and OpenFGA mints a new immutable id per write, so every publisher grows the history
    and an older image that publishes adds its older body; one publisher keeps that to the catalog.
    Every service checks against the model its own image carries (`fga_provision.resolve`, [[LH-201]]).
    Measured live 2026-09-09: 1,256 models in the store, the oldest carrying no `pass_grants`,
    `managed_access` or `maintainer`, and three services running images whose vendored `model.fga` had
    no `maintainer` rung at all.
    Pinned by `packages/lakehouse/tests/test_attach_auth_postures.py::test_the_default_posture_RESOLVES_when_unpinned`,
    and the TID251 banned-api entry on `lakehouse.governed.fga_provision.provision` keeps the call at one site.
    A data writer that minted a store and wrote an authorization model would become the source of truth for everyone
    else's permissions, so every service but the catalog resolves read-only and fails closed against an estate that
    has not been bootstrapped. Reading which store exists is not authoring one, which is the distinction
    `fga_provision.resolve` exists for.
  * **`on_failure`** (`BootFailure`) — whether a failed build takes the pod down. `catalog` and `lineage`
    wrap neither construction in a `try` and therefore crash on boot; that is deliberate and it is
    the only signal an operator watches. Defaulting them into the swallowing posture would convert a
    CrashLoopBackOff into a fleet of pods serving 503, which looks healthy from every angle.

WHAT IT GUARANTEES otherwise, and both halves are load-bearing:

  * **A non-fatal failure to BUILD is logged and leaves the attribute UNSET.** The dependency then
    finds nothing on `app.state` and answers 503. Falling back to a permissive checker would turn a
    broken authorization layer into an OPEN one — a far worse failure, and a silent one.
  * **Nothing is built at import.** `OIDCVerifier` fetches discovery, so constructing one at module
    scope would put a network round-trip on the import path of every service and every test.

Both postures default to the cautious value (`ModelRole.READER`, `BootFailure.SERVE_UNAVAILABLE`): a caller that names
neither publishes no model, and a build of its that fails leaves the governed doors answering 503 rather than open.
"""

import logging
from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool


if TYPE_CHECKING:
    from fastapi import FastAPI
    from openfga_sdk import OpenFgaClient

    from lakehouse.governed.decider import AccessDecider
    from lakehouse.governed.settings import FgaSettings, GovernedAuthSettings, OidcProvider
    from lakehouse.process.supervision import Supervisor


log = logging.getLogger(__name__)


class ModelRole(StrEnum):
    """What a service does with the authorization model: publish the one its image carries, or resolve it."""

    #: Create the store when none exists and write the model this image carries: the catalog, and nothing else.
    PUBLISHER = "publisher"
    #: Find the store the estate uses and the model this image carries, creating neither.
    READER = "reader"


class BootFailure(StrEnum):
    """What a failure to build a verifier or the authorization client does to the service's boot."""

    #: The error ends the boot, so the pod crash-loops where an operator watches.
    CRASH = "crash"
    #: The failure is logged, the attribute is left unset, and the governed doors answer 503.
    SERVE_UNAVAILABLE = "serve_unavailable"


async def build_fga_client(
    settings: FgaSettings,
    *,
    service: str,
    model_role: ModelRole = ModelRole.READER,
    on_failure: BootFailure = BootFailure.SERVE_UNAVAILABLE,
) -> OpenFgaClient | None:
    """The FGA client for this service, or `None` when it is off or could not be built.

    It returns the client rather than assigning it, because one caller builds its client outside a FastAPI
    lifespan: the catalog's warehouse-grant backfill holds the client for its own run and closes it.

    `ModelRole.READER` takes the read-only half: `fga_provision.resolve` finds the store the estate already uses and the model
    this image carries, and can never create either. When either is still absent at its deadline it raises
    `fga_provision.OpenFgaModelAbsentError`: a `SERVE_UNAVAILABLE` caller logs `openfga_unpinned` with the cause, builds no
    client and its gate answers 503; a `CRASH` caller lets the error end its boot, because it reads a missing client as
    "authorization off".

    Disposal belongs to the caller's lifespan: `lakehouse.governed.fga_provision.dispose`.
    """
    if not settings.fga_enabled:
        return None
    from lakehouse.governed import fga_provision

    try:
        # THE MODEL IS NEVER A SETTING: every service checks against the model its own image carries, found in the store
        # by its body ([[LH-201]]), so a `model.fga` edit takes effect with the image that ships it. A store id may be
        # named (a per-cluster id, never a committed default); the model's id is read from that store.
        if model_role is ModelRole.PUBLISHER:
            store_id, model_id = await fga_provision.provision(settings.fga_api_url, token_file=settings.fga_token_file, store_id=settings.fga_store_id)  # noqa: TID251 — the estate's one publisher of the model
            # Structured, not a printf: `openfga_provisioned` is a documented INFO audit-tier event
            # (`lakehouse.process.log_handler`), and its fields are what the audit reads.
            log.info("openfga_provisioned", extra={"service": service, "store_id": store_id, "model_id": model_id})
        else:
            store_id, model_id = await fga_provision.resolve(settings.fga_api_url, token_file=settings.fga_token_file, store_id=settings.fga_store_id)
            log.info("openfga_resolved_by_name", extra={"service": service, "store_id": store_id, "model_id": model_id})
        client = fga_provision.make_client(
            settings.fga_api_url, store_id, model_id, token_file=settings.fga_token_file, timeout_seconds=settings.fga_timeout_seconds
        )
        log.info("%s: FGA client ready (%s)", service, settings.fga_api_url)
    except fga_provision.OpenFgaModelAbsentError as exc:
        # Fails CLOSED, and never by provisioning: the service that noticed must not be the one that decides
        # what everyone is allowed to do. A `CRASH` caller lets it end the boot rather than serve with no checker
        # behind its doors.
        if on_failure is BootFailure.CRASH:
            raise
        log.warning("openfga_unpinned", extra={"service": service, "reason": f"{exc}; governed routes report unavailable"})
        return None
    except Exception as exc:
        # A refused credential is raised whatever `on_failure` says: the pod must not report Ready with every
        # governed door failing closed ([[XC-077]], `fga_provision.OpenFgaCredentialRefusedError`).
        if fga_provision.credential_refused(exc) and not isinstance(exc, fga_provision.OpenFgaCredentialRefusedError):
            raise fga_provision.OpenFgaCredentialRefusedError(f"{service}: OpenFGA refused this pod's credential") from exc
        if on_failure is BootFailure.CRASH or fga_provision.credential_refused(exc):
            raise
        log.exception("openfga_client_failed", extra={"service": service})
        return None
    return client


async def attach_auth(
    app: FastAPI,
    settings: GovernedAuthSettings,
    *,
    service: str,
    loops: Supervisor,
    model_role: ModelRole = ModelRole.READER,
    on_failure: BootFailure = BootFailure.SERVE_UNAVAILABLE,
) -> None:
    """Put `app.state.oidc`, `app.state.sa_oidc`, `app.state.fga` and `app.state.decider` in place when configured, and None otherwise.

    `app.state.decider` answers every gate's authorization question: the catalog's decision door for a service
    configured with `fga_decider_url`, which then has no `app.state.fga`, or the store through `app.state.fga`.

    Each verifier's keys are fetched here and then again on a loop of ``loops`` (`OIDCVerifier.refresh_forever`), so a
    request reads keys already held and an identity-provider outage serves the last good ones.

    Call from a lifespan, before yielding. `service` names the caller in the log lines, which is the
    only thing that differed between the copies this replaces. See the module docstring for what
    `model_role` and `on_failure` select and which services need which.

    All three start at None, so the doors read this lifespan's settings and nothing an earlier lifespan of the same
    app object built: a producer door reads a lingering `sa_oidc` as "a verifier is configured" and refuses the
    acknowledged-open caller (measured 2026-10-12, CI's serial `ms-test` lane, where one suite's lifespan with a
    service-account issuer preceded another's without).
    """
    app.state.oidc = None
    app.state.sa_oidc = None
    app.state.fga = None
    app.state.decider = None
    providers = settings.oidc_person_providers
    if settings.oidc_enabled and providers:
        try:
            await _attach_person_verifier(app, settings, providers=providers, service=service, loops=loops)
        except Exception:
            if on_failure is BootFailure.CRASH:
                raise
            log.exception("%s: OIDC verifier failed to build — governed routes will 503", service)

    try:
        await _attach_service_account_verifier(app, settings, service=service, loops=loops)
    except Exception:
        if on_failure is BootFailure.CRASH:
            raise
        log.exception("%s: service-account verifier failed to build — service callers will 503", service)

    app.state.decider = build_catalog_decider(settings, service=service)
    if app.state.decider is not None:
        return
    client = await build_fga_client(settings, service=service, model_role=model_role, on_failure=on_failure)
    if client is not None:
        from lakehouse.governed.decider import OpenFgaDecider

        app.state.fga = client
        app.state.decider = OpenFgaDecider(client)


async def _attach_person_verifier(app: FastAPI, settings: GovernedAuthSettings, *, providers: Sequence[OidcProvider], service: str, loops: Supervisor) -> None:
    """Put ``app.state.oidc`` in place over the person issuers ``providers`` names when an audience is set, keys kept fresh."""
    audience = settings.oidc_audience
    if not audience:
        return
    from lakehouse.governed import idp_identity, oidc

    # A caller is the principal its issuer's naming rule reads from the verified token ([[XC-214]], ADR 0208), so
    # one verifier over every issuer is wrapped with each issuer's rule rather than handed to the doors on its own.
    app.state.oidc = idp_identity.IdpVerifier(
        oidc.OIDCVerifier(
            [provider.issuer for provider in providers],
            audience,
            settings.oidc_cache_ttl,
            leeway=settings.oidc_leeway,
            allow_insecure=settings.oidc_allow_insecure,
            # Split-horizon (reverse-proxied IdP): fetch discovery/JWKS in-cluster while tokens
            # keep the public issuer string. The omission of this argument at ONE of six doors
            # broke every signed-in ingest; it is centralized here so there is no sixth door.
            discovery_overrides={provider.issuer: url for provider in providers if (url := provider.discovery_url)} or None,
        ),
        providers,
    )
    # PROVED, not asserted. The constructor above performs no I/O, so until this call the
    # log line "verifier ready" said only that a Python object existed. `warm` resolves every
    # configured issuer, which puts a wrong issuer, a wrong split-horizon override or an
    # unreachable IdP in this pod's log AT BOOT with the URL that failed — instead of in a
    # user's failed request minutes later.
    #
    # OFF THE EVENT LOOP: `refresh` performs bounded but blocking HTTP, and this runs inside the
    # lifespan of an async app. Awaiting it inline would stall the worker.
    failures = await run_in_threadpool(app.state.oidc.refresh)
    loops.keep("oidc-keys", app.state.oidc.refresh_forever)
    if failures:
        # NOT FATAL, and never readiness. Gating startup or `/readyz` on a downstream IdP
        # turns one blip into every governed pod leaving its Service endpoints at once —
        # worse than the 503 the door answers on its own. The pod serves and says what is
        # wrong; an operator reads this line.
        for issuer, reason in failures:
            log.error("%s: OIDC issuer unusable (issuer=%s): %s", service, issuer, reason)
    else:
        log.info("%s: OIDC verifier ready (issuers=%s, discovery reached)", service, ", ".join(provider.issuer for provider in providers))


async def _attach_service_account_verifier(app: FastAPI, settings: GovernedAuthSettings, *, service: str, loops: Supervisor) -> None:
    """Put ``app.state.sa_oidc`` in place for the configured service-account issuer, if any, its keys kept fresh on ``loops``."""
    sa_issuer, sa_audience = settings.sa_issuer, settings.sa_audience
    if not (sa_issuer and sa_audience):
        return
    from lakehouse.governed import machine_identity

    app.state.sa_oidc = machine_identity.ServiceAccountVerifier(
        sa_issuer,
        sa_audience,
        cache_ttl=settings.oidc_cache_ttl,
        leeway=settings.oidc_leeway,
        fetch_token_file=settings.sa_fetch_token_file,
        ca_file=settings.sa_ca_file,
        max_lifetime=settings.sa_max_token_seconds,
    )
    # Proved at boot, off the loop, reported and never fatal: the Dex verifier's reasons above.
    failures = await run_in_threadpool(app.state.sa_oidc.refresh)
    loops.keep("service-account-keys", app.state.sa_oidc.refresh_forever)
    for issuer, reason in failures:
        log.error("%s: service-account issuer unusable (issuer=%s): %s", service, issuer, reason)
    if not failures:
        log.info("%s: service-account verifier ready (issuer=%s, discovery reached)", service, sa_issuer)


def build_catalog_decider(settings: FgaSettings, *, service: str) -> AccessDecider | None:
    """The catalog's decision door for a service that writes no tuple, or `None` for one of the store's clients ([[LH-442]]).

    A service given `fga_decider_url` builds no OpenFGA client at all, so it holds no credential that could write a
    tuple. Nothing is fetched here: the first gated request asks, and a refusal there is a 503 that names the cause in
    this pod's log (`decision_refused`).
    """
    if not (settings.fga_enabled and settings.fga_decider_url):
        return None
    from lakehouse.governed.decider import CatalogDecider
    from lakehouse.governed.machine_identity import read_identity_token

    token_file = settings.fga_decider_token_file
    if not token_file:
        raise ValueError(f"{service}: RASK_FGA_DECIDER_URL is set but RASK_FGA_DECIDER_TOKEN_FILE is not; the catalog would refuse every decision")

    def token() -> str:
        return read_identity_token(token_file)

    log.info("%s: authorization decided by the catalog (%s)", service, settings.fga_decider_url)
    return CatalogDecider(settings.fga_decider_url, token=token, timeout_seconds=settings.fga_timeout_seconds)


__all__ = [
    "BootFailure",
    "ModelRole",
    "attach_auth",
    "build_catalog_decider",
    "build_fga_client",
]
