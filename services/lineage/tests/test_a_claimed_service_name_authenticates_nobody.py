"""At lineage's door a service is the service account its projected token names, and a name it claims proves nothing.

[[LH-220]], clause "the shared token plus a claimed name answers 401". Measured live before the row: notifications was
allowlisted and not privileged, so the estate-wide `dapr-api-token` plus `x-lance-service-identity: notifications`
authenticated as notifications, and any holder of that one token could speak as any allowlisted subject. Under D1 a
service authenticates with its pod's projected token, verified offline against the cluster issuer and mapped by its
full username; the shared token stays only as the proof a sidecar delivered a request, and names nobody.

Driven at lineage's real door: its `authenticate`, the verifiers `attach_auth` builds from lineage's settings at boot,
and the handlers `build_lance_service_app` installs. The service-account issuer is the loopback one in the root
conftest, which serves its key set only to a bearer from a private CA, as k3s does.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lineage.api import security
from lineage.core.config import get_settings
from service_kit.exceptions import register_handlers
from service_kit.governed.auth_lifespan import attach_auth
from service_kit.lakehouse.ns_errors import install_problem_handlers


_SHARED = "the-estate-wide-app-token"
_NOTIFICATIONS = "rask-sa-notifications"


@pytest.fixture
def door(sa_issuer: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    issuer = sa_issuer
    for key, value in {
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": "https://idp.invalid",
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_SA_ISSUER": issuer.issuer,
        "RASK_SA_AUDIENCE": "rask-lineage",
        "RASK_SA_SUBJECTS": json.dumps({f"system:serviceaccount:default:{_NOTIFICATIONS}": "notifications"}),
        "RASK_SA_FETCH_TOKEN_FILE": str(issuer.fetch_token_file),
        "RASK_SA_CA_FILE": str(issuer.ca_file),
        # The deployment as it was: the shared token configured and notifications on the service allowlist.
        "APP_API_TOKEN": _SHARED,
        "LINEAGE_SERVICE_SUBJECTS": "notifications",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await attach_auth(app, get_settings(), service="lineage")
        yield

    app = FastAPI(lifespan=lifespan)
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))

    @app.get("/who")
    def who(caller: security.CurrentToken) -> dict[str, str | None]:
        return {"sub": caller.sub if caller is not None else None}

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("credential", "status", "subject"),
    [
        pytest.param("shared-token-and-a-name", 401, None, id="the-shared-token-and-a-claimed-name"),
        pytest.param("own-projected-token", 200, "notifications", id="the-pods-own-projected-token"),
    ],
)
def test_lineage_admits_a_service_by_its_projected_token_and_never_by_a_claimed_name(
    door: TestClient, sa_issuer: Any, credential: str, status: int, subject: str | None
) -> None:
    headers = (
        {"dapr-api-token": _SHARED, "x-lance-service-identity": "notifications"}
        if credential == "shared-token-and-a-name"
        else {"Authorization": f"Bearer {sa_issuer.mint(_NOTIFICATIONS, audience='rask-lineage')}"}
    )

    answered = door.get("/who", headers=headers)

    assert answered.status_code == status, answered.text
    if status == 200:
        assert answered.json() == {"sub": subject}
