"""How ingest identifies itself upstream — ONE builder, for both doors it calls.

There are two, and they are different SERVICES rather than two routes on one: the catalog's service
door (`catalog_service.py`) and the lineage graph's (`provenance.py`). Ingest claims the same subject
at both — `RASK_CATALOG_SERVICE_IDENTITY` and `RASK_LINEAGE_SERVICE_IDENTITY` are both
`service-ingest` in the chart — so a dedicated credential that reached only one of them would be a
control at one door and decoration at the other. `dapr_auth.service_principal` makes that concrete:
it refuses a privileged subject presenting the shared token AND an ordinary subject presenting a
dedicated one, so a subject privileged at one door and ordinary at the other cannot satisfy both —
whichever token it sends, one door refuses it.

THE LINEAGE DOOR IS THE ONE THAT FAILS QUIETLY. A refused emit is swallowed by design, because a
landed commit must not become a failed run — so a 401 there does not surface as an error, it
surfaces as a permanent gap in the graph that looks exactly like a healthy estate. That has already
happened twice on this lane (the trainer in 2026-07, `service-ingest` on 2026-08-06, a day of 403s
while the data landed).

WHY THE STORE READ IS GATED ON AN EXPLICIT FLAG rather than built unconditionally the way
maintenance's is. `catalog_token()` deliberately SKIPS the secret store when the identity and the
shared token are both set, and its comment records why: a fail-closed fetch written before the
catalog had an identity door turned a missing-and-unneeded `catalog-token` into a failed run at the
first activity. A resolver built unconditionally here would put that read back — a dev stack with an
identity, a token and no store would start reading one and fail closed on a configuration that works
today. `RASK_INGEST_SECRETS_FROM_DAPR` is the same shape `MAINTENANCE_SECRETS_FROM_DAPR` already
has, and off is the honest default for a service that must keep working with no store at all.
"""

from __future__ import annotations

from collections.abc import Callable

from ingest.config import IngestSettings


def dedicated_token_for(config: IngestSettings) -> Callable[[str], str | None] | None:
    """The resolver ingest uses to present its OWN credential, or ``None`` when it cannot.

    ``None`` when secrets do not come from Dapr, which leaves the shared-token path exactly as it
    was. An identity the store simply lacks resolves to ``None`` INSIDE the resolver and the caller
    falls back to the shared bearer; the door stays the single authority on whether that is
    acceptable. An UNREADABLE store raises instead — "we could not read it" and "this identity is not
    privileged" are different answers, and conflating them is how a credential control becomes
    decorative.
    """
    if not config.secrets_from_dapr:
        return None
    from service_kit.governed.dapr_auth import dedicated_token_from_store

    return dedicated_token_from_store(config.secret_store, config.secret_key)


def service_headers(config: IngestSettings, *, identity: str | None, shared_token: str | None) -> dict[str, str]:
    """Both halves of the service identity for one door, or nothing at all.

    HALF-CONFIGURED IS WORTH NOTHING and is worse than empty: a door needs the token AND the subject,
    and sending one is a request refused for a reason invisible from this side. So an unset identity
    or no credential at all yields ``{}`` and the caller takes its own fallback — a user's bearer at
    the catalog, silence at the graph — rather than a request that cannot succeed.

    The identity is a parameter because the two doors read it from two settings fields. They hold the
    same value in the chart today, and passing it explicitly is what keeps that a fact about the
    deployment rather than an assumption compiled in here.
    """
    if not identity:
        return {}
    resolver = dedicated_token_for(config)
    own = resolver(identity) if resolver is not None else None
    token = own or shared_token
    if not token:
        return {}
    return {"dapr-api-token": token, "x-lance-service-identity": identity}
