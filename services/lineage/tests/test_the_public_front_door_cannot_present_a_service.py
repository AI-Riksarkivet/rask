"""The public front door cannot present a service at lineage's door, even holding a valid service-account token.

A request the gateway forwards is a person's or nobody's. A service-account token arriving with the gateway's
`dapr-caller-app-id` is refused 403 before any verifier answers it, so a token lifted from a pod cannot be replayed
through the edge as that pod's service.

Driven at lineage's real door with the verifiers `attach_auth` builds from lineage's settings, against the loopback
service-account issuer in the root conftest; the token is one the same door admits from a service (the clause test,
`test_a_claimed_service_name_authenticates_nobody.py`).
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


_TRAINER = "rask-sa-ray"


@pytest.fixture
def door(sa_issuer: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    for key, value in {
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": "https://idp.invalid",
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_SA_ISSUER": sa_issuer.issuer,
        "RASK_SA_AUDIENCE": "rask-lineage",
        "RASK_SA_SUBJECTS": json.dumps({f"system:serviceaccount:default:{_TRAINER}": "service-trainer"}),
        "RASK_SA_FETCH_TOKEN_FILE": str(sa_issuer.fetch_token_file),
        "RASK_SA_CA_FILE": str(sa_issuer.ca_file),
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


def test_a_service_account_token_carried_by_the_public_front_door_is_refused(door: TestClient, sa_issuer: Any) -> None:
    token = sa_issuer.mint(_TRAINER, audience="rask-lineage")

    answered = door.get("/who", headers={"Authorization": f"Bearer {token}", "dapr-caller-app-id": "gateway"})

    assert answered.status_code == 403, answered.text
