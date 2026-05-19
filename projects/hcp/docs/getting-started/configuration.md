# Configuration

All configuration is managed through environment variables. Place them in a `.env` file and the backend will load them automatically at startup.

!!! tip
    The `.env` file lives in the **project root** (one level above `backend/`). The backend reads it from there via its settings module.

## HCP Management API (MAPI)

These variables configure the connection to the HCP Management API used for administrative operations.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `HCP_HOST` | str | `""` | MAPI admin host. Auto-derived from `HCP_DOMAIN` as `admin.<domain>` if left empty. |
| `HCP_DOMAIN` | str | `""` | HCP domain (e.g., `hcp.example.com`). |
| `HCP_PORT` | int | `9090` | MAPI port. |
| `HCP_USERNAME` | str | `""` | HCP admin username. |
| `HCP_PASSWORD` | str | `""` | HCP admin password. |
| `HCP_AUTH_TYPE` | str | `"hcp"` | Auth type: `hcp` (base64 encoding) or `ad` (Active Directory). |
| `HCP_VERIFY_SSL` | bool | `False` | Verify SSL certificates when connecting to HCP. |
| `HCP_TIMEOUT` | int | `60` | Request timeout in seconds. |

## S3 Data Plane

These variables configure the S3-compatible data plane used for object storage operations.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `S3_ENDPOINT_URL` | str | `"https://s3.hcp.example.com"` | S3 endpoint URL. |
| `S3_REGION` | str | `"us-east-1"` | AWS region. HCP ignores this value, but aioboto3/botocore requires it to be set. |

!!! tip
    S3 credentials are derived from the MAPI credentials automatically. The base64-encoded `HCP_USERNAME` becomes the access key, and the MD5 hash of `HCP_PASSWORD` becomes the secret key. There is no need to configure S3 credentials separately.

## Storage Backend

These variables control which S3-compatible storage adapter the backend uses. Most deployments use the default `hcp` backend.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `STORAGE_BACKEND` | str | `"hcp"` | Storage adapter: `hcp`, `minio`, or `generic`. |
| `S3_ADDRESSING_STYLE` | str | `"auto"` | S3 addressing style: `auto`, `path`, or `virtual`. |
| `S3_ACCESS_KEY` | str | `""` | Direct S3 access key (MinIO/generic backends only). |
| `S3_SECRET_KEY` | str | `""` | Direct S3 secret key (MinIO/generic backends only). |
| `S3_VERIFY_SSL` | bool | `False` | Verify SSL certificates for S3 connections. |

!!! note
    When using the `hcp` backend, S3 credentials are derived from `HCP_USERNAME` and `HCP_PASSWORD` automatically. The `S3_ACCESS_KEY` and `S3_SECRET_KEY` variables are only needed for `minio` or `generic` backends.

## Redis Cache

Redis caching is optional. When `REDIS_URL` is empty, all caching is disabled and every request hits HCP directly.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REDIS_URL` | str | `""` | Redis connection URL (e.g., `redis://localhost:6379`). Empty disables caching. |
| `CACHE_DEFAULT_TTL` | int | `300` | Default cache TTL in seconds (5 min) -- used for MAPI listings. |
| `CACHE_STATS_TTL` | int | `60` | Stats cache TTL in seconds (1 min). |
| `CACHE_CONFIG_TTL` | int | `600` | Config cache TTL in seconds (10 min) -- used for security and permissions data. |
| `CACHE_S3_LIST_TTL` | int | `120` | S3 list operations cache TTL in seconds (2 min). |
| `CACHE_S3_META_TTL` | int | `300` | S3 metadata cache TTL in seconds (5 min). |
| `CACHE_QUERY_OBJECT_TTL` | int | `60` | Query object cache TTL in seconds (1 min). |
| `CACHE_QUERY_OPERATION_TTL` | int | `120` | Query operation cache TTL in seconds (2 min). |
| `CACHE_KEY_PREFIX` | str | `"hcp"` | Prefix for all Redis keys. Useful when sharing a Redis instance across services. |

## Authentication

These variables control JWT token generation and CORS policy for the API.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `API_SECRET_KEY` | str | `"change-me-in-production"` | Secret key used to sign JWT tokens. |
| `API_TOKEN_EXPIRE_MINUTES` | int | `480` | JWT token expiration time in minutes (default: 8 hours). |
| `CORS_ORIGINS` | str | `""` | Comma-separated list of allowed CORS origins. Empty allows all origins. |

!!! warning
    The default `API_SECRET_KEY` is **not secure**. You must change it to a strong, random value in any production or publicly accessible deployment. Generate one with:

    ```bash
    python -c "import secrets; print(secrets.token_urlsafe(64))"
    ```

## OpenTelemetry

The backend integrates OpenTelemetry for traces, metrics, and structured JSON logging. Configuration uses standard OTel environment variables.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `OTEL_SERVICE_NAME` | str | `"ra-hcp"` | Service name reported in traces and metrics. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | str | `""` | OTLP collector endpoint (e.g., `http://localhost:4318`). When empty, traces are printed to console and OTLP log/metric export is disabled. |

!!! tip
    When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, the backend exports traces, metrics, and logs via OTLP/HTTP to the configured collector (e.g., Grafana Alloy, Jaeger, or the OTel Collector). Without it, traces go to console and structured JSON logs go to stderr.

## Application

General application settings.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `ROOT_PATH` | str | `""` | Root path prefix when the API sits behind a reverse proxy (e.g., `/proxy/8000`). |

## Frontend

These variables configure the SvelteKit frontend. They are set as environment variables when starting the frontend server (not in `.env`).

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `BACKEND_URL` | str | `"http://127.0.0.1:8000"` | URL of the FastAPI backend. The frontend proxies all `/api/*` requests here. |
| `COOKIE_SECURE` | str | `""` | Set to `"true"` or `"false"` to override the Secure flag on session cookies. Defaults to `true` in production, `false` in dev. |

## Docker Publishing

These variables are used by the `make publish` targets to push container images to Docker Hub via the Dagger pipeline.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `DOCKER_USERNAME` | str | `""` | Docker Hub username. |
| `DOCKER_PASSWORD` | str | `""` | Docker Hub password or access token. |

## Example `.env` File

```bash
# HCP Connection
HCP_DOMAIN=hcp.example.com
HCP_PORT=9090
HCP_USERNAME=admin
HCP_PASSWORD=secretpassword
HCP_AUTH_TYPE=hcp
HCP_VERIFY_SSL=False

# S3
S3_ENDPOINT_URL=https://s3.hcp.example.com

# Redis (optional)
REDIS_URL=redis://localhost:6379

# Auth
API_SECRET_KEY=replace-with-a-strong-random-value

# Docker publishing (optional)
DOCKER_USERNAME=
DOCKER_PASSWORD=
```
