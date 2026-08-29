"""SK-08 — `make_service_app` builds its own `Settings` at IMPORT time and offers no way in.

Four of the five factory callers invoke `make_service_app(...)` at module level, so `build_settings()`
— `load_dotenv()` plus a full `Settings.model_validate({})` off the process environment — runs as a
side effect of `import compute` / `import flows` / `import notifications` / `import gateway`. A test,
a script or a second app in the same process cannot say what the settings are; it can only mutate the
environment before the import and hope nothing imported first.

The seam is a parameter. `settings=` supplied ⇒ that object IS the app's settings, reaching
`app.state.settings`, the router prefix, the docs URLs, the middleware and the lifespan factory.
Omitted ⇒ `build_settings()` exactly as before, so no caller changes.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from service_kit import make_service_app
from service_kit.config import Settings
from service_kit.dependencies import SettingsDep


def _router() -> APIRouter:
    router = APIRouter()

    @router.get("/where")
    async def where(settings: SettingsDep) -> dict[str, str]:
        return {"prefix": settings.api_prefix}

    return router


def _injected() -> Settings:
    return Settings.model_validate({"RASK_API_PREFIX": "/api/injected", "RASK_DOCS": True, "RASK_MAX_BODY_BYTES": 4096})


def test_the_injected_settings_reach_the_router_prefix_and_the_docs_urls() -> None:
    settings = _injected()
    app = make_service_app(title="injected", routers=[_router()], settings=settings)
    with TestClient(app) as client:
        assert client.get("/api/injected/where").status_code == 200
        assert client.get("/api/injected/where").json() == {"prefix": "/api/injected"}
        assert client.get("/api/injected/openapi.json").status_code == 200


def test_the_injected_object_itself_is_what_the_app_carries() -> None:
    settings = _injected()
    app = make_service_app(title="injected", routers=[APIRouter()], settings=settings)
    with TestClient(app):
        assert app.state.settings is settings, "the factory rebuilt Settings instead of using the injected object"


def test_the_injected_settings_reach_a_service_lifespan() -> None:
    seen: list[Settings] = []

    def lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
        seen.append(settings)

        @asynccontextmanager
        async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
            app.state.settings = settings
            yield

        return _lifespan

    settings = _injected()
    with TestClient(make_service_app(title="injected", routers=[APIRouter()], lifespan=lifespan, settings=settings)):
        pass
    assert seen == [settings]


def test_omitting_settings_still_builds_them_from_the_environment() -> None:
    app = make_service_app(title="default", routers=[APIRouter()])
    with TestClient(app):
        assert isinstance(app.state.settings, Settings)
