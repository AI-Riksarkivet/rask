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
import pytest
import respx
from medallion.services.catalog_register import RegisterError, register_stage_output


CATALOG = "http://catalog.test"
ROOT = "s3://lance-catalog"


def _routes() -> respx.Route:
    # ONLY the register call. A `POST /v1/namespace/silver/create` route lived here too, answering 200 —
    # a call the cascade is ruled never to make (see TestItNeverTriesToCreateATopLevelNamespace below)
    # and which answers 400 in-cluster, not 200. Six tests carried it and none ever fired it; respx's
    # `assert_all_called` is off on the bare `@respx.mock` router, so nothing said so. Registering it
    # actively WEAKENED the file: had the regression returned, the mover would have received a cheerful
    # 200 from a mock promising something the real catalog refuses. With no route, `assert_all_mocked`
    # (on by default) raises AllMockedAssertionError the moment that call is made.
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
        # No route for the create. Asserting over the RECORDED calls states the same claim without
        # mocking a door the cascade must not knock on — and `assert_all_mocked` makes the attempt a
        # hard AllMockedAssertionError rather than a silent 400 this test then has to notice.
        route = respx.post(f"{CATALOG}/v1/table/silver$features/register").mock(return_value=httpx.Response(200, json={}))

        register_stage_output(catalog_url=CATALOG, catalog_root=ROOT, table_id="silver$features", to_uri=f"{ROOT}/medallion/silver")

        assert not [c for c in respx.calls if "/namespace/" in str(c.request.url)], "the mover tried to mint a top-level namespace the warehouse door owns"
        assert route.called

    @respx.mock
    def test_a_NESTED_parent_is_still_created(self) -> None:
        """The case the create exists for: a namespace inside a tenant's own, which the mover may make."""
        create = respx.post(f"{CATALOG}/v1/namespace/acme$silver/create").mock(return_value=httpx.Response(200, json={}))
        respx.post(f"{CATALOG}/v1/table/acme$silver$features/register").mock(return_value=httpx.Response(200, json={}))

        register_stage_output(catalog_url=CATALOG, catalog_root=ROOT, table_id="acme$silver$features", to_uri=f"{ROOT}/medallion/silver")

        assert create.called


class TestA409MustAgreeAboutWHERE:
    """A 409 means "already registered". It does not mean "registered where you are writing".

    Found live: `silver$features` was registered at `s3://bind86-wh/medallion/silver` — a leftover
    warehouse — while the mover writes `s3://lance-catalog/medallion/silver`. Register answered 409,
    the lane logged "already governed" and moved on, and the publish that followed opened the STALE
    location, found no dataset there and 500'd. The mover had written real rows to a place the
    catalog did not believe the table lived, and nothing in between noticed.

    A registration that names a different location is a CONFLICT, not a convergence. The lane must
    not silently adopt it: whoever fixes it needs both paths named, which is what the error carries.
    """

    @respx.mock
    def test_a_409_at_the_SAME_location_is_the_steady_state(self) -> None:
        respx.post(f"{CATALOG}/v1/table/silver$features/register").mock(return_value=httpx.Response(409, json={}))
        respx.post(f"{CATALOG}/v1/table/silver$features/describe").mock(return_value=httpx.Response(200, json={"location": f"{ROOT}/medallion/silver"}))

        _register()  # every redelivery after the first lands here — must not raise

    @respx.mock
    def test_a_409_at_a_DIFFERENT_location_refuses_and_names_both(self) -> None:
        respx.post(f"{CATALOG}/v1/table/silver$features/register").mock(return_value=httpx.Response(409, json={}))
        respx.post(f"{CATALOG}/v1/table/silver$features/describe").mock(return_value=httpx.Response(200, json={"location": "s3://bind86-wh/medallion/silver"}))

        with pytest.raises(RegisterError) as exc:
            _register()

        message = str(exc.value)
        assert "bind86-wh" in message, "the error must name where the catalog thinks the table is"
        assert "medallion/silver" in message, "and where this stage actually wrote"

    @respx.mock
    def test_an_UNREADABLE_describe_does_not_invent_agreement(self) -> None:
        """If the check itself cannot be made, that is not a pass. Treating an unanswerable describe as
        agreement would restore exactly the silence this closes."""
        respx.post(f"{CATALOG}/v1/table/silver$features/register").mock(return_value=httpx.Response(409, json={}))
        respx.post(f"{CATALOG}/v1/table/silver$features/describe").mock(return_value=httpx.Response(500, text="boom"))

        with pytest.raises(RegisterError):
            _register()
