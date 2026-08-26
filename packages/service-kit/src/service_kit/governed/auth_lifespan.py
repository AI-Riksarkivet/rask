"""Build the OIDC verifier and the FGA client onto `app.state`, once, for any governed service.

WHY THIS EXISTS. Every service with a door carried its own ~30-line copy of this block —
`flows/lifespan.py`, and a near-identical one in the viewer, annotator and notifications. Adding a
door to `compute` and `controlplane` would have made it five and six. That is the shape the audit's
own DUP findings are about, and it has already cost this estate once: the ING-02 blocking-verify fix
landed on the ingest copy of a duplicated auth function and never reached the medallion copy.

WHAT IT GUARANTEES, and both halves are load-bearing:

  * **A failure to BUILD is logged and non-fatal.** The dependency then finds nothing on `app.state`
    and answers 503. Falling back to a permissive checker would turn a broken authorization layer
    into an OPEN one — a far worse failure, and a silent one.
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


log = logging.getLogger(__name__)


class _GovernedSettings(Protocol):
    """The subset of `GovernedAuthSettings` this needs. A Protocol, so any service's own settings
    class satisfies it structurally without importing a base."""

    oidc_enabled: bool
    oidc_issuer: str | None
    oidc_audience: str | None
    oidc_discovery_url: str | None
    oidc_cache_ttl: int
    oidc_leeway: int
    oidc_allow_insecure: bool
    fga_enabled: bool
    fga_api_url: str
    fga_store_id: str | None
    fga_model_id: str | None
    fga_timeout_seconds: float


async def attach_auth(app: FastAPI, settings: _GovernedSettings, *, service: str) -> None:
    """Put `app.state.oidc` and `app.state.fga` in place when configured.

    Call from a lifespan, before yielding. `service` names the caller in the log lines, which is the
    only thing that differed between the copies this replaces.
    """
    if settings.oidc_enabled and settings.oidc_issuer and settings.oidc_audience:
        try:
            from service_kit.governed.oidc import OIDCVerifier

            app.state.oidc = OIDCVerifier(
                settings.oidc_issuer,
                settings.oidc_audience,
                settings.oidc_cache_ttl,
                leeway=settings.oidc_leeway,
                allow_insecure=settings.oidc_allow_insecure,
                discovery_overrides=({settings.oidc_issuer: settings.oidc_discovery_url} if settings.oidc_discovery_url else None),
            )
            log.info("%s: OIDC verifier ready (issuer=%s)", service, settings.oidc_issuer)
        except Exception:
            log.exception("%s: OIDC verifier failed to build — governed routes will 503", service)

    if settings.fga_enabled:
        try:
            from service_kit.governed import fga

            store_id, model_id = settings.fga_store_id, settings.fga_model_id
            if not (store_id and model_id):
                store_id, model_id = await fga.provision(settings.fga_api_url)
                log.info("%s: openfga provisioned store=%s model=%s", service, store_id, model_id)
            app.state.fga = fga.make_client(settings.fga_api_url, store_id, model_id, timeout_seconds=settings.fga_timeout_seconds)
            log.info("%s: FGA client ready (%s)", service, settings.fga_api_url)
        except Exception:
            log.exception("%s: FGA client failed to build — governed routes will 503", service)
