"""At lineage's door a service is the service account its projected token names, and a name it claims proves nothing.

[[LH-220]], clause "a claimed name answers 401". A service authenticates with its pod's projected token, verified
offline against the cluster issuer, and is the subject its full username names
(`kubernetes~system:serviceaccount:<ns>:<sa>`, ADR 0156 C5); a name it claims in `x-lance-service-identity` names nobody,
or any caller could speak as any service.

Driven at lineage's real door: its `authenticate`, the verifiers `attach_auth` builds from lineage's settings at boot,
and the handlers `build_service_app` installs. The service-account issuer is the loopback one in the root
conftest, which serves its key set only to a bearer from a private CA, as k3s does. A service admitted by its own
projected token is driven end to end by `tests/integration/test_a_service_account_records_lineage_on_the_tables_it_names.py`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lakehouse.exceptions import register_handlers
from lakehouse.governed.audit import AUDIT_LOGGER
from lakehouse.governed.auth_lifespan import attach_auth
from lakehouse.ns_errors import install_problem_handlers
from lakehouse.supervision import Supervisor
from lineage.api import security
from lineage.core.config import get_settings


@pytest.fixture
def door(sa_issuer: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    issuer = sa_issuer
    for key, value in {
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": "https://idp.invalid",
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_OIDC_IDP_ID": "dex",
        "RASK_OIDC_CONNECTORS": '{"local": "dex"}',
        "RASK_SA_ISSUER": issuer.issuer,
        "RASK_SA_AUDIENCE": "rask-lineage",
        "RASK_SA_FETCH_TOKEN_FILE": str(issuer.fetch_token_file),
        "RASK_SA_CA_FILE": str(issuer.ca_file),
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with Supervisor() as loops:
            await attach_auth(app, get_settings(), service="lineage", loops=loops)
            yield

    app = FastAPI(lifespan=lifespan)
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))

    @app.get("/who")
    def who(caller: security.CurrentToken) -> dict[str, str | None]:
        return {"sub": caller.subject if caller is not None else None}

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    get_settings.cache_clear()


def test_lineage_refuses_a_claimed_service_name(door: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """The refusal is on the audit trail, as the catalog's is: a door that refuses a caller and records nothing leaves
    no evidence anyone knocked ([[LH-585]])."""
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)

    answered = door.get("/who", headers={"x-lance-service-identity": "maintenance"})

    assert answered.status_code == 401, answered.text
    recorded = [(r.__dict__["audit.action"], r.__dict__["audit.outcome"], r.__dict__["audit.reason"]) for r in caplog.records if r.name == AUDIT_LOGGER]
    assert recorded == [("authn", "failure", "missing_token")]
