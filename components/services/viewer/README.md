# viewer

FastAPI backend for the rask viewer. Serves images and ALTO XML from any
`Source` (filesystem, MinIO, HCP) and optionally hosts the SvelteKit SPA.

## Endpoints

```
GET  /api/health
GET  /api/volumes/{vol}/pages              -> [{ key, hasAlto }]
GET  /api/volumes/{vol}/pages/{key}/image  -> image bytes
GET  /api/volumes/{vol}/pages/{key}/alto   -> ALTO XML (404 if absent)
GET  /                                     -> SvelteKit SPA (if frontend/build exists)
```

Volumes are simply key prefixes in the input bucket (e.g. `A0060198/`).
The viewer always navigates by **known** volume ID — there is no listing of
all volumes (an inventory mapping will replace that later).

## Run

Set `RASK_VIEWER_INPUT` and `RASK_VIEWER_OUTPUT` in `.env` (URIs or paths) and:

```bash
make viewer
```

Or directly:

```bash
RASK_VIEWER_INPUT=s3://images-batch RASK_VIEWER_OUTPUT=s3://images-batch-alto \
  uv run uvicorn viewer.app:app --host 0.0.0.0 --port 8888
```

For HCP, `.env` should also set `HCP_USERNAME`/`HCP_PASSWORD`/`HCP_ENDPOINT`.

## Frontend

The SvelteKit app lives in `frontend/` (workspace sibling). Dev workflow:

```bash
make viewer            # backend on :8888
make viewer-frontend   # vite dev server on :5173 with HMR, proxies /api to :8888
```

For production, build the SPA once and the FastAPI process serves both:

```bash
make viewer-frontend-build
make viewer
```
