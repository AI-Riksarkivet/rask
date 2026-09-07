"""How notifications identifies itself to the lineage graph — its ONE door, and its own credential.

F2-3's last subject. `notifications` was the third and final holder of the estate's shared
`APP_API_TOKEN`; it now presents `service-token-notifications` when the store has one.

IT HAS EXACTLY ONE DOOR, which is why this module is smaller than its siblings. Maintenance calls two
(compaction and vending) and ingest calls two on two different services (the catalog and the graph);
notifications reads lineage's durable feed and nothing else. One door still gets a builder rather than
an inline pair, because the reason the other two have one is not arithmetic: a credential control
applied at some sites and not others is not a control, and a SECOND door added later would otherwise
be written by copying the inline version.

WHAT MAKES THIS SUBJECT DIFFERENT FROM THE OTHER TWO. Its reader fails in the direction that hides:
`reconcile` walks `GET /events` to catch the events the bus alone provably misses, and a refused walk
returns no rows rather than an error — so a 401 here does not surface as a failure, it surfaces as an
inbox that is quietly incomplete. That is the same shape as ingest's lineage door and the reason both
were worth doing before anyone reported a symptom.

BOTH HEADERS OR NEITHER, unchanged. Lineage's door opens on the pair: a request carrying only
`dapr-api-token` falls through to OIDC by design (the sidecar stamps that token on everything it
delivers), and one carrying only the identity is an unauthenticated claim. So a deployment with no
credential at all still sends nothing, rather than half of a service door.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import SecretStr

from notifications.api.settings import IngressSettings


def dedicated_token_for(settings: IngressSettings) -> Callable[[str], str | None] | None:
    """The resolver notifications uses to present its OWN credential, or ``None`` when it cannot.

    ``None`` when secrets do not come from Dapr, which leaves the shared-token path exactly as it was.
    An identity the store simply lacks resolves to ``None`` INSIDE the resolver and the caller falls
    back; the door stays the single authority on whether that is acceptable. An UNREADABLE store raises
    instead — "we could not read it" and "this identity is not privileged" are different answers, and
    conflating them is how a credential control becomes decorative.
    """
    if not settings.secrets_from_dapr:
        return None
    from service_kit.governed.dapr_auth import dedicated_token_from_store

    return dedicated_token_from_store(settings.secret_store, settings.secret_key)


def feed_token(settings: IngressSettings) -> SecretStr | None:
    """The credential this service presents at lineage's door: its own, else the estate's shared one.

    `SecretStr` all the way through, so the value cannot reach a log line or a repr by accident — the
    reason the shared token was already read as a setting rather than off `os.environ` at the call
    site.
    """
    resolver = dedicated_token_for(settings)
    if resolver is not None and (own := resolver(settings.service_identity)):
        return SecretStr(own)
    return settings.app_api_token
