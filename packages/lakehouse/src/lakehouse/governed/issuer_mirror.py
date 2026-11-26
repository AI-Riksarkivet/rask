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
state, and the chart runs it from the catalog image with ``uvicorn --factory``. It logs as every lakehouse process does
(`lakehouse.process.log_handler.setup_logging`), so its lines reach stdout in the one format, at the one level. Its probes are the library's
(`lakehouse.process.probes`): ``/readyz`` is ready while the issuer serves this pod its discovery document, the one thing
the mirror depends on, as a health watch read it last (`lakehouse.process.health`): the probe awaits no fetch.
"""

import logging
import ssl
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from functools import partial
from typing import Annotated, Any, Final

import httpx
from fastapi import Depends, FastAPI, Request
from lance_namespace import ServiceUnavailableError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakehouse.governed.machine_identity import IdentityTokenUnavailableError, identity_bearer
from lakehouse.problems.ns_errors import install_problem_handlers
from lakehouse.process.correlation import FRAMEWORK_TELEMETRY_OFF
from lakehouse.process.draining import arm_drain_on_sigterm
from lakehouse.process.health import Dependency, HealthWatch
from lakehouse.process.lifecycle import mark_draining, mark_started
from lakehouse.process.log_handler import setup_logging
from lakehouse.process.probes import make_probes_router
from lakehouse.process.supervision import Supervisor
from lakehouse.schemas.health import Readiness


log = logging.getLogger(__name__)


DISCOVERY_PATH: Final = "/.well-known/openid-configuration"
#: The path the Kubernetes API server serves its key set at; the mirror serves it at the same one.
JWKS_PATH: Final = "/openid/v1/jwks"


class IssuerMirrorSettings(BaseSettings):
    """Where the issuer is, how the mirror authenticates to it, and the URL verifiers reach the mirror by."""

    model_config = SettingsConfigDict(frozen=True, validate_by_name=True, validate_by_alias=True)

    issuer: str = Field(alias="RASK_ISSUER_MIRROR_ISSUER")
    public_url: str = Field(alias="RASK_ISSUER_MIRROR_PUBLIC_URL")
    token_file: str = Field(alias="RASK_ISSUER_MIRROR_TOKEN_FILE")
    ca_file: str | None = Field(default=None, alias="RASK_ISSUER_MIRROR_CA_FILE")
    timeout_seconds: float = Field(default=10.0, gt=0, alias="RASK_ISSUER_MIRROR_TIMEOUT_SECONDS")


async def _upstream(request: Request) -> httpx.AsyncClient:
    return request.app.state.upstream


UpstreamDep = Annotated[httpx.AsyncClient, Depends(_upstream)]


class IssuerUnansweredError(ServiceUnavailableError):
    """The issuer did not answer the mirror's own fetch with the document: the fault is upstream of the mirror.

    The spec's outage, 503 with code 17: its error table has no 502, and one code rides one status. The issuer's address
    and answer are logged before the raise, since the body a caller reads is redacted.
    """


class _Discovery(BaseModel):
    """The issuer's discovery document: the key set's address is the one field the mirror reads, every other is served on."""

    model_config = ConfigDict(extra="allow")

    jwks_uri: str


class _KeySet(BaseModel):
    """The issuer's key set (RFC 7517 § 5), served as the issuer answered it."""

    model_config = ConfigDict(extra="allow")

    keys: list[dict[str, Any]]


async def _fetch[M: BaseModel](client: httpx.AsyncClient, url: str, token_file: str, *, shape: type[M]) -> M:
    """One upstream GET carrying the mirror's token, read now, its answer read as ``shape``.

    Raises:
        ServiceUnavailableError: The mirror's own token cannot be read.
        IssuerUnansweredError: The issuer did not answer, answered another status than 200, or answered a body that is
            not the document.
    """
    try:
        response = await client.get(url, headers=identity_bearer(token_file))
    except IdentityTokenUnavailableError as exc:
        # The cause names the token file's path, and this door answers callers with no credential.
        log.error("issuer_mirror_token_unreadable", exc_info=True)
        raise ServiceUnavailableError("the mirror's own token cannot be read") from exc
    except httpx.HTTPError as exc:
        log.warning("issuer_mirror_upstream_unanswered", extra={"url": url, "error": f"{type(exc).__name__}: {exc}"[:300]})
        raise IssuerUnansweredError(f"the issuer did not answer {url}: {exc}") from exc
    if response.status_code != 200:
        log.warning("issuer_mirror_upstream_unanswered", extra={"url": url, "status": response.status_code})
        raise IssuerUnansweredError(f"the issuer answered {response.status_code} to {url}")
    try:
        return shape.model_validate_json(response.content)
    except ValidationError as exc:
        log.warning("issuer_mirror_upstream_unanswered", extra={"url": url, "status": response.status_code, "error": str(exc)[:300]})
        raise IssuerUnansweredError(f"the issuer answered {url} with a body that is not its {shape.__name__.lstrip('_').lower()}") from exc


async def _discovered(client: httpx.AsyncClient, settings: IssuerMirrorSettings) -> _Discovery:
    """The issuer's discovery document, fetched now under the mirror's own token."""
    return await _fetch(client, f"{settings.issuer.rstrip('/')}{DISCOVERY_PATH}", settings.token_file, shape=_Discovery)


def _lifespan(settings: IssuerMirrorSettings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """The mirror's lifespan: its upstream client, and a health watch reading the issuer's discovery document, which
    ``/readyz`` answers from."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=settings.ca_file) if settings.ca_file else True
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(httpx.AsyncClient(verify=verify, timeout=settings.timeout_seconds))
            loops = await stack.enter_async_context(Supervisor())
            app.state.upstream = client
            issuer_answers = Dependency(name="issuer", read=partial(_discovered, client, settings), required=True)
            app.state.health = HealthWatch([issuer_answers])
            # One read before the start, so the first probe answers what the issuer said rather than that nobody asked.
            await app.state.health.check(issuer_answers)
            app.state.health.watch(loops)
            mark_started(app)
            # At SIGTERM, not at teardown: uvicorn reaches the teardown only once it has drained.
            disarm = arm_drain_on_sigterm(app)
            try:
                yield
            finally:
                mark_draining(app)
                disarm()

    return lifespan


async def _ready(request: Request) -> Readiness:
    health: HealthWatch = request.app.state.health
    return health.readiness()


def create_app(settings: IssuerMirrorSettings | None = None) -> FastAPI:
    """The mirror app; ``settings`` defaults to the environment's (``uvicorn --factory`` passes none)."""
    setup_logging()
    resolved = settings or IssuerMirrorSettings()
    app = FastAPI(lifespan=_lifespan(resolved), docs_url=None, redoc_url=None, openapi_url=None, telemetry=FRAMEWORK_TELEMETRY_OFF)
    install_problem_handlers(app, log)
    app.include_router(make_probes_router(_ready))

    @app.get(DISCOVERY_PATH)
    async def discovery(client: UpstreamDep) -> dict[str, Any]:
        document = await _discovered(client, resolved)
        return {**document.model_dump(), "jwks_uri": f"{resolved.public_url.rstrip('/')}{JWKS_PATH}"}

    @app.get(JWKS_PATH)
    async def jwks(client: UpstreamDep) -> dict[str, Any]:
        document = await _discovered(client, resolved)
        return (await _fetch(client, document.jwks_uri, resolved.token_file, shape=_KeySet)).model_dump()

    return app


__all__ = [
    "DISCOVERY_PATH",
    "JWKS_PATH",
    "IssuerMirrorSettings",
    "IssuerUnansweredError",
    "UpstreamDep",
    "create_app",
]
