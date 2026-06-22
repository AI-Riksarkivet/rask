# The service fleet: ports, entrypoints, and which brick each composes

The fleet (`make dev-micro`, driven by `dev-micro.sh` / `Procfile.micro`):
gateway `:8888` + core-api `:8801` + search `:8802` + volumes `:8803` +
ray `:8804` + orchestrator `:8810`. The frontend's Vite proxy targets `:8888`
(the gateway). With `make viewer` instead, `:8888` is the `core.main:app`
monolith — single-process dev convenience over the same `core` brick.

## Every HTTP entrypoint is a thin shell over `make_service_app`

Each `components/services/<svc>/src/<svc>/__init__.py` imports routers (+ maybe a
lifespan) from a brick and calls `service_kit.make_service_app`. No business
logic in the entrypoint.

| Service | Port | Composes brick(s) | Lifespan | DB? | Notes |
|---|---|---|---|---|---|
| `gateway` | 8888 | none (httpx proxy) | own asynccontextmanager | no | path-routes `/api/*` longest-prefix-first; **does not import `service-kit` or any domain brick** — pure forwarder. Upstreams env-overridable (`RASK_*_API_URL`). |
| `core_api` | 8801 | `core` | `core.lifespan.make_lifespan` | yes | batches/chunks/catalog/health; orchestrator loop **OFF** |
| `orchestrator` | 8810 | `core` (same brick) | `core.lifespan.make_lifespan` | yes | health + orchestrator endpoints; loop **ON** via `RASK_ORCHESTRATOR_AUTOSTART` |
| `volumes_api` | 8803 | `storage` only | default (stateless) | no | S3/IIIF image+ALTO proxy; builds storage sources on demand |
| `search_api` | 8802 | `storage` + `lancedb` | `search_api.lifespan.make_lifespan` | no | Lance `lines` FTS + S3 thumbnails (lines-only lifespan) |
| `ray_api` | 8804 | `ray-kit` | `ray_api.lifespan.make_lifespan` | no | Ray dashboard introspection; `proxy_router` mounts at root so `/api/serve/*` reaches Serve status API |

## The core-api / orchestrator split is config, not code

Both compose the **same `core` brick** with the **same `make_lifespan`**. They
differ only in (a) which routers they mount and (b) `RASK_ORCHESTRATOR_AUTOSTART`
— the lifespan starts the reconcile→derive→submit `asyncio.Task` only when it's
set. They run as **two processes over one brick, sharing the `batches` table
transactionally**. The loop must run in exactly one process, so the fleet runs
`core-api` with it OFF and `orchestrator` with it ON. Operators flip it at
runtime via `POST /api/v1/orchestrator/{start,stop}`. (Transitional — destined
to become a NATS JetStream consumer.)

## Why the viewer-free services never touch `core`

`volumes_api` / `search_api` / `ray_api` depend only on `service-kit` + their own
libs — **no `core`, no DB**. That's why `service-kit` must stay dependency-light
(no `lancedb`/`ray`/`sqlmodel`): pulling a heavy dep into `service-kit` would
force it onto these stateless services and onto every test that imports the
factory. Heavy deps live in the brick that needs them (`core` → sqlmodel,
`search_api` → lancedb, `ray-kit` → ray).

## Lifespan injection recap

`make_service_app(lifespan=...)` takes a `LifespanFactory =
Callable[[Settings], Callable[[FastAPI], AbstractAsyncContextManager[None]]]`.
Omit it → `default_lifespan` (puts `settings` on `app.state`, nothing else).
Stateful services pass a factory that opens DB/Lance/Ray/S3 on enter and tears
down on exit. This is the seam that lets one brick (`core`) back two processes
with different startup behavior.
