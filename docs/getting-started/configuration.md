# Configuration

rask is configured through environment variables, read by each service's
`Settings` (pydantic-settings, via `service-kit`) from the process environment.
In-cluster, the chart renders them from `chart/values.yaml` into a ConfigMap, and every
credential reaches a pod from the secret store (the Dapr secret store, or an
ExternalSecret), never as a chart value. A local `.env` is git-ignored and never committed.

## Identity and authorization (every governed service)

Declared once in `packages/service-kit/src/service_kit/governed/settings.py`, one name per setting:

| Variable | Purpose |
|---|---|
| `RASK_OIDC_ENABLED` | Verify an OIDC bearer token on every request (on in the chart). |
| `RASK_OIDC_ISSUER` / `RASK_OIDC_AUDIENCE` | The issuer (Dex in development) and the lakehouse's own audience the token must carry (`auth.oidc.audience`, never a sign-in client's id). |
| `RASK_FGA_ENABLED` | Authorize every request on OpenFGA (on in the chart). |
| `RASK_FGA_API_URL` / `RASK_FGA_STORE_ID` | The OpenFGA server and store. |
| `RASK_FGA_ROOT_OBJECT` | The estate's root object (default `estate:rask`). |
| `RASK_SA_ISSUER` / `RASK_SA_AUDIENCE` | The cluster's service-account issuer a service token is verified against. |

## The catalog

The catalog's own settings carry the `LANCE_` prefix (`services/catalog/src/catalog/core/config.py`), for example:

| Variable | Purpose |
|---|---|
| `LANCE_REST_IMPL` / `LANCE_REST_ROOT` | The namespace implementation and the object-store root it serves. |
| `LANCE_LINEAGE_EMIT_ENABLED` | Announce every committed write on lineage (off in the code default, on in the chart). |
| `LANCE_LINEAGE_OUTBOX_URI` | Stage each lineage event in the object store before publishing it. |
| `LANCE_CONTROL_EMIT_ENABLED` | Broadcast the catalog's control-plane mutations on `catalog.control.v1`. |

## The edge

There is no rask process at the edge: the platform's Gateway (Kubernetes Gateway API) serves the chart's HTTPRoutes
(`chart/templates/route.yaml`, one rule per public prefix from `rask.apiRoutes` in `_helpers.tpl`), and each service
authenticates every request itself. The route table:

| Public prefix | Service | Path at the service |
|---|---|---|
| `/api/explorer/annotations` | annotator (the annotator fixture chart's own HTTPRoute) | `/api/annotations…` |
| `/api/catalog` | catalog | `/…` |
| `/api/lineage` | lineage | `/…` |
| `/api/notifications` | notifications | unchanged |
| `/api/ray`, `/api/serve` | compute (the compute fixture chart's own HTTPRoute) | unchanged |

The rewrite is Gateway API `URLRewrite` `ReplacePrefixMatch`. An unmatched `/api/*` is the Gateway's own 404.

| Value / variable | Meaning |
|---|---|
| `route.parentRefs` | The Gateway the routes attach to (required). In development, `traefik-gateway` in kube-system. |
| `route.hostnames` | Hostnames the platform's Gateway routes to this release. Required on a real deployment, which the render refuses with an empty list; empty, in development, the routes match every host the listener accepts. |
| `forwardedAllowIps` | The data plane's pod CIDR, trusted for `X-Forwarded-For` by every service the route publishes. |

Locally there is no router: `make dev-micro` runs the fleet only. The zones' SSR reads `RASK_EDGE_URL` (default
`http://localhost:8888`), and `make dev-frontends-k3s` puts the in-cluster Gateway there by port-forwarding k3s
Traefik's Service to `:8888`; `make dev-zone ZONE=<z>` runs one zone against its own mocks instead. The zones' Vite
dev `/api` proxies are per zone: `compute`/`studio`/`models` → `VIEWER_BACKEND` (`:8888`), `home`/`lakehouse` →
`LANCE_BACKEND` (`:8001`).

## Storage

The object store is S3-compatible: the RustFS fixture in development, the platform's S3 in production
(`objectStore.externalEndpoint`). Services reach it through `packages/storage` and the catalog's vended credentials; the
store's root key is held by the store alone, and each service presents a scoped identity.

Changes to the environment are read once at service startup — restart the relevant service to pick them up.
