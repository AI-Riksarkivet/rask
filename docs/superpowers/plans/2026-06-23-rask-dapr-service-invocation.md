# Dapr Sidecars + Service Invocation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inject Dapr sidecars on the six rask backend services, expose a shared `DaprClient` to every service, and route the gateway to the backends via Dapr service invocation (with a clean fallback to the existing httpx env-URL path).

**Architecture:** A `RASK_DAPR_ENABLED` flag gates everything. `service-kit` gains a `build_dapr_client` factory; `make_service_app` wraps the injected lifespan so every service it builds gets `app.state.dapr` (+ a `DaprClientDep`). The gateway, which is a standalone app (not built by `make_service_app`), maps each path-prefix to a Dapr **app-id** and, when enabled, rewrites upstreams to its local sidecar's invoke endpoint (`http://127.0.0.1:$DAPR_HTTP_PORT/v1.0/invoke/<app-id>/method<path>`); when disabled it uses the env URLs exactly as today. The chart injects sidecars via `fleet.yaml` annotations driven by a `dapr.sidecars` toggle.

**Tech Stack:** Python 3.13 / uv workspace, FastAPI + uvicorn, Dapr (Python SDK `dapr`), Helm (umbrella chart), k3s.

## Global Constraints

- Feature flag `RASK_DAPR_ENABLED` (bool). `service-kit` `Settings` default **false**; the chart ConfigMap sets it **"true"**. When false: no `DaprClient` is built and the gateway uses the httpx env-URL path.
- Dapr Python SDK dependency: `dapr` (pin the exact latest stable resolved at implementation time, e.g. `dapr>=1.15,<2`; record the resolved pin in the commit).
- Dapr sidecar HTTP port read from `DAPR_HTTP_PORT` (default `"3500"`); the injector sets it.
- app-id == the service key in `chart/values.yaml` `services`: `core-api`, `search-api`, `volumes-api`, `ray-api`, `orchestrator`, `gateway`.
- API prefix stays `/api` in the cluster (`RASK_API_PREFIX`); gateway longest-prefix routing semantics unchanged.
- The dapr SDK import MUST be lazy (inside `build_dapr_client`) so non-Dapr runs (tests, `make viewer`) don't require the package at import time.
- Orchestrator stays a singleton (Recreate); sidecar injection does not change that.
- No `Co-Authored-By: Claude` trailer on commits/PRs.
- Run Python via `uv run`; full suite is `uv run --all-packages pytest <path> --no-cov` for a single service's tests.

## File Structure

- `packages/service-kit/src/service_kit/config.py` — add `dapr_enabled` + `dapr_http_port` settings (modify).
- `packages/service-kit/src/service_kit/__init__.py` — `build_dapr_client`, `get_dapr`/`DaprClientDep`, lifespan-wrapping in `make_service_app` (modify).
- `packages/service-kit/pyproject.toml` — add `dapr` dependency (modify).
- `packages/service-kit/tests/test_dapr.py` — unit tests for the factory + gating (create).
- `components/services/gateway/src/gateway/__init__.py` — app-id routing + invoke/fallback (modify).
- `components/services/gateway/tests/test_routing.py` — unit tests for URL building (create).
- `chart/values.yaml` — `dapr.sidecars`/`logLevel` block + `config.RASK_DAPR_ENABLED` (modify).
- `chart/templates/fleet.yaml` — sidecar annotations (modify).

---

### Task 1: service-kit Settings — Dapr fields

**Files:**
- Modify: `packages/service-kit/src/service_kit/config.py` (after line 96, the `orchestrator_autostart` field)
- Test: `packages/service-kit/tests/test_dapr.py` (create)

**Interfaces:**
- Produces: `Settings.dapr_enabled: bool` (alias `RASK_DAPR_ENABLED`, default `False`), `Settings.dapr_http_port: str` (alias `DAPR_HTTP_PORT`, default `"3500"`).

- [ ] **Step 1: Write the failing test**

Create `packages/service-kit/tests/test_dapr.py`:

```python
"""service-kit Dapr wiring — config gating + client factory (no sidecar needed)."""

import os

import pytest

from service_kit.config import Settings


def _settings(**env: str) -> Settings:
    return Settings.model_validate(
        {"RASK_VIEWER_INPUT": "/dev/null", "RASK_VIEWER_OUTPUT": "/dev/null", **env}
    )


def test_dapr_disabled_by_default() -> None:
    s = _settings()
    assert s.dapr_enabled is False
    assert s.dapr_http_port == "3500"


def test_dapr_enabled_from_env() -> None:
    s = _settings(RASK_DAPR_ENABLED="true", DAPR_HTTP_PORT="3555")
    assert s.dapr_enabled is True
    assert s.dapr_http_port == "3555"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --all-packages pytest packages/service-kit/tests/test_dapr.py -v --no-cov`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'dapr_enabled'`.

- [ ] **Step 3: Add the settings fields**

In `packages/service-kit/src/service_kit/config.py`, immediately after the `orchestrator_autostart` field (line 96), add:

```python

    # Dapr service invocation. When false, build_dapr_client returns None and the
    # gateway falls back to direct httpx upstreams. DAPR_HTTP_PORT is set by the
    # Dapr sidecar injector in-cluster.
    dapr_enabled: bool = Field(default=False, alias="RASK_DAPR_ENABLED")
    dapr_http_port: str = Field(default="3500", alias="DAPR_HTTP_PORT")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --all-packages pytest packages/service-kit/tests/test_dapr.py -v --no-cov`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add packages/service-kit/src/service_kit/config.py packages/service-kit/tests/test_dapr.py
git commit -m "feat(service-kit): RASK_DAPR_ENABLED + DAPR_HTTP_PORT settings"
```

---

### Task 2: service-kit DaprClient factory + lifespan wiring + DI

**Files:**
- Modify: `packages/service-kit/pyproject.toml` (dependencies)
- Modify: `packages/service-kit/src/service_kit/__init__.py`
- Test: `packages/service-kit/tests/test_dapr.py` (extend)

**Interfaces:**
- Consumes: `Settings.dapr_enabled`, `Settings.dapr_http_port` (Task 1).
- Produces:
  - `build_dapr_client(settings: Settings) -> "DaprClient | None"` — returns `None` when `dapr_enabled` is false; otherwise a `dapr.clients.DaprClient` pointed at `127.0.0.1:<dapr_http_port>`.
  - `get_dapr(request: Request) -> "DaprClient | None"` and `DaprClientDep = Annotated[..., Depends(get_dapr)]`.
  - `make_service_app` sets `app.state.dapr` (the client or `None`) and closes it on shutdown.

- [ ] **Step 1: Add the dapr dependency**

In `packages/service-kit/pyproject.toml`, add `"dapr"` to the `[project] dependencies` list (resolve the exact version with `uv add --package service-kit dapr` in Step 5; keep the lazy import so it's only needed when enabled).

- [ ] **Step 2: Write the failing tests**

Append to `packages/service-kit/tests/test_dapr.py`:

```python
def test_build_dapr_client_none_when_disabled() -> None:
    from service_kit import build_dapr_client

    assert build_dapr_client(_settings()) is None


def test_build_dapr_client_builds_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import service_kit

    captured: dict[str, str] = {}

    class FakeDaprClient:
        def __init__(self, address: str) -> None:
            captured["address"] = address

        def close(self) -> None:
            captured["closed"] = "yes"

    # Patch the lazy import target so no real dapr package / sidecar is needed.
    monkeypatch.setattr(service_kit, "_import_dapr_client", lambda: FakeDaprClient, raising=True)

    client = service_kit.build_dapr_client(_settings(RASK_DAPR_ENABLED="true", DAPR_HTTP_PORT="3500"))
    assert isinstance(client, FakeDaprClient)
    assert captured["address"] == "http://127.0.0.1:3500"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run --all-packages pytest packages/service-kit/tests/test_dapr.py -v --no-cov`
Expected: FAIL — `ImportError: cannot import name 'build_dapr_client'`.

- [ ] **Step 4: Implement the factory + DI + lifespan wrapping**

In `packages/service-kit/src/service_kit/__init__.py`:

Add imports near the top (after the existing `from fastapi import ...` line):

```python
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, FastAPI, Request

if TYPE_CHECKING:
    from dapr.clients import DaprClient
```

(Replace the existing `from fastapi import APIRouter, FastAPI` import with the line above.)

Add the factory + DI helpers after `build_settings` (line 41):

```python
def _import_dapr_client() -> "type[DaprClient]":
    # Lazy import: the dapr SDK is only required when RASK_DAPR_ENABLED is true.
    from dapr.clients import DaprClient

    return DaprClient


def build_dapr_client(settings: Settings) -> "DaprClient | None":
    """Dapr SDK client at the local sidecar, or None when Dapr is disabled."""
    if not settings.dapr_enabled:
        return None
    dapr_client_cls = _import_dapr_client()
    return dapr_client_cls(f"http://127.0.0.1:{settings.dapr_http_port}")


def get_dapr(request: Request) -> "DaprClient | None":
    return request.app.state.dapr


DaprClientDep = Annotated["DaprClient | None", Depends(get_dapr)]
```

Then wrap the lifespan inside `make_service_app` so every app gets `app.state.dapr`. Replace the body from `lifespan_factory: LifespanFactory = ...` (line 76) through the `app = FastAPI(...)` block with:

```python
    base_factory: LifespanFactory = lifespan if lifespan is not None else default_lifespan

    def lifespan_factory(s: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
        base = base_factory(s)

        @asynccontextmanager
        async def wrapped(app: FastAPI) -> AsyncIterator[None]:
            app.state.dapr = build_dapr_client(s)
            try:
                async with base(app):
                    yield
            finally:
                if app.state.dapr is not None:
                    app.state.dapr.close()

        return wrapped

    app = FastAPI(
        title=title,
        version="0.1.0",
        lifespan=lifespan_factory(settings),
        docs_url=f"{settings.api_prefix}/docs",
        redoc_url=f"{settings.api_prefix}/redoc",
        openapi_url=f"{settings.api_prefix}/openapi.json",
    )
```

- [ ] **Step 5: Resolve the dapr pin, then run tests**

Run: `uv add --package service-kit dapr`
Then: `uv run --all-packages pytest packages/service-kit/tests/test_dapr.py -v --no-cov`
Expected: PASS (4 passed). The "builds when enabled" test uses a fake, so no sidecar is contacted.

- [ ] **Step 6: Smoke-check an app still builds with Dapr off**

Run: `uv run --all-packages pytest components/services/volumes_api/tests -v --no-cov`
Expected: PASS — `make_service_app` wrapping didn't break the default lifespan (Dapr off ⇒ `app.state.dapr is None`).

- [ ] **Step 7: Commit**

```bash
git add packages/service-kit/pyproject.toml packages/service-kit/src/service_kit/__init__.py packages/service-kit/tests/test_dapr.py uv.lock
git commit -m "feat(service-kit): build_dapr_client + DaprClientDep + lifespan wiring (gated)"
```

---

### Task 3: Gateway — Dapr service invocation with httpx fallback

**Files:**
- Modify: `components/services/gateway/src/gateway/__init__.py`
- Test: `components/services/gateway/tests/test_routing.py` (create)

**Interfaces:**
- Consumes: `RASK_DAPR_ENABLED`, `DAPR_HTTP_PORT` from env (gateway reads env directly; it is not a `make_service_app` service).
- Produces:
  - `_routes() -> list[tuple[str, str, str]]` — `(path_prefix, app_id, fallback_url)`.
  - `_pick_route(path, routes) -> tuple[str, str] | None` — `(app_id, fallback_url)`.
  - `_target_base(app_id, fallback_url) -> str` — sidecar invoke base when enabled, else `fallback_url`.

- [ ] **Step 1: Write the failing tests**

Create `components/services/gateway/tests/test_routing.py`:

```python
"""gateway routing — Dapr invoke base vs httpx fallback (no network)."""

import importlib

import pytest


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import gateway

    return importlib.reload(gateway)


def test_routes_map_prefix_to_appid(gw, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = gw._routes()
    picked = gw._pick_route("/api/search/q", routes)
    assert picked is not None
    app_id, fallback = picked
    assert app_id == "search-api"
    assert fallback.endswith(":8802")
    # catch-all → core-api
    assert gw._pick_route("/api/batches/", routes)[0] == "core-api"


def test_target_base_uses_sidecar_when_enabled(gw, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    base = gw._target_base("core-api", "http://127.0.0.1:8801")
    assert base == "http://127.0.0.1:3500/v1.0/invoke/core-api/method"


def test_target_base_falls_back_when_disabled(gw, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_DAPR_ENABLED", "false")
    base = gw._target_base("core-api", "http://127.0.0.1:8801")
    assert base == "http://127.0.0.1:8801"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --all-packages pytest components/services/gateway/tests/test_routing.py -v --no-cov`
Expected: FAIL — `AttributeError: module 'gateway' has no attribute '_pick_route'`.

- [ ] **Step 3: Rewrite routing to carry app-id + invoke base**

In `components/services/gateway/src/gateway/__init__.py`, replace `_routes`, `_pick_upstream`, and `_distinct_upstreams` (lines 32–64) with:

```python
def _routes() -> list[tuple[str, str, str]]:
    # (path-prefix, dapr app-id, httpx fallback URL). Mirror RASK_API_PREFIX so
    # routing lines up with where endpoints mount. load_dotenv() so the gateway
    # sees the same .env config the backends do.
    load_dotenv()
    prefix = os.environ.get("RASK_API_PREFIX", "/api/v1").rstrip("/")
    core = ("core-api", os.environ.get("RASK_CORE_API_URL", "http://127.0.0.1:8801"))
    search = ("search-api", os.environ.get("RASK_SEARCH_API_URL", "http://127.0.0.1:8802"))
    volumes = ("volumes-api", os.environ.get("RASK_VOLUMES_API_URL", "http://127.0.0.1:8803"))
    ray = ("ray-api", os.environ.get("RASK_RAY_API_URL", "http://127.0.0.1:8804"))
    orch = ("orchestrator", os.environ.get("RASK_ORCH_API_URL", "http://127.0.0.1:8810"))
    # longest / most-specific prefixes first; the prefix itself is the catch-all
    return [
        (f"{prefix}/search", *search),
        (f"{prefix}/volumes", *volumes),
        (f"{prefix}/ray", *ray),
        (f"{prefix}/orchestrator", *orch),
        ("/api/serve", *ray),
        (prefix, *core),
        ("/api", *core),
    ]


def _pick_route(path: str, routes: list[tuple[str, str, str]]) -> tuple[str, str] | None:
    for prefix, app_id, fallback in routes:
        if path == prefix or path.startswith(prefix + "/"):
            return app_id, fallback
    return None


def _dapr_enabled() -> bool:
    return os.environ.get("RASK_DAPR_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _target_base(app_id: str, fallback_url: str) -> str:
    """Invoke base for an app: the local Dapr sidecar when enabled, else the
    direct httpx upstream URL. Append the request path to this to get the URL."""
    if _dapr_enabled():
        port = os.environ.get("DAPR_HTTP_PORT", "3500")
        return f"http://127.0.0.1:{port}/v1.0/invoke/{app_id}/method"
    return fallback_url


def _distinct_targets(routes: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """Unique (app_id, fallback_url) pairs, first-seen order (for openapi merge)."""
    seen: dict[str, str] = {}
    for _, app_id, fallback in routes:
        seen.setdefault(app_id, fallback)
    return list(seen.items())
```

- [ ] **Step 4: Update the openapi merge to use targets**

Replace `_merged_openapi` (lines 67–88) signature + loop so it builds each base via `_target_base`:

```python
async def _merged_openapi(client: httpx.AsyncClient, prefix: str, targets: list[tuple[str, str]]) -> dict:
    """Fetch each backend's OpenAPI and merge into one spec so the gateway's
    /docs shows every service's endpoints. Unreachable backends are skipped."""
    merged: dict = {
        "openapi": "3.1.0",
        "info": {"title": "rask API (gateway)", "version": "0.1.0"},
        "paths": {},
        "components": {"schemas": {}},
    }
    for app_id, fallback in targets:
        base = _target_base(app_id, fallback)
        try:
            resp = await client.get(f"{base}{prefix}/openapi.json", timeout=10.0)
            resp.raise_for_status()
            spec = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning(f"openapi fetch failed for {app_id}: {exc}")
            continue
        merged["openapi"] = spec.get("openapi", merged["openapi"])
        merged["paths"].update(spec.get("paths", {}))
        merged["components"]["schemas"].update(spec.get("components", {}).get("schemas", {}))
    return merged
```

- [ ] **Step 5: Update the proxy handler to use the new helpers**

In the `lifespan` (line 96–97) replace the logging loop:

```python
    for prefix, app_id, fallback in app.state.routes:
        log.info(f"route {prefix} -> {app_id} ({_target_base(app_id, fallback)})")
```

In `proxy` (lines 114–123) replace the openapi call and upstream selection:

```python
    if request.url.path == f"{prefix}/openapi.json":
        return JSONResponse(await _merged_openapi(client, prefix, _distinct_targets(request.app.state.routes)))
    if request.url.path == f"{prefix}/docs":
        return get_swagger_ui_html(openapi_url=f"{prefix}/openapi.json", title="rask API (gateway)")

    picked = _pick_route(request.url.path, request.app.state.routes)
    if picked is None:
        raise HTTPException(status_code=404, detail=f"no upstream for {request.url.path}")
    app_id, fallback = picked
    base = _target_base(app_id, fallback)

    url = httpx.URL(f"{base}{request.url.path}").copy_with(query=request.url.query.encode("utf-8") or None)
```

Leave the rest of `proxy` (headers, `build_request`, `send(stream=True)`, the 502 handler, `StreamingResponse`) unchanged — it already works against any base URL.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run --all-packages pytest components/services/gateway/tests/test_routing.py -v --no-cov`
Expected: PASS (3 passed).

- [ ] **Step 7: Commit**

```bash
git add components/services/gateway/src/gateway/__init__.py components/services/gateway/tests/test_routing.py
git commit -m "feat(gateway): route via Dapr service invocation, httpx env-URL fallback"
```

---

### Task 4: Chart — sidecar injection + config

**Files:**
- Modify: `chart/values.yaml`
- Modify: `chart/templates/fleet.yaml`

**Interfaces:**
- Consumes: `.Values.services` map (per-service `port`), `RASK_DAPR_ENABLED` from `.Values.config`.
- Produces: pod-template `dapr.io/*` annotations on each fleet Deployment when `.Values.dapr.sidecars`.

- [ ] **Step 1: Add the dapr block + config flag to values**

In `chart/values.yaml`, add `RASK_DAPR_ENABLED: "true"` to the `config:` map (alongside the other `RASK_*` keys), and add a top-level block:

```yaml
# Dapr sidecar injection on the fleet (the control-plane subchart is dapr.enabled
# above). Off => plain pods + the gateway falls back to httpx env-URLs.
dapr:
  sidecars: true
  logLevel: "info"
```

- [ ] **Step 2: Inspect the fleet pod template**

Run: `sed -n '1,40p' chart/templates/fleet.yaml`
Expected: see the `spec.template.metadata` block (where `checksum/config` or labels live) and the ranged `$name`/`$svc` variables. Note the exact indentation of `template.metadata`.

- [ ] **Step 3: Add the annotations**

In `chart/templates/fleet.yaml`, under each Deployment's `spec.template.metadata`, add an `annotations:` block (create it if absent) gated on the toggle. Using the range vars `$root`, `$name`, `$svc`:

```yaml
      annotations:
        {{- if $root.Values.dapr.sidecars }}
        dapr.io/enabled: "true"
        dapr.io/app-id: {{ $name | quote }}
        dapr.io/app-port: {{ $svc.port | quote }}
        dapr.io/log-level: {{ $root.Values.dapr.logLevel | quote }}
        {{- end }}
```

(If a `template.metadata.annotations` block already exists, append the four `dapr.io/*` lines inside the same `{{- if }}` guard rather than adding a second `annotations:` key.)

- [ ] **Step 4: Render and verify the annotations appear**

Run:
```bash
helm template rask ./chart --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  | grep -E 'dapr.io/(enabled|app-id|app-port)' | sort -u
```
Expected: `dapr.io/app-id` lines for core-api, search-api, volumes-api, ray-api, orchestrator, gateway, plus `dapr.io/enabled: "true"` and the app-port values.

- [ ] **Step 5: Verify the toggle off removes them**

Run:
```bash
helm template rask ./chart --set dapr.sidecars=false --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  | grep -c 'dapr.io/enabled' || echo 0
```
Expected: `0`.

- [ ] **Step 6: Lint + commit**

```bash
helm lint ./chart
git add chart/values.yaml chart/templates/fleet.yaml
git commit -m "feat(chart): inject Dapr sidecars on the fleet + RASK_DAPR_ENABLED"
```

---

### Task 5: Live deploy + verify (k3s)

**Files:** none (operational). Requires sudo + the running k3s cluster.

**Interfaces:**
- Consumes: everything from Tasks 1–4.

- [ ] **Step 1: Rebuild + import the affected images**

The six backend images changed (service-kit + gateway). Run:
```bash
for s in gateway core-api search-api volumes-api ray-api orchestrator; do
  docker buildx build -f .docker/$s.dockerfile -t $s:dev --load . || exit 1
  docker save $s:dev | sudo k3s ctr images import -
done
```
Expected: each builds and imports cleanly.

- [ ] **Step 2: Upgrade the release**

Run: `make k3s-up`
Expected: `UP … EXIT 0`; gateway rollout completes.

- [ ] **Step 3: Verify sidecars injected (pods 2/2)**

Run:
```bash
export KUBECONFIG=$HOME/.kube/config
kubectl get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.annotations.dapr\.io/sidecar-injected}{"\n"}{end}' | grep -E 'core-api|gateway|search|volumes|ray-api|orchestrator'
kubectl get pods | grep -E 'core-api|gateway' 
```
Expected: fleet pods show `2/2` (app + `daprd`) and `dapr.io/sidecar-injected=true`.

- [ ] **Step 4: Verify API works through the gateway via invoke**

Run:
```bash
for p in /api/health /api/batches/ /api/ray/health; do
  printf "%-20s %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1$p)"
done
```
Expected: `200` for each (gateway → Dapr invoke → backend).

- [ ] **Step 5: Confirm it's actually using Dapr (not the fallback)**

Run: `kubectl logs deploy/rask-gateway -c gateway | grep -E 'route .* -> ' | head`
Expected: log lines show `... -> core-api (http://127.0.0.1:3500/v1.0/invoke/core-api/method)` etc. — proving the sidecar invoke base is in use.

- [ ] **Step 6: Record verification**

No commit needed (operational). Note the results in the PR/branch summary.

---

## Self-Review

**Spec coverage:**
- Sidecar injection (spec §1) → Task 4. ✅
- Shared DaprClient on app.state + DaprClientDep + RASK_DAPR_ENABLED gating (spec §2) → Tasks 1, 2. ✅
- Gateway → Dapr service invocation with fallback + openapi merge through invoke (spec §3) → Task 3. ✅
- Dependencies & images (spec §4) → Task 2 (dep), Task 5 (images). ✅
- Chart config RASK_DAPR_ENABLED + dapr block (spec §5) → Task 4. ✅
- Testing (spec §Testing) → unit tests in Tasks 1–3, live verify in Task 5. ✅
- Non-goals (orchestrator pub/sub, state, frontends, OTel) → not in any task. ✅

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step shows full code. The only deferred value is the exact `dapr` version, resolved by `uv add` in Task 2 Step 5 (explicitly, not a placeholder).

**Type consistency:** `build_dapr_client(settings) -> DaprClient | None` used consistently (Tasks 1→2). Gateway `_routes` 3-tuples / `_pick_route` / `_target_base` / `_distinct_targets` signatures match across Task 3 steps and the tests. `app.state.dapr` set in service-kit, read by `get_dapr`. `RASK_DAPR_ENABLED`/`DAPR_HTTP_PORT` names identical in config, gateway, chart.
