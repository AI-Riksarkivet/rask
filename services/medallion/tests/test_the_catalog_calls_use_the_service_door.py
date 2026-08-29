"""A mover authenticates to the catalog as a SERVICE, not by presenting a human's bearer.

`MEDALLION_CATALOG_TOKEN` sends `Authorization: Bearer {token}`. That setting is rendered by no chart
template, and it should not be: the catalog verifies OIDC JWTs, a JWT EXPIRES, and a static string in
a secret store cannot be one. The ingest plane made this exact mistake first and its own fix records
the reasoning — "chasing it produced a fail-closed run on a `catalog-token` secret that never needed
to exist" (`ingest/catalog_service.py`).

The catalog runs an identity door instead (`catalog/api/security.py`): `dapr-api-token` — which daprd
already injects from a managed secret — plus `x-lance-service-identity`, the subject the caller
claims, checked against `LANCE_SERVICE_SUBJECTS`. The mover has both halves in-cluster, and on a
governed estate (`auth.enabled: true`, the shipped default) a call that sends neither 401s, raises
`RegisterError` and returns RETRY — an infinite redelivery.

BOTH HALVES OR NEITHER: the door requires the token AND the identity, and sending one is a request
refused for a reason invisible from this side.

THE SEAM UNDER TEST IS `publish_stage_output`, and this file used to drive `register_stage_output`.
That door was deleted with its last caller: `_credential` is shared by every call this module makes,
so the rule is pinned on a seam the cascade actually reaches rather than on one only tests opened.
Two properties the old file pinned went with the door and are now STRUCTURAL, which is why no
replacement assertion appears below: a mover cannot mint a top-level namespace (no namespace call
remains in the module at all — the one test that could still say so is kept), and a registration
cannot disagree about WHERE the stage wrote (the stage writes where `ensure_stage_output` vended,
so there is no second claim to disagree with).
"""

from __future__ import annotations

import httpx
import pyarrow as pa
import pytest
import respx
from medallion.services.catalog_register import RegisterError, ensure_stage_output, publish_stage_output


CATALOG = "http://catalog.test"


def _routes() -> respx.Route:
    """The publish door, which is ONE call — so the assertions read the credential, not a sequence."""
    return respx.post(f"{CATALOG}/v1/table/silver$features/publish").mock(return_value=httpx.Response(200, json={"published": True}))


def _publish(*, token: str | None = None, app_token: str | None = None, service_identity: str | None = None) -> None:
    publish_stage_output(
        catalog_url=CATALOG,
        table_id="silver$features",
        version=2,
        key_column="id",
        token=token,
        app_token=app_token,
        service_identity=service_identity,
    )


class TestTheServiceDoor:
    @respx.mock
    def test_the_identity_and_the_app_token_are_sent_together(self) -> None:
        route = _routes()

        _publish(app_token="stamped-by-daprd", service_identity="service-bronze-to-silver")

        headers = route.calls.last.request.headers
        assert headers["dapr-api-token"] == "stamped-by-daprd"
        assert headers["x-lance-service-identity"] == "service-bronze-to-silver"

    @respx.mock
    def test_the_bearer_is_NOT_sent_when_the_service_door_is_available(self) -> None:
        """A service call has no human to forward. Sending both would present two principals and let
        the catalog pick, which is not a decision this side gets to delegate."""
        route = _routes()

        _publish(app_token="stamped-by-daprd", service_identity="service-bronze-to-silver", token="a-jwt")

        assert "authorization" not in route.calls.last.request.headers

    @respx.mock
    def test_the_CREATE_carries_the_same_credential_as_the_describe(self) -> None:
        """Resolving a location can be two calls. Authenticating only the second leaves the first
        401-ing — and the describe runs FIRST, so the hop fails before the create is reached."""
        respx.post(f"{CATALOG}/v1/table/silver$features/describe").mock(return_value=httpx.Response(404, json={}))
        create = respx.post(f"{CATALOG}/v1/table/silver$features/create").mock(
            return_value=httpx.Response(200, json={"location": "s3://acme-wh/abc_silver$features"})
        )

        ensure_stage_output(
            catalog_url=CATALOG,
            table_id="silver$features",
            schema=pa.schema([pa.field("id", pa.int64())]),
            app_token="stamped-by-daprd",
            service_identity="service-bronze-to-silver",
        )

        assert create.calls.last.request.headers["x-lance-service-identity"] == "service-bronze-to-silver"


class TestBothHalvesOrNeither:
    @respx.mock
    def test_an_identity_with_no_token_does_not_open_the_door(self) -> None:
        """Half a credential is refused for a reason the caller cannot see. Don't send it."""
        route = _routes()

        _publish(service_identity="service-bronze-to-silver")

        assert "x-lance-service-identity" not in route.calls.last.request.headers

    @respx.mock
    def test_a_token_with_no_identity_does_not_open_the_door(self) -> None:
        route = _routes()

        _publish(app_token="stamped-by-daprd")

        assert "dapr-api-token" not in route.calls.last.request.headers


class TestTheBearerRemainsForTheCaseThatNeedsIt:
    @respx.mock
    def test_a_forwarded_human_bearer_still_rides(self) -> None:
        """Forwarding a user's token is a real case, and this is the only door for it. Kept, not
        preferred — the service path takes precedence when it is available."""
        route = _routes()

        _publish(token="a-humans-jwt")

        assert route.calls.last.request.headers["authorization"] == "Bearer a-humans-jwt"

    @respx.mock
    def test_no_credential_at_all_sends_no_auth_headers(self) -> None:
        """The dev/local path: an ungoverned catalog needs nothing, and inventing a header would make
        an anonymous call look like a failed authenticated one."""
        route = _routes()

        _publish()

        sent = route.calls.last.request.headers
        assert "authorization" not in sent
        assert "dapr-api-token" not in sent
        assert "x-lance-service-identity" not in sent


class TestItNeverTriesToCreateATopLevelNamespace:
    """Found in-cluster, on the first run register ever made: the cascade dead-lettered on a 400.

    The register door created the parent namespace before registering, treating 409 as the steady
    state. For a NESTED parent that is right. For a TOP-LEVEL one — `silver`, `gold`, every namespace
    the cascade actually writes into — the catalog refuses outright:

        400 InvalidInputError: top-level namespace 'silver' must belong to a warehouse ...
        Create it through its warehouse — POST /v1/warehouses/{id}/namespaces

    The guard runs BEFORE the existence check, so an already-existing namespace answers 400 rather
    than 409 and the whole hop failed. A mover has no business minting a tenant's top-level
    namespace; that door is the warehouse's.

    The mover reaches the catalog only through `ensure_stage_output` now, which knocks on no namespace
    door at all — so this asserts a property of the seam that replaced the offender rather than
    re-testing the offender.
    """

    @respx.mock
    def test_resolving_a_location_knocks_on_no_namespace_door(self) -> None:
        # No route for a namespace call. `assert_all_mocked` (on by default) turns an attempt into a
        # hard AllMockedAssertionError rather than a silent 400 this test would then have to notice.
        respx.post(f"{CATALOG}/v1/table/silver$features/describe").mock(return_value=httpx.Response(404, json={}))
        respx.post(f"{CATALOG}/v1/table/silver$features/create").mock(return_value=httpx.Response(200, json={"location": "s3://acme-wh/abc_silver$features"}))

        ensure_stage_output(catalog_url=CATALOG, table_id="silver$features", schema=pa.schema([pa.field("id", pa.int64())]))

        assert not [c for c in respx.calls if "/namespace/" in str(c.request.url)], "the mover tried to mint a namespace the warehouse door owns"


class TestAPrivilegedIdentityPresentsItsOwnCredential:
    """The CLIENT half of the dedicated-credential binding.

    `service_kit.governed.dapr_auth` already binds a privileged subject to its own
    `service-token-<identity>` on the SERVER side. Rendering that alone is not enabling the control,
    it is refusing every privileged caller — measured on the live estate 2026-08-26, where the catalog
    began demanding the dedicated token while the movers went on presenting the shared
    `APP_API_TOKEN`, and every call answered `401 Unauthorized` until it was reverted.

    So the mover must PRESENT what the door will ask for. Until it does, the estate is stuck with the
    shared token, and any holder of it can authenticate as any allowlisted identity — including ones
    that hold `owner` on every warehouse.

    The resolver is injected rather than read from the store here, for the same reason the server side
    takes a `dedicated_token=` callback: the secret store is a Dapr sidecar call, and a unit test that
    needed one would be testing the sidecar.
    """

    @respx.mock
    def test_a_dedicated_token_is_sent_instead_of_the_shared_one(self) -> None:
        route = _routes()

        publish_stage_output(
            catalog_url=CATALOG,
            table_id="silver$features",
            version=2,
            key_column="id",
            app_token="shared-app-token",
            service_identity="service-bronze-to-silver",
            dedicated_token=lambda identity: f"dedicated-for-{identity}",
        )

        headers = route.calls.last.request.headers
        assert headers["dapr-api-token"] == "dedicated-for-service-bronze-to-silver", "the mover still presented the SHARED token"
        assert headers["x-lance-service-identity"] == "service-bronze-to-silver"

    @respx.mock
    def test_an_identity_with_no_dedicated_token_falls_back_to_the_shared_one(self) -> None:
        """`None` means the bundle was READ and this identity simply is not privileged.

        Falling back rather than refusing keeps ONE authority over the decision: the door already
        hard-refuses a privileged subject that presents the wrong credential, so a client-side refusal
        would only produce the same outcome from a place with less information.
        """
        route = _routes()

        publish_stage_output(
            catalog_url=CATALOG,
            table_id="silver$features",
            version=2,
            key_column="id",
            app_token="shared-app-token",
            service_identity="service-web",
            dedicated_token=lambda _identity: None,
        )

        assert route.calls.last.request.headers["dapr-api-token"] == "shared-app-token"

    @respx.mock
    def test_no_resolver_at_all_is_the_shared_token(self) -> None:
        """The default, and what every existing caller gets — this change adds a path, it moves none."""
        route = _routes()

        _publish(app_token="shared-app-token", service_identity="service-bronze-to-silver")

        assert route.calls.last.request.headers["dapr-api-token"] == "shared-app-token"


class TestTheFailurePostureIsUnchangedByTheDoorThatWent:
    @respx.mock
    def test_a_refusal_names_the_status_and_the_table(self) -> None:
        """`RegisterError` still carries both, which is what an operator reads out of a dead-letter."""
        respx.post(f"{CATALOG}/v1/table/silver$features/publish").mock(return_value=httpx.Response(403, text="denied"))

        with pytest.raises(RegisterError, match="HTTP 403"):
            _publish()
