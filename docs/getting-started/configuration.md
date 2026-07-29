# Configuration

rask is configured through environment variables, read by each service's
`Settings` (pydantic-settings, via `service-kit`) from the process environment
and a local `.env` file. The `.env` is git-ignored — it holds storage
credentials and is never committed.

## Storage (HCP / S3)

Production storage is a Hitachi Content Platform (HCP) S3 endpoint; local dev can
point at MinIO instead.

| Variable | Purpose |
|---|---|
| `HCP_ENDPOINT` | S3 endpoint URL (e.g. `https://dev-ai.hcp.ra-dev.int`). |
| `HCP_USERNAME` / `HCP_PASSWORD` | Credentials; the `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` pair is **derived** from these. |
| `HCP_INSECURE` | Skip TLS verification (HCP serves a self-signed cert). |
| `AWS_REGION` | S3 region (default `us-east-1`). |

!!! warning "Self-signed certificates"
    HCP presents a self-signed certificate. boto3 honours `HCP_INSECURE` with
    `verify=False`; LanceDB's Rust S3 client needs `allow_invalid_certificates`
    in its storage options. Both are wired up from `HCP_INSECURE` — without them
    you get `SSLError` (boto3) or `error sending request` (Lance).

## Buckets & search

| Variable | Default | Purpose |
|---|---|---|
| `RASK_CACHE_BUCKET` | `images-batch` | Input image cache. |
| `RASK_OUTPUT_BUCKET` | `images-batch-alto` | ALTO XML output. |
| `RASK_SEARCH_BUCKET` | `images-batch-search` | Lance search/catalog tables. |
| `RASK_LINES_TABLE` | `lines` | Full-text line index. |
| `RASK_CATALOG_TABLE` | `archive_catalog` | Archival catalog index. |

## Gateway

The gateway (`:8888`) is the frontend's single proxy target. Its upstream
service URLs are overridable via:

| Variable | Default | Upstream |
|---|---|---|
| `RASK_COMPUTE_URL` | `http://127.0.0.1:8804` | compute (`/api/ray`, `/api/serve`) |
| `RASK_CONTROLPLANE_URL` | `http://127.0.0.1:8820` | controlplane (`/api/projects`) |
| `RASK_CATALOG_API_URL` | `http://127.0.0.1:2333` | lance catalog (`/api/catalog`) |
| `RASK_LINEAGE_API_URL` | `http://127.0.0.1:8000` | lineage (`/api/lineage`) |
| `RASK_MEDALLION_API_URL` | `http://127.0.0.1:8002` | medallion producer (`/api/produce`, `/api/ingest-iiif`, `/api/train`) |
| `RASK_MEDIA_VIEWER_URL` | `http://127.0.0.1:8101` | media viewer (`/api/media`, incl. the objects browser) |
| `RASK_MEDIA_SEARCH_URL` | `http://127.0.0.1:8102` | media search (`/api/media/search`) |
| `RASK_MEDIA_ANNOTATOR_URL` | `http://127.0.0.1:8103` | annotator (`/api/media/annotations`) |

There is **no `/api` catch-all** since the R6/R20 wave — an unmatched `/api/*`
404s at the gateway. (The core-api/search-api/volumes-api upstream vars died
with their services.)

## The ray service

| Variable | Default | Purpose |
|---|---|---|
| `RASK_API_PREFIX` | `/api/v1` | Route prefix; the Vite proxy assumes `/api`. |
| `RASK_CORS_ORIGINS` | `[]` | Allowed CORS origins. |
| `RAY_DASHBOARD_URL` | `http://localhost:8265` | The Ray dashboard the service introspects/proxies. |

Changes to `.env` are read once at service startup — restart the relevant
service to pick them up.
