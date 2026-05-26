# Health Checks

FastAPI-side health endpoints (`/livez`, `/readyz`) with healthy / degraded / unhealthy states and per-component reporting. K8s probe wiring lives in [`kubernetes.md`](kubernetes.md).

## Contents

- Two endpoints, two purposes
- Three states, not two
- Track startup + shutdown state on `app.state`
- Component check primitives
- Endpoints
- Rules of thumb

## Two endpoints, two purposes

Don't conflate them.

| Endpoint   | Used by              | Behaviour                                                                                                          |
| ---------- | -------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `/livez`   | Process supervisor   | Cheap — return 200 if the event loop is responsive. **No** dependency calls.                                       |
| `/readyz`  | Load balancer / k8s  | Check each critical dependency. Return 200 / 503 / 200-with-degraded based on per-component results.               |

## Three states, not two

Real systems have a middle ground: dependencies that are **degraded** (pool near exhaustion, external API slow) but the service can still serve traffic. Surface that distinction so dashboards and runbooks can act on it without pulling the pod.

| State           | HTTP | Meaning                                                         | k8s effect                       |
| --------------- | :--: | --------------------------------------------------------------- | -------------------------------- |
| `healthy`       | 200  | All components nominal.                                         | In rotation.                     |
| `degraded`      | 200  | Some component is slow / near-limit but requests still succeed. | **Still in rotation** — alert.   |
| `unhealthy`     | 503  | A required component is down.                                   | Out of rotation until 200.       |
| (shutting down) | 503  | Lifespan post-`yield` started.                                  | Out of rotation; pod terminating.|

## Track startup + shutdown state on `app.state`

Set flags from the lifespan — never module-level globals.

```python
# main.py — extend the lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.startup_complete = False
    app.state.shutting_down = False
    # ... build engine, http client, etc. (see production-patterns § Lifespan) ...
    app.state.startup_complete = True

    yield

    app.state.shutting_down = True
    # ... dispose ...
```

## Component check primitives

```python
# core/health.py
from enum import StrEnum
from time import perf_counter
import asyncio

import httpx
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ComponentHealth(BaseModel):
    name: str
    status: HealthStatus
    latency_ms: float | None = None
    message: str | None = None
    details: dict[str, object] | None = None


async def check_db(engine: AsyncEngine, *, timeout: float = 2.0) -> ComponentHealth:
    start = perf_counter()
    try:
        async with asyncio.timeout(timeout):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        latency = (perf_counter() - start) * 1000

        # Pool-pressure check — degraded, not unhealthy.
        pool = engine.pool
        size = pool.size()
        idle = pool.checkedin()
        if idle < 2:
            return ComponentHealth(
                name="db", status=HealthStatus.DEGRADED, latency_ms=latency,
                message=f"low idle connections ({idle}/{size})",
                details={"pool_size": size, "idle": idle},
            )
        return ComponentHealth(name="db", status=HealthStatus.HEALTHY, latency_ms=latency,
                               details={"pool_size": size, "idle": idle})
    except TimeoutError:
        return ComponentHealth(name="db", status=HealthStatus.UNHEALTHY, message="timeout")
    except Exception as e:
        return ComponentHealth(name="db", status=HealthStatus.UNHEALTHY,
                               message=f"{type(e).__name__}: {e}")


async def check_http(client: httpx.AsyncClient, url: str, name: str,
                     *, timeout: float = 2.0) -> ComponentHealth:
    start = perf_counter()
    try:
        r = await client.get(url, timeout=timeout)
        latency = (perf_counter() - start) * 1000
        if r.status_code == 200:
            return ComponentHealth(name=name, status=HealthStatus.HEALTHY, latency_ms=latency)
        return ComponentHealth(name=name, status=HealthStatus.DEGRADED, latency_ms=latency,
                               message=f"HTTP {r.status_code}")
    except httpx.TimeoutException:
        return ComponentHealth(name=name, status=HealthStatus.UNHEALTHY, message="timeout")
    except Exception as e:
        return ComponentHealth(name=name, status=HealthStatus.UNHEALTHY,
                               message=f"{type(e).__name__}: {e}")
```

## Endpoints

```python
# api/health.py
import asyncio
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.core.health import HealthStatus, check_db, check_http

router = APIRouter(tags=["health"])


@router.get("/livez")
async def liveness() -> dict[str, str]:
    # No dependency calls — just proves the event loop is responsive.
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(request: Request) -> JSONResponse:
    state = request.app.state

    if not state.startup_complete:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "starting"},
        )
    if state.shutting_down:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "shutting_down"},
        )

    components = list(await asyncio.gather(
        check_db(state.db_engine),
        # check_http(state.http, "https://idp.example.com/.well-known/...", "idp"),
    ))

    statuses = {c.status for c in components}
    if HealthStatus.UNHEALTHY in statuses:
        overall, http_status = HealthStatus.UNHEALTHY, status.HTTP_503_SERVICE_UNAVAILABLE
    elif HealthStatus.DEGRADED in statuses:
        # 200 — keep serving — but flag it so dashboards / alerts can act.
        overall, http_status = HealthStatus.DEGRADED, status.HTTP_200_OK
    else:
        overall, http_status = HealthStatus.HEALTHY, status.HTTP_200_OK

    return JSONResponse(
        status_code=http_status,
        content={"status": overall.value,
                 "components": [c.model_dump() for c in components]},
    )
```

Mount **without** the API version prefix — operational, not part of the public API:

```python
app.include_router(router)                                # → /livez, /readyz
app.include_router(api_router, prefix=settings.API_V1_STR)
```

## Rules of thumb

- **Liveness never touches a dependency.** A DB blip should not restart every replica.
- **Readiness check is per-request fresh.** Don't `@lru_cache` it; you want the real state, not a stale "yes".
- **Slow startup → startup probe.** If your lifespan takes >15s (model loading, large warm-up), add a `startupProbe` in the Deployment (see `kubernetes.md`) so liveness doesn't kill the pod during boot.
- **Degraded ≠ broken.** Don't 503 on `pool_pre_ping` warnings or a slow IdP — emit metrics, return 200, let an alert fire.
