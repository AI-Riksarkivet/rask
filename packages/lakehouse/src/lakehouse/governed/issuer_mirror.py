"""The cluster ServiceAccount issuer's discovery document and key set, served to a verifier that cannot carry a credential.

[[XC-077]]. OpenFGA authenticates its clients by their projected ServiceAccount tokens (OIDC, audience
`rask-openfga`), and verifying one needs the issuer's keys. OpenFGA v1.18.3 fetches
``<issuer>/.well-known/openid-configuration`` and then its ``jwks_uri`` with a bare HTTP client
(``internal/authn/oidc/oidc.go`` ``GetConfiguration``/``GetKeys``), and it refuses to start when either
fetch fails. The k3s API server serves both only to a bearer from its private CA (measured 2026-10-05:
``--anonymous-auth=false`` answers 401 to an unauthenticated GET of each), so OpenFGA cannot reach them
directly. The rask verifiers fetch them with their own token (`lakehouse.governed.oidc`); this module
does the same for a verifier that cannot.

It answers two GETs and its probes, and both bodies are public material: the discovery document with
``jwks_uri`` pointed at this mirror, and the key set as the API server serves it. Each request re-reads
the mirror pod's own API-audience token, because the kubelet rotates it, and fetches upstream; nothing is
cached, since a verifier asks at boot, every 48 h, and on an unknown key id (rate-limited to once a
minute by OpenFGA's ``keyfunc`` options). The ``iss`` the tokens carry stays the cluster issuer, so a
verifier names this mirror as its issuer URL and the cluster issuer as an alias (OpenFGA
``authn.oidc.issuerAliases``).

A plain FastAPI app rather than `lakehouse.service_app.factory`'s: it has no governed door and no
state, and the chart runs it from the catalog image with ``uvicorn --factory``. Its probes are the library's
(`lakehouse.probes`): ``/readyz`` is ready while the issuer serves this pod its discovery document, the one thing
the mirror depends on.
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Annotated, Any, Final

import httpx
from fastapi import Depends, FastAPI, Request
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakehouse.draining import arm_drain_on_sigterm
from lakehouse.exceptions import DomainError, ServiceUnavailableError, register_handlers
from lakehouse.governed.machine_identity import IdentityTokenUnavailableError, identity_bearer
from lakehouse.lifecycle import mark_draining, mark_started
from lakehouse.probes import make_probes_router
from lakehouse.schemas.health import Readiness, ReadinessStatus


log = logging.getLogger(__name__)


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


class IssuerUnansweredError(DomainError):
    """The issuer did not answer the mirror's own fetch with the document: the fault is upstream of the mirror."""

    status_code = HTTPStatus.BAD_GATEWAY
    title = "Bad Gateway"


async def _fetch(client: httpx.AsyncClient, url: str, token_file: str) -> dict[str, Any]:
    """One upstream GET carrying the mirror's token, read now.

    Raises:
        ServiceUnavailableError: The mirror's own token cannot be read.
        IssuerUnansweredError: The issuer did not answer, or answered another status than 200.
    """
    try:
        response = await client.get(url, headers=identity_bearer(token_file))
    except IdentityTokenUnavailableError as exc:
        # The cause names the token file's path, and this door answers callers with no credential.
        log.error("issuer_mirror_token_unreadable", exc_info=True)
        raise ServiceUnavailableError("the mirror's own token cannot be read") from exc
    except httpx.HTTPError as exc:
        raise IssuerUnansweredError(f"the issuer did not answer {url}: {exc}") from exc
    if response.status_code != 200:
        raise IssuerUnansweredError(f"the issuer answered {response.status_code} to {url}")
    return response.json()


def create_app(settings: IssuerMirrorSettings | None = None) -> FastAPI:
    """The mirror app; ``settings`` defaults to the environment's (``uvicorn --factory`` passes none)."""
    resolved = settings or IssuerMirrorSettings()
    issuer = resolved.issuer.rstrip("/")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=resolved.ca_file) if resolved.ca_file else True
        async with httpx.AsyncClient(verify=verify, timeout=resolved.timeout_seconds) as client:
            app.state.upstream = client
            mark_started(app)
            # At SIGTERM, not at teardown: uvicorn reaches the teardown only once it has drained.
            disarm = arm_drain_on_sigterm(app)
            try:
                yield
            finally:
                mark_draining(app)
                disarm()

    async def ready(request: Request) -> Readiness:
        try:
            await _fetch(request.app.state.upstream, f"{issuer}{DISCOVERY_PATH}", resolved.token_file)
        except DomainError as exc:
            return Readiness(status=ReadinessStatus.unhealthy, components={"issuer": exc.detail})
        return Readiness(status=ReadinessStatus.ready, components={"issuer": "healthy"})

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    register_handlers(app)
    app.include_router(make_probes_router(ready))

    @app.get(DISCOVERY_PATH)
    async def discovery(client: UpstreamDep) -> dict[str, Any]:
        document = await _fetch(client, f"{issuer}{DISCOVERY_PATH}", resolved.token_file)
        return {**document, "jwks_uri": f"{resolved.public_url.rstrip('/')}{JWKS_PATH}"}

    @app.get(JWKS_PATH)
    async def jwks(client: UpstreamDep) -> dict[str, Any]:
        document = await _fetch(client, f"{issuer}{DISCOVERY_PATH}", resolved.token_file)
        return await _fetch(client, str(document["jwks_uri"]), resolved.token_file)

    return app
