"""At the catalog a service is the service account its projected token names, whatever else the request claims.

[[LH-220]], clauses "pod A's projected token cannot authenticate as B" and "nothing reads x-lance-service-identity".
Before the row the catalog took a service's identity from the `x-lance-service-identity` header and accepted the
estate-wide app token as proof, so the name was the caller's choice. Under D1 the projected token is verified offline
against the cluster issuer and its full username, `system:serviceaccount:<ns>:<sa>`, is mapped to a subject; the map
is exact, so the same account name in another namespace, an account it does not list, and a token minted for another
door are all refused, and none of them reaches authorization.

The catalog app as deployed, on a real `dir` namespace, with authentication on: its lifespan builds the verifiers from
these settings. Authorization is answered by a stand-in for OpenFGA's check that grants the namespace to
`service-maintenance` alone, and records who it was asked about, so a request acting as maintenance would pass where
one acting as ingest is refused.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from lance_namespace import connect
from lance_namespace_urllib3_client.models import CreateNamespaceRequest


_SHARED = "the-estate-wide-app-token"
_INGEST, _MAINTENANCE = "rask-sa-ingest", "rask-sa-maintenance"


@pytest.fixture
def catalog(sa_issuer: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, list[str]]]:
    env = {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": "https://idp.invalid",
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_SA_ISSUER": sa_issuer.issuer,
        "RASK_SA_AUDIENCE": "rask-catalog",
        "RASK_SA_SUBJECTS": json.dumps(
            {f"system:serviceaccount:default:{_INGEST}": "service-ingest", f"system:serviceaccount:default:{_MAINTENANCE}": "service-maintenance"}
        ),
        "RASK_SA_FETCH_TOKEN_FILE": str(sa_issuer.fetch_token_file),
        "RASK_SA_CA_FILE": str(sa_issuer.ca_file),
        # The deployment as it was: the shared token configured and both services on the service allowlist.
        "APP_API_TOKEN": _SHARED,
        "LANCE_SERVICE_SUBJECTS": "service-ingest,service-maintenance",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    connect("dir", {"root": str(tmp_path)}).create_namespace(CreateNamespaceRequest(id=["db"]))

    from catalog.core.config import Settings, get_settings

    get_settings.cache_clear()
    from catalog.main import app

    asked: list[str] = []

    async def check(_client: object, *, user: str, relation: str, obj: str, **_kwargs: object) -> bool:
        del relation, obj
        asked.append(user)
        return user == "service-maintenance"

    monkeypatch.setattr("service_kit.governed.fga.check", check)
    with TestClient(app, raise_server_exceptions=False) as client:
        # Authorization on for the requests, after a lifespan that built the verifiers and no OpenFGA client.
        governed = Settings.model_validate(
            {**env, "RASK_SA_SUBJECTS": json.loads(env["RASK_SA_SUBJECTS"]), "RASK_FGA_ENABLED": True, "RASK_FGA_API_URL": "http://openfga.invalid:8080"}
        )
        app.dependency_overrides[get_settings] = lambda: governed
        app.state.fga = MagicMock(close=AsyncMock())
        yield client, asked
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("account", "namespace", "audience", "status", "asked_about"),
    [
        pytest.param(_INGEST, "default", "rask-catalog", 403, "service-ingest", id="ingests-token-naming-maintenance-acts-as-ingest"),
        pytest.param(_INGEST, "other", "rask-catalog", 401, None, id="the-same-account-name-in-another-namespace"),
        pytest.param("rask-sa-unlisted", "default", "rask-catalog", 401, None, id="an-account-the-map-does-not-list"),
        pytest.param(_INGEST, "default", "rask-lineage", 401, None, id="a-token-minted-for-another-door"),
    ],
)
def test_the_catalog_takes_a_service_from_its_projected_token_alone(  # noqa: PLR0913 - parametrized
    catalog: tuple[TestClient, list[str]], sa_issuer: Any, account: str, namespace: str, audience: str, status: int, asked_about: str | None
) -> None:
    client, asked = catalog
    token = sa_issuer.mint(account, audience=audience, namespace=namespace)

    answered = client.post(
        "/v1/namespace/db/describe",
        json={},
        # Everything the old door read to name maintenance rides along; only the bearer may decide who this is.
        headers={"Authorization": f"Bearer {token}", "dapr-api-token": _SHARED, "x-lance-service-identity": "service-maintenance"},
    )

    assert answered.status_code == status, answered.text
    assert set(asked) == ({asked_about} if asked_about else set()), f"authorization was asked about {sorted(set(asked))}"
