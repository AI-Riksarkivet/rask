# Configuration

rask is configured through environment variables, read by the viewer's
`Settings` (pydantic-settings) from the process environment and a local `.env`
file. The `.env` is git-ignored — it holds storage credentials and is never
committed.

## Storage (HCP / S3)

Production storage is a Hitachi Content Platform (HCP) S3 endpoint; local dev can
point at MinIO instead.

| Variable | Purpose |
|---|---|
| `HCP_ENDPOINT` | S3 endpoint URL (e.g. `https://dev-ai.hcp.ra-dev.int`). |
| `HCP_USERNAME` / `HCP_PASSWORD` | Credentials; the `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` pair is **derived** from these. |
| `HCP_INSECURE` | Skip TLS verification (HCP serves a self-signed cert). |
| `AWS_DEFAULT_REGION` | S3 region (default `us-east-1`). |

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

## Viewer service

| Variable | Default | Purpose |
|---|---|---|
| `RASK_VIEWER_INPUT` / `RASK_VIEWER_OUTPUT` | — | Required `s3://` input/output prefixes. |
| `RASK_API_PREFIX` | `/api/v1` | Route prefix; the Vite proxy assumes it. |
| `RASK_CORS_ORIGINS` | `[]` | Allowed CORS origins. |
| `DATABASE_URL` | — | Postgres DSN; falls back to SQLite when unset. |
| `RAY_DASHBOARD_URL` | `http://localhost:8265` | Ray cluster the viewer submits to and proxies. |

## Orchestrator

| Variable | Default | Purpose |
|---|---|---|
| `RASK_ORCHESTRATOR_AUTOSTART` | `false` | Start the submission loop on boot. |
| `RASK_ORCHESTRATOR_INTERVAL_SECONDS` | `60` | Tick interval. |
| `RASK_HTR_PIPELINE` | `htr` | Pipeline submitted for the HTR lane. |
| `RASK_PREFETCH_PIPELINE` | `prefetch` | Prefetch lane pipeline (`none` to disable). |
| `RASK_HTR_MAX_INFLIGHT` | `0` | Max concurrent HTR jobs (`0` = unlimited). |

!!! tip "Targeting a shared cluster"
    Point `RAY_DASHBOARD_URL` at a remote KubeRay dashboard to submit jobs
    there. On clusters whose workers lack the runner's heavy dependencies, set
    `RASK_HTR_PIPELINE=htr_http` so the orchestrator submits the lightweight
    HTTP driver instead.

Changes to `.env` are read once at viewer startup — restart the service to pick
them up.
