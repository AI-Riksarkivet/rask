"""Build the OIDC verifier and the FGA client onto `app.state`, once, for any governed service.

WHY THIS EXISTS. Every service with a door carried its own ~30-line copy of this block — twelve doors,
two mechanisms — and a duplicated bootstrap is one a fix cannot travel through. The estate has paid for
that twice in the same shape: the ING-02 blocking-verify fix landed on the ingest copy of a duplicated
auth function and never reached the medallion copy, and split-horizon discovery was written into five
copies and omitted from the sixth (`tests/unit/test_oidc_discovery_parity.py`), where every
service-to-service test passed anyway because the service-token path returns before the verifier is
touched.

THE POSTURES ARE PARAMETERS, and that is what makes the collapse honest rather than lossy. The copies
were not stylistic variants; they encoded three decisions, and a single-posture helper is why ten of
them survived the first extraction:

  * **`provision`** — whether an unpinned store may be CREATED. `ingest` and `maintenance` pass
    `False`: a data writer that mints a store and writes an authorization model becomes the source of
    truth for everyone else's permissions. They resolve read-only and fail closed against an estate
    that has not been bootstrapped. Reading which store exists is not authoring one, which is the
    distinction `fga.resolve` exists for.
  * **`fatal`** — whether a failed build takes the pod down. `catalog`, `lineage` and both medallion
    apps wrap neither construction in a `try` and therefore crash on boot; that is deliberate and it is
    the only signal an operator watches. Defaulting them into the swallowing posture would convert a
    CrashLoopBackOff into a fleet of pods serving 503, which looks healthy from every angle.

WHAT IT GUARANTEES otherwise, and both halves are load-bearing:

  * **A non-fatal failure to BUILD is logged and leaves the attribute UNSET.** The dependency then
    finds nothing on `app.state` and answers 503. Falling back to a permissive checker would turn a
    broken authorization layer into an OPEN one — a far worse failure, and a silent one.
  * **Nothing is built at import.** `OIDCVerifier` fetches discovery, so constructing one at module
    scope would put a network round-trip on the import path of every service and every test.

Both flags default OFF, so a service that calls this with an unconfigured settings object behaves
exactly as it did before it had a door.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol


if TYPE_CHECKING:
    from fastapi import FastAPI
    from openfga_sdk import OpenFgaClient


log = logging.getLogger(__name__)


class _FgaSettings(Protocol):
    """The FGA half alone — `maintenance` reaches the client without an OIDC door in front of it.

    READ-ONLY members (properties, not annotations) because a mutable protocol member is INVARIANT:
    the estate's settings classes disagree on optionality — `GovernedAuthSettings` declares
    `fga_store_id: str | None`, medallion declares `fga_store_id: str` defaulting to `""` — and an
    invariant `str | None` member rejects the second outright. Nothing here writes to settings, so
    accepting both is correct rather than lax.
    """

    @property
    def fga_enabled(self) -> bool: ...
    @property
    def fga_api_url(self) -> str: ...
    @property
    def fga_store_id(self) -> str | None: ...
    @property
    def fga_model_id(self) -> str | None: ...
    @property
    def fga_timeout_seconds(self) -> float: ...


class _GovernedSettings(_FgaSettings, Protocol):
    """The subset of `GovernedAuthSettings` this needs. A Protocol, so any service's own settings
    class satisfies it structurally without importing a base — which is what lets medallion's
    `MedallionSettings`, ingest's `IngestAuthSettings` and the catalog's pre-mixin twin all pass."""

    @property
    def oidc_enabled(self) -> bool: ...
    @property
    def oidc_issuer(self) -> str | None: ...
    @property
    def oidc_audience(self) -> str | None: ...
    @property
    def oidc_discovery_url(self) -> str | None: ...
    @property
    def oidc_cache_ttl(self) -> int: ...
    @property
    def oidc_leeway(self) -> int: ...
    @property
    def oidc_allow_insecure(self) -> bool: ...


async def build_fga_client(
    settings: _FgaSettings,
    *,
    service: str,
    provision: bool = True,
    fatal: bool = False,
) -> OpenFgaClient | None:
    """The FGA client for this service, or `None` when it is off or could not be built.

    RETURNS rather than assigns, because one consumer needs the value and not the assignment:
    `maintenance` builds its client outside a FastAPI lifespan (its sweep runs from a cron route and it
    stores the result as `app.state.fga_client`). Forcing it through an `app`-shaped signature is how a
    twelfth copy would have been justified.

    `provision=False` takes the read-only half — `fga.resolve` finds the store the estate already uses
    and can never create one. `None` back from it means the estate is not bootstrapped, so no client is
    built and the caller's gate keeps answering 503, which is the honest answer.

    Disposal belongs to the caller's lifespan: `service_kit.governed.fga.dispose`.
    """
    if not settings.fga_enabled:
        return None
    try:
        from service_kit.governed import fga

        store_id, model_id = settings.fga_store_id, settings.fga_model_id
        if not (store_id and model_id):
            if provision:
                store_id, model_id = await fga.provision(settings.fga_api_url)
                # STRUCTURED, not a printf: `openfga_provisioned` is a documented INFO audit-tier
                # event (`service_kit.obs`, severity 9), and it had two hand-written emitters
                # (catalog + lineage) before this became the single bootstrap. One structured
                # emitter here supersedes both — collapsing them into a message string would have
                # dropped the tier obs.py raises to OTLP.
                log.info("openfga_provisioned", extra={"service": service, "store_id": store_id, "model_id": model_id})
            else:
                resolved = await fga.resolve(settings.fga_api_url)
                if resolved is None:
                    # Fails CLOSED, and never by provisioning: an absent store means nobody has
                    # bootstrapped this estate, and the service that noticed must not be the one that
                    # decides what everyone is allowed to do. Structured, carrying the reason —
                    # maintenance's `reconcile_fga_unpinned` diagnostic, now shared.
                    log.warning(
                        "openfga_unpinned",
                        extra={
                            "service": service,
                            "reason": "FGA enabled but no provisioned store to resolve — governed routes/authz categories report unavailable",
                        },
                    )
                    return None
                store_id, model_id = resolved
                log.info("openfga_resolved_by_name", extra={"service": service, "store_id": store_id, "hint": "pin the store/model ids for production"})
        client = fga.make_client(settings.fga_api_url, store_id, model_id, timeout_seconds=settings.fga_timeout_seconds)
        log.info("%s: FGA client ready (%s)", service, settings.fga_api_url)
    except Exception:
        if fatal:
            raise
        # Structured, carrying the service — maintenance's `reconcile_fga_client_failed`, now shared.
        log.exception("openfga_client_failed", extra={"service": service})
        return None
    return client


async def attach_auth(
    app: FastAPI,
    settings: _GovernedSettings,
    *,
    service: str,
    provision: bool = True,
    fatal: bool = False,
) -> None:
    """Put `app.state.oidc` and `app.state.fga` in place when configured.

    Call from a lifespan, before yielding. `service` names the caller in the log lines, which is the
    only thing that differed between the copies this replaces. See the module docstring for what
    `provision` and `fatal` select and which services need which.

    An attribute is only ASSIGNED when its half actually built, so a caller that pre-sets
    `app.state.fga = None` (the services whose dependencies read the attribute directly rather than
    through `getattr`) keeps its explicit None.
    """
    if settings.oidc_enabled and settings.oidc_issuer and settings.oidc_audience:
        try:
            from service_kit.governed import oidc

            app.state.oidc = oidc.OIDCVerifier(
                settings.oidc_issuer,
                settings.oidc_audience,
                settings.oidc_cache_ttl,
                leeway=settings.oidc_leeway,
                allow_insecure=settings.oidc_allow_insecure,
                # Split-horizon (reverse-proxied IdP): fetch discovery/JWKS in-cluster while tokens
                # keep the public issuer string. The omission of this argument at ONE of six doors
                # broke every signed-in ingest; it is centralized here so there is no sixth door.
                discovery_overrides=({settings.oidc_issuer: settings.oidc_discovery_url} if settings.oidc_discovery_url else None),
            )
            log.info("%s: OIDC verifier ready (issuer=%s)", service, settings.oidc_issuer)
        except Exception:
            if fatal:
                raise
            log.exception("%s: OIDC verifier failed to build — governed routes will 503", service)

    client = await build_fga_client(settings, service=service, provision=provision, fatal=fatal)
    if client is not None:
        app.state.fga = client
