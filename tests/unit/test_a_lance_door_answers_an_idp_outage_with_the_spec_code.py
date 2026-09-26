"""A door that answers in the Lance taxonomy answers an IdP outage in it too: a 503 carrying the spec's `code` 17.

`lance_docs/ns_catalog/spec.yaml` requires `code` on `ErrorResponse`, which `ServiceUnavailableErrorResponse`
references, and a generated Lance client dispatches on it. Every door here raises `lance_namespace` errors for its own
refusals, so an outage that left one as the fleet's four-key `about:blank#` body would be the one answer on that door a
Lance client cannot read. The same body would also carry the in-cluster URL the verifier could not use, past the 5xx
redaction every other Lance-shaped 503 gets.

EVERY PROVIDER FAULT, NOT ONE. Each door catches the verifier's `ProviderUnavailableError` by type, so a door answers
code 17 only for the faults whose raise site uses that subclass. One case per raise site in `oidc.py`, all seven: a
site that raised the fleet base class instead would leave its fault answering the fleet body at every door here.

AND ITS LOG LINE. The Lance body redacts the message, so the warning each site logs is the only place an operator sees
which fault it was. Each case asserts that event, which also proves the case reached the site it is named for.

THE VERIFIER IS REAL, and so is the IdP: a loopback HTTP server serving the broken document, or a port nothing accepts
on. The non-HTTPS URL check needs no IdP: an HTTPS issuer fetched through an http split-horizon override is refused
before any fetch. Each door sits behind both handler installers in the order both app factories install them
(`register_handlers`, then `install_problem_handlers`), so the body asserted is the one a client receives. The
catalog's door also has its own file, `services/catalog/tests/test_an_unreachable_idp_is_audited_as_ours.py`, because
it also audits the refusal.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Awaitable, Callable, Iterator
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from lance_namespace import ErrorCode
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict

from catalog.api import security as catalog_security
from catalog.core.config import Settings as CatalogSettings
from ingest import auth as ingest_auth
from lineage.api import security as lineage_security
from lineage.core.config import LineageSettings
from medallion.api import produce_auth
from medallion.core.config import MedallionSettings
from service_kit.exceptions import register_handlers
from service_kit.governed import oidc
from service_kit.governed.oidc import OIDCVerifier
from service_kit.lakehouse.ns_errors import install_problem_handlers


AUDIENCE = "rask"
DISCOVERY_PATH = "/.well-known/openid-configuration"
JWKS_PATH = "/jwks"

type _Door = Callable[[Request, str, str], Awaitable[object]]


def _oidc_on(issuer: str) -> dict[str, object]:
    return {"RASK_OIDC_ENABLED": True, "RASK_OIDC_ISSUER": issuer, "RASK_OIDC_AUDIENCE": AUDIENCE, "RASK_OIDC_ALLOW_INSECURE": True}


def _credentials(bearer: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=bearer)


async def _catalog(request: Request, issuer: str, bearer: str) -> object:
    settings = CatalogSettings.model_validate({"LANCE_S3_ACCESS_KEY_ID": "x", "LANCE_S3_SECRET_ACCESS_KEY": "y", **_oidc_on(issuer)})
    return catalog_security.authenticate(request, settings, _credentials(bearer))


async def _lineage(request: Request, issuer: str, bearer: str) -> object:
    settings = LineageSettings.model_validate(_oidc_on(issuer))
    return lineage_security.authenticate(request, settings, _credentials(bearer))


async def _medallion_produce(request: Request, issuer: str, bearer: str) -> object:
    # A configured service token, so the door is not the unconfigured refusal; none presented, so it reaches the bearer.
    settings = MedallionSettings.model_validate({**_oidc_on(issuer), "APP_API_TOKEN": "s3cr3t"})
    return await produce_auth.authorize_produce(request, settings, cast(OpenFgaClient, object()), authorization=f"Bearer {bearer}")


async def _medallion_subject(request: Request, issuer: str, bearer: str) -> object:
    settings = MedallionSettings.model_validate(_oidc_on(issuer))
    return await produce_auth.authenticate_subject(request, settings, authorization=f"Bearer {bearer}")


async def _ingest(request: Request, issuer: str, bearer: str) -> object:
    settings = ingest_auth.IngestAuthSettings.model_validate(_oidc_on(issuer))
    return await ingest_auth.authorize_ingest(request, settings, project="demo", authorization=f"Bearer {bearer}")


DOORS: dict[str, _Door] = {
    "catalog-authenticate": _catalog,
    "lineage-authenticate": _lineage,
    "medallion-authorize-produce": _medallion_produce,
    "medallion-authenticate-subject": _medallion_subject,
    "ingest-authorize-ingest": _ingest,
}


class _Fault(StrEnum):
    """One per provider-fault raise site in `oidc.py`."""

    DISCOVERY_OVER_HTTP = "discovery-over-http"
    DISCOVERY_UNREACHABLE = "discovery-unreachable"
    DISCOVERY_NOT_A_DOCUMENT = "discovery-not-a-document"
    DISCOVERY_NAMES_ANOTHER_ISSUER = "discovery-names-another-issuer"
    DISCOVERY_ADVERTISES_NO_ALGORITHM_WE_ACCEPT = "discovery-advertises-no-algorithm-we-accept"
    KEY_SET_UNREACHABLE = "key-set-unreachable"
    KEY_SET_WITHOUT_A_USABLE_KEY = "key-set-without-a-usable-key"


#: The warning each fault's raise site logs.
LOGGED_AS: dict[_Fault, str] = {
    _Fault.DISCOVERY_OVER_HTTP: "oidc_insecure_url",
    _Fault.DISCOVERY_UNREACHABLE: "oidc_discovery_unreachable",
    _Fault.DISCOVERY_NOT_A_DOCUMENT: "oidc_discovery_malformed",
    _Fault.DISCOVERY_NAMES_ANOTHER_ISSUER: "oidc_discovery_issuer_mismatch",
    _Fault.DISCOVERY_ADVERTISES_NO_ALGORITHM_WE_ACCEPT: "oidc_no_common_algorithm",
    _Fault.KEY_SET_UNREACHABLE: "oidc_jwks_unreachable",
    _Fault.KEY_SET_WITHOUT_A_USABLE_KEY: "oidc_jwks_malformed",
}


class _BrokenProvider(BaseModel):
    """An issuer whose provider fails with `fault`, and the verifier that meets the failure."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    fault: _Fault
    issuer: str
    verifier: OIDCVerifier


def _refusing_port() -> socket.socket:
    """A port bound and never listening, so a connect is refused at once and no other process can take it meanwhile."""
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    return held


def _discovery(idp_url: str, /, **overrides: object) -> bytes:
    """A sound discovery document for the IdP at `idp_url`, with `overrides` replacing its fields."""
    return json.dumps({"issuer": idp_url, "jwks_uri": f"{idp_url}{JWKS_PATH}", **overrides}).encode()


def _documents(fault: _Fault, issuer: str) -> dict[str, bytes]:
    """What the IdP serves for `fault`, by path; a path absent here answers 404."""
    match fault:
        case _Fault.DISCOVERY_NOT_A_DOCUMENT:
            return {DISCOVERY_PATH: b"<!doctype html><title>Sign in</title>"}
        case _Fault.DISCOVERY_NAMES_ANOTHER_ISSUER:
            return {DISCOVERY_PATH: _discovery(issuer, issuer="https://another-issuer.example.test")}
        case _Fault.DISCOVERY_ADVERTISES_NO_ALGORITHM_WE_ACCEPT:
            return {DISCOVERY_PATH: _discovery(issuer, id_token_signing_alg_values_supported=["PS256"])}
        case _Fault.KEY_SET_UNREACHABLE:
            return {DISCOVERY_PATH: _discovery(issuer)}
        case _Fault.KEY_SET_WITHOUT_A_USABLE_KEY:
            return {DISCOVERY_PATH: _discovery(issuer), JWKS_PATH: json.dumps({"keys": []}).encode()}
        case _Fault.DISCOVERY_OVER_HTTP | _Fault.DISCOVERY_UNREACHABLE:
            raise ValueError(f"{fault} is served by no IdP at all")


def _loopback_verifier(issuer: str) -> OIDCVerifier:
    return OIDCVerifier(issuer, AUDIENCE, cache_ttl=300, allow_insecure=True)


@pytest.fixture(params=list(_Fault), ids=str)
def broken_provider(request: pytest.FixtureRequest) -> Iterator[_BrokenProvider]:
    fault = _Fault(request.param)
    if fault is _Fault.DISCOVERY_OVER_HTTP:
        issuer = "https://idp.example.test"
        # Refused before any fetch; the port refuses too, so a verifier that skipped the check logs a different event.
        with _refusing_port() as held:
            override = f"http://127.0.0.1:{held.getsockname()[1]}"
            verifier = OIDCVerifier(issuer, AUDIENCE, cache_ttl=300, allow_insecure=False, discovery_overrides={issuer: override})
            yield _BrokenProvider(fault=fault, issuer=issuer, verifier=verifier)
        return
    if fault is _Fault.DISCOVERY_UNREACHABLE:
        with _refusing_port() as held:
            issuer = f"http://127.0.0.1:{held.getsockname()[1]}"
            yield _BrokenProvider(fault=fault, issuer=issuer, verifier=_loopback_verifier(issuer))
        return

    documents: dict[str, bytes] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = documents.get(self.path)
            self.send_response(404 if body is None else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — stdlib's own parameter name
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    issuer = f"http://127.0.0.1:{server.server_address[1]}"
    documents.update(_documents(fault, issuer))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    try:
        yield _BrokenProvider(fault=fault, issuer=issuer, verifier=_loopback_verifier(issuer))
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _no_service_token_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every door reads `APP_API_TOKEN` from the process too; a developer's shell must not choose the branch under test."""
    monkeypatch.delenv("APP_API_TOKEN", raising=False)


def _client(door: _Door, provider: _BrokenProvider) -> TestClient:
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.state.oidc = provider.verifier
    # Never verified: every fault is met before a signature is checked. The `kid` carries it to the key-set fetch.
    claims = {"iss": provider.issuer, "sub": "gina", "aud": AUDIENCE, "iat": 0, "exp": 1 << 31}
    bearer = jwt.encode(claims, "unverified", algorithm="HS256", headers={"kid": "k1"})

    @app.get("/door")
    async def _door(request: Request) -> dict[str, str]:
        await door(request, provider.issuer, bearer)
        return {"door": "open"}

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("door", DOORS.values(), ids=DOORS.keys())
def test_a_provider_the_door_cannot_use_is_a_503_with_the_spec_code(door: _Door, broken_provider: _BrokenProvider, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=oidc.__name__)

    response = _client(door, broken_provider).get("/door")

    assert response.status_code == 503, response.text
    assert response.json()["code"] == ErrorCode.SERVICE_UNAVAILABLE, response.text
    assert broken_provider.issuer not in response.text
    assert [record.getMessage() for record in caplog.records if record.name == oidc.__name__] == [LOGGED_AS[broken_provider.fault]]
