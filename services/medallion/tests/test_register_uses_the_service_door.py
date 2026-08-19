"""A mover authenticates to the catalog as a SERVICE, not by presenting a human's bearer.

`register_stage_output` sends `Authorization: Bearer {token}` from `MEDALLION_CATALOG_TOKEN`. That
setting is rendered by no chart template, and it should not be: the catalog verifies OIDC JWTs, a JWT
EXPIRES, and a static string in a secret store cannot be one. The ingest plane made this exact mistake
first and its own fix records the reasoning — "chasing it produced a fail-closed run on a
`catalog-token` secret that never needed to exist" (`ingest/catalog_service.py`).

The catalog runs an identity door instead (`catalog/api/security.py`): `dapr-api-token` — which daprd
already injects from a managed secret — plus `x-lance-service-identity`, the subject the caller
claims, checked against `LANCE_SERVICE_SUBJECTS`. The mover already has both halves in-cluster and
sends neither, so on a governed estate (`auth.enabled: true`, the shipped default) every register 401s,
raises `RegisterError`, and returns RETRY — an infinite redelivery.

BOTH HALVES OR NEITHER: the door requires the token AND the identity, and sending one is a request
refused for a reason invisible from this side.
"""

from __future__ import annotations

import httpx
import respx
from medallion.services.catalog_register import register_stage_output


CATALOG = "http://catalog.test"
ROOT = "s3://lance-catalog"


def _routes() -> respx.Route:
    respx.post(f"{CATALOG}/v1/namespace/silver/create").mock(return_value=httpx.Response(200, json={}))
    return respx.post(f"{CATALOG}/v1/table/silver$features/register").mock(return_value=httpx.Response(200, json={}))


def _register(*, token: str | None = None, app_token: str | None = None, service_identity: str | None = None) -> None:
    register_stage_output(
        catalog_url=CATALOG,
        catalog_root=ROOT,
        table_id="silver$features",
        to_uri=f"{ROOT}/medallion/silver",
        token=token,
        app_token=app_token,
        service_identity=service_identity,
    )


class TestTheServiceDoor:
    @respx.mock
    def test_the_identity_and_the_app_token_are_sent_together(self) -> None:
        route = _routes()

        _register(app_token="stamped-by-daprd", service_identity="service-bronze-to-silver")

        headers = route.calls.last.request.headers
        assert headers["dapr-api-token"] == "stamped-by-daprd"
        assert headers["x-lance-service-identity"] == "service-bronze-to-silver"

    @respx.mock
    def test_the_bearer_is_NOT_sent_when_the_service_door_is_available(self) -> None:
        """A service call has no human to forward. Sending both would present two principals and let
        the catalog pick, which is not a decision this side gets to delegate."""
        route = _routes()

        _register(app_token="stamped-by-daprd", service_identity="service-bronze-to-silver", token="a-jwt")

        assert "authorization" not in route.calls.last.request.headers

    @respx.mock
    def test_the_PARENT_namespace_create_carries_the_same_credential(self) -> None:
        """Registration can be two calls. Authenticating only the second leaves the first 401-ing, and
        the parent create runs FIRST — so the hop fails before the register is reached.

        Uses a NESTED id, because that is the only shape that still creates a parent: a top-level
        namespace belongs to the warehouse door, not to this lane."""
        ns = respx.post(f"{CATALOG}/v1/namespace/acme$silver/create").mock(return_value=httpx.Response(200, json={}))
        respx.post(f"{CATALOG}/v1/table/acme$silver$features/register").mock(return_value=httpx.Response(200, json={}))

        register_stage_output(
            catalog_url=CATALOG,
            catalog_root=ROOT,
            table_id="acme$silver$features",
            to_uri=f"{ROOT}/medallion/silver",
            app_token="stamped-by-daprd",
            service_identity="service-bronze-to-silver",
        )

        assert ns.calls.last.request.headers["x-lance-service-identity"] == "service-bronze-to-silver"


class TestBothHalvesOrNeither:
    @respx.mock
    def test_an_identity_with_no_token_does_not_open_the_door(self) -> None:
        """Half a credential is refused for a reason the caller cannot see. Don't send it."""
        route = _routes()

        _register(service_identity="service-bronze-to-silver")

        assert "x-lance-service-identity" not in route.calls.last.request.headers

    @respx.mock
    def test_a_token_with_no_identity_does_not_open_the_door(self) -> None:
        route = _routes()

        _register(app_token="stamped-by-daprd")

        assert "dapr-api-token" not in route.calls.last.request.headers


class TestTheBearerRemainsForTheCaseThatNeedsIt:
    @respx.mock
    def test_a_forwarded_human_bearer_still_rides(self) -> None:
        """Forwarding a user's token is a real case, and this is the only door for it. Kept, not
        preferred — the service path takes precedence when it is available."""
        route = _routes()

        _register(token="a-humans-jwt")

        assert route.calls.last.request.headers["authorization"] == "Bearer a-humans-jwt"

    @respx.mock
    def test_no_credential_at_all_sends_no_auth_headers(self) -> None:
        """The dev/local path: an ungoverned catalog needs nothing, and inventing a header would make
        an anonymous call look like a failed authenticated one."""
        route = _routes()

        _register()

        sent = route.calls.last.request.headers
        assert "authorization" not in sent
        assert "dapr-api-token" not in sent
        assert "x-lance-service-identity" not in sent


class TestItNeverTriesToCreateATopLevelNamespace:
    """Found in-cluster, on the first run register ever made: the cascade dead-lettered on a 400.

    `register_stage_output` created the parent namespace before registering, treating 409 as the
    steady state. For a NESTED parent that is right. For a TOP-LEVEL one — `silver`, `gold`, every
    namespace the cascade actually writes into — the catalog refuses outright:

        400 InvalidInputError: top-level namespace 'silver' must belong to a warehouse ...
        Create it through its warehouse — POST /v1/warehouses/{id}/namespaces

    The guard runs BEFORE the existence check, so an already-existing namespace answers 400 rather
    than 409 and the whole hop fails. A mover has no business minting a tenant's top-level namespace;
    that door is the warehouse's, and a missing one is an operator's problem stated by the register
    call's own error rather than papered over here.
    """

    @respx.mock
    def test_a_TOP_LEVEL_parent_is_not_created(self) -> None:
        create = respx.post(f"{CATALOG}/v1/namespace/silver/create").mock(return_value=httpx.Response(400, json={}))
        route = respx.post(f"{CATALOG}/v1/table/silver$features/register").mock(return_value=httpx.Response(200, json={}))

        register_stage_output(catalog_url=CATALOG, catalog_root=ROOT, table_id="silver$features", to_uri=f"{ROOT}/medallion/silver")

        assert not create.called, "the mover tried to mint a top-level namespace the warehouse door owns"
        assert route.called

    @respx.mock
    def test_a_NESTED_parent_is_still_created(self) -> None:
        """The case the create exists for: a namespace inside a tenant's own, which the mover may make."""
        create = respx.post(f"{CATALOG}/v1/namespace/acme$silver/create").mock(return_value=httpx.Response(200, json={}))
        respx.post(f"{CATALOG}/v1/table/acme$silver$features/register").mock(return_value=httpx.Response(200, json={}))

        register_stage_output(catalog_url=CATALOG, catalog_root=ROOT, table_id="acme$silver$features", to_uri=f"{ROOT}/medallion/silver")

        assert create.called
