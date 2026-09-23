"""How maintenance identifies itself to the catalog — ONE builder, for every door it calls.

There are two: the compaction plan/commit pair and credential vending. They stamped the same two
headers from two copies, and a credential control applied to one of them is not a control. Vending is
the dangerous copy to miss, because `credentials.py` reports any `>=400` as "vending unavailable" and
returns `None` — so a 401 there does not fail, it silently falls back to the configured S3 key with
an `info` log. A refusal that reads as an outage is at least visible; one that reads as absence is
not.

THE THIRD HALF. A privileged subject needs its token SEEDED as well as demanded and presented:
`openbao.yaml` derives what to mint from its own list and `services.yaml` derives what to demand from
another, and an identity on the second but not the first resolves to `None` here — correct, meaning
"not provisioned" — falls back to the shared bearer, and is refused for being privileged. All three
are asserted by `tests/unit/test_a_privileged_subject_can_present_its_own_credential.py`.
"""

from __future__ import annotations

from collections.abc import Callable

from maintenance.core.config import MaintenanceSettings
from service_kit.governed.dapr_auth import expected_app_token


def dedicated_token_for(settings: MaintenanceSettings) -> Callable[[str], str | None] | None:
    """The resolver maintenance uses to present its OWN credential, or ``None`` when it cannot.

    ``None`` when secrets do not come from Dapr, so a dev stack with no store keeps the shared-token
    path unchanged. An identity the store simply lacks resolves to ``None`` INSIDE the resolver and
    the caller falls back; the door stays the single authority on whether that is acceptable. An
    UNREADABLE store raises instead — "we could not read it" and "this identity is not privileged"
    are different answers, and conflating them is how a credential control becomes decorative.
    """
    if not settings.secrets_from_dapr:
        return None
    from service_kit.governed.dapr_auth import dedicated_token_from_store

    return dedicated_token_from_store(settings.dapr_secret_store)


def service_headers(settings: MaintenanceSettings) -> dict[str, str]:
    """Both halves of the service identity, or the door refuses with a reason invisible from here.

    Its OWN credential when the store has one; the shared bearer otherwise, which is every estate
    that has not provisioned this identity.

    THE FALLBACK RESOLVES THROUGH `expected_app_token`, not through `DaprDoorSettings().app_api_token`,
    and the difference is a whole branch. That attribute is the ENV half alone; the resolver returns
    the STORE's value when `app_token_from_store` is set, which is the case this estate runs — measured
    on the deployed pod 2026-09-19, the attribute is `None` while the resolver answers a token. Its own
    docstring is the rule: "a service door still reading env while the Dapr door reads the store is a
    pod where half the credentials are configured and nothing says which half."

    The combination that was broken is the one the line above calls common — token in the store,
    dedicated identity absent — where this sent NO bearer and the catalog refused a call whose cause is
    a service away. `medallion/core/config.py::outbound_app_token` is the same fix one service over.
    """
    headers = {"x-lance-service-identity": settings.catalog_service_identity}
    resolver = dedicated_token_for(settings)
    if resolver is not None and (own := resolver(settings.catalog_service_identity)):
        headers["dapr-api-token"] = own
    elif token := expected_app_token():
        headers["dapr-api-token"] = token
    return headers
