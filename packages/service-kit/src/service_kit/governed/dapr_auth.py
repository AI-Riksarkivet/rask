"""Authenticate Dapr-delivered routes (pub/sub subscriptions + input bindings).

Dapr delivers events to the SAME FastAPI app that serves the public HTTP API (``/lineage-events``,
``/medallion-event``, the compaction cron route), so without a check any client that can reach the port
can POST a forged CloudEvent — bypassing ``enforce_author`` and poisoning the authoritative lineage
graph (security audit, prod-blocker). When the pod is annotated ``dapr.io/app-token-secret``, Dapr
injects ``APP_API_TOKEN`` into the app **and** adds a ``dapr-api-token`` header to every request it
delivers. This dependency rejects any delivery whose header doesn't match.

Defense-in-depth (the token is one layer): the ``pubsub.jetstream`` component is **scoped** to the
trusted app-ids (only they can publish to the topic), the gateway **blocks** these routes from external
traffic, and the route is only registered when Dapr is enabled. No ``APP_API_TOKEN`` set = the open dev
default (documented); set it in any deployment that must be trusted.
"""

from __future__ import annotations

import os
import secrets
from typing import Annotated

from fastapi import Header, HTTPException


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


def public_callers() -> frozenset[str]:
    """The configured public front-door app-ids, lower-cased — ONE list for the whole estate.

    A per-service setting would let one deployment forget an edge the others know about, and the set
    is a property of the topology rather than of any single service.
    """
    raw = os.environ.get("RASK_PUBLIC_CALLERS", _DEFAULT_PUBLIC_CALLERS)
    return frozenset(caller.strip().lower() for caller in raw.split(",") if caller.strip())


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

    The token check is a no-op when ``APP_API_TOKEN`` is unset — the open dev default;
    ``assert_app_token_configured`` makes that a startup error once Dapr ingest is actually enabled,
    so the no-op can only apply in dev.

    The public-caller refusal is deliberately NOT conditional on the token. These routes are
    sidecar-delivery-only by construction, so a front-door invocation of one is never legitimate in
    any environment — and in dev, where the token is unset, it is the only guard there is.

    Threading the caller costs nothing at the call sites: this is consumed exclusively as
    ``Depends(...)``, so FastAPI resolves the new header itself and every door it guards is fixed
    without touching a single endpoint signature.
    """
    if is_public_caller(dapr_caller_app_id):
        raise HTTPException(
            status_code=403,
            detail=f"{dapr_caller_app_id!r} is a public front door: its Dapr app-token authenticates the proxy, not the caller",
        )
    expected = os.environ.get("APP_API_TOKEN")
    # compare_digest: the token is the only guard on these routes, so no timing side-channel; bytes
    # (not str) so a non-ASCII header value is a clean 403, never a TypeError.
    if expected and not secrets.compare_digest((dapr_api_token or "").encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="invalid or missing Dapr app-api-token")


def assert_app_token_configured(*, dapr_enabled: bool) -> None:
    """Fail closed at startup: when Dapr ingest is enabled the delivery route is live and MUST be
    authenticated, so an unset/blank ``APP_API_TOKEN`` is a misconfiguration — not the dev default — and
    the pod must refuse to start rather than silently expose an unauthenticated ingest path (the security
    audit's 'blanked token silently reopens the route' residual). No-op when Dapr ingest is off."""
    if dapr_enabled and not os.environ.get("APP_API_TOKEN"):
        raise RuntimeError(
            "APP_API_TOKEN must be set when Dapr ingest is enabled — the delivery route would otherwise be "
            "unauthenticated. Wire dapr.io/app-token-secret + the APP_API_TOKEN env, or disable Dapr ingest."
        )
