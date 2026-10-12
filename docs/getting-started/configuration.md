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
| `RASK_OIDC_ISSUER` / `RASK_OIDC_AUDIENCE` | The issuer (Dex in development) and audience the token must carry. |
| `RASK_FGA_ENABLED` | Authorize every request on OpenFGA (on in the chart). |
| `RASK_FGA_API_URL` / `RASK_FGA_STORE_ID` | The OpenFGA server and store. |
| `RASK_FGA_ROOT_OBJECT` | The estate's root object (default `warehouse:lance_catalog`). |
| `RASK_SA_ISSUER` / `RASK_SA_AUDIENCE` | The cluster's service-account issuer a service token is verified against. |

## The catalog

The catalog's own settings carry the `LANCE_` prefix (`services/catalog/src/catalog/core/config.py`), for example:

| Variable | Purpose |
|---|---|
| `LANCE_REST_IMPL` / `LANCE_REST_ROOT` | The namespace implementation and the object-store root it serves. |
| `LANCE_NS_DELIMITER` | The table-id delimiter (`$`). |
| `LANCE_LINEAGE_EMIT_ENABLED` | Announce every committed write on lineage (off in the code default, on in the chart). |
| `LANCE_LINEAGE_OUTBOX_URI` | Stage each lineage event in the object store before publishing it. |
| `LANCE_CONTROL_EMIT_ENABLED` | Broadcast the catalog's control-plane mutations on `catalog.control.v1`. |

## Gateway

The gateway (`:8888`) is the frontend's single proxy target (`services/gateway/src/gateway/config.py`). Its upstream
service URLs are overridable via:

| Variable | Default | Upstream |
|---|---|---|
| `RASK_CATALOG_API_URL` | `http://127.0.0.1:2333` | catalog (`/api/catalog`) |
| `RASK_LINEAGE_API_URL` | `http://127.0.0.1:8000` | lineage (`/api/lineage`) |
| `RASK_EXPLORER_VIEWER_URL` | `http://127.0.0.1:8101` | media viewer (`/api/explorer`, incl. the objects browser) |
| `RASK_EXPLORER_SEARCH_URL` | `http://127.0.0.1:8102` | media search (`/api/explorer/search`) |
| `RASK_EXPLORER_ANNOTATOR_URL` | `http://127.0.0.1:8103` | annotator (`/api/explorer/annotations`) |
| `RASK_FLOWS_URL` | `http://127.0.0.1:8840` | flows (`/api/flows`) |
| `RASK_NOTIFICATIONS_URL` | `http://127.0.0.1:8850` | notifications (`/api/notifications`) |
| `RASK_COMPUTE_URL` | `http://127.0.0.1:8804` | compute (`/api/ray`, `/api/serve`) |

There is **no `/api` catch-all** — an unmatched `/api/*` 404s at the gateway.

## Storage

The object store is S3-compatible: the MinIO fixture in development, the platform's S3 in production
(`minio.externalEndpoint`). Services reach it through `packages/storage` and the catalog's vended credentials; the
store's root key is held by the store alone, and each service presents a scoped identity.

Changes to the environment are read once at service startup — restart the relevant service to pick them up.
