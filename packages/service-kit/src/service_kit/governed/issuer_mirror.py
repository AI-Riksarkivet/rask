"""The cluster ServiceAccount issuer's discovery document and key set, served to a verifier that cannot carry a credential.

[[XC-077]]. OpenFGA authenticates its clients by their projected ServiceAccount tokens (OIDC, audience
`rask-openfga`), and verifying one needs the issuer's keys. OpenFGA v1.18.3 fetches
``<issuer>/.well-known/openid-configuration`` and then its ``jwks_uri`` with a bare HTTP client
(``internal/authn/oidc/oidc.go`` ``GetConfiguration``/``GetKeys``), and it refuses to start when either
fetch fails. The k3s API server serves both only to a bearer from its private CA (measured 2026-10-05:
``--anonymous-auth=false`` answers 401 to an unauthenticated GET of each), so OpenFGA cannot reach them
directly. The rask verifiers fetch them with their own token (`service_kit.governed.oidc`); this module
does the same for a verifier that cannot.

It answers two GETs and nothing else, and both bodies are public material: the discovery document with
``jwks_uri`` pointed at this mirror, and the key set as the API server serves it. Each request re-reads
the mirror pod's own API-audience token, because the kubelet rotates it, and fetches upstream; nothing is
cached, since a verifier asks at boot, every 48 h, and on an unknown key id (rate-limited to once a
minute by OpenFGA's ``keyfunc`` options). The ``iss`` the tokens carry stays the cluster issuer, so a
verifier names this mirror as its issuer URL and the cluster issuer as an alias (OpenFGA
``authn.oidc.issuerAliases``).

A plain FastAPI app rather than `make_service_app`: it has no governed door, no Dapr sidecar and no
state, and the chart runs it from the catalog image with ``uvicorn --factory``.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Final

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.machine_identity import IdentityTokenUnavailableError, identity_bearer


DISCOVERY_PATH: Final = "/.well-known/openid-configuration"
#: The path the Kubernetes API server serves its key set at; the mirror serves it at the same one.
JWKS_PATH: Final = "/openid/v1/jwks"


class IssuerMirrorSettings(BaseSettings):
    """Where the issuer is, how the mirror authenticates to it, and the URL verifiers reach the mirror by."""

    model_config = SettingsConfigDict(frozen=True, populate_by_name=True)

    issuer: str = Field(alias="RASK_ISSUER_MIRROR_ISSUER")
    public_url: str = Field(alias="RASK_ISSUER_MIRROR_PUBLIC_URL")
    token_file: str = Field(alias="RASK_ISSUER_MIRROR_TOKEN_FILE")
    ca_file: str | None = Field(default=None, alias="RASK_ISSUER_MIRROR_CA_FILE")
    timeout_seconds: float = Field(default=10.0, gt=0, alias="RASK_ISSUER_MIRROR_TIMEOUT_SECONDS")


def _upstream(request: Request) -> httpx.AsyncClient:
    return request.app.state.upstream


UpstreamDep = Annotated[httpx.AsyncClient, Depends(_upstream)]


async def _fetch(client: httpx.AsyncClient, url: str, token_file: str) -> dict[str, Any]:
    """One upstream GET carrying the mirror's token, read now; an upstream that does not answer is a 502."""
    try:
        response = await client.get(url, headers=identity_bearer(token_file))
    except IdentityTokenUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"the issuer did not answer {url}: {exc}") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"the issuer answered {response.status_code} to {url}")
    return response.json()


def create_app(settings: IssuerMirrorSettings | None = None) -> FastAPI:
    """The mirror app; ``settings`` defaults to the environment's (``uvicorn --factory`` passes none)."""
    resolved = settings or IssuerMirrorSettings()
    issuer = resolved.issuer.rstrip("/")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=resolved.ca_file) if resolved.ca_file else True
        async with httpx.AsyncClient(verify=verify, timeout=resolved.timeout_seconds) as client:
            app.state.upstream = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get(DISCOVERY_PATH)
    async def discovery(client: UpstreamDep) -> dict[str, Any]:
        document = await _fetch(client, f"{issuer}{DISCOVERY_PATH}", resolved.token_file)
        return {**document, "jwks_uri": f"{resolved.public_url.rstrip('/')}{JWKS_PATH}"}

    @app.get(JWKS_PATH)
    async def jwks(client: UpstreamDep) -> dict[str, Any]:
        document = await _fetch(client, f"{issuer}{DISCOVERY_PATH}", resolved.token_file)
        return await _fetch(client, str(document["jwks_uri"]), resolved.token_file)

    return app
