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


def require_dapr_token(dapr_api_token: Annotated[str | None, Header()] = None) -> None:
    """FastAPI dependency: reject a sidecar-delivered request whose ``dapr-api-token`` header doesn't
    match the app's ``APP_API_TOKEN`` (set by Dapr from ``dapr.io/app-token-secret``). No-op when the
    token is unset — the open dev default; ``assert_app_token_configured`` makes that a startup error
    once Dapr ingest is actually enabled, so the no-op can only apply in dev."""
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
