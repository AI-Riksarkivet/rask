# Services

`services/` holds every runnable Python service — the rask **fleet** (gateway +
ray + controlplane), the lance **lakehouse** plane (catalog, lineage, medallion,
compaction), and the lance **media** plane (viewer, search, annotator).

The old monolithic `viewer` service was dissolved (June 2026) into a fleet of
per-domain services; the batches/orchestrator plane died at P7a (2026-07-27,
compute-plane cutover); and the R6/R20 media wave (2026-07-28) retired
`core-api`, `search-api`, and `volumes-api` — their still-needed capabilities
serve from the media plane. History: `docs/architecture/microservices.md` and
`docs/architecture/lance-ns-merge.md`.

## Gateway — `services/gateway`

App `gateway:app`, port **:8888**. The frontend's single proxy target and the
only external-facing service. Receives `/api/*` and routes by longest-prefix to
the services below; owns no state and no DB. **There is no `/api` catch-all** —
an unmatched `/api/*` 404s with `no upstream`. Upstream URLs are env-overridable:

| Env var | Default | Upstream |
|---|---|---|
| `RASK_RAY_URL` | `http://127.0.0.1:8804` | ray (`/api/ray`, `/api/serve`) |
| `RASK_CONTROLPLANE_URL` | `http://127.0.0.1:8820` | controlplane (`/api/projects`) |
| `RASK_CATALOG_API_URL` | `http://127.0.0.1:2333` | lance catalog (`/api/catalog`) |
| `RASK_LINEAGE_API_URL` | `http://127.0.0.1:8000` | lineage (`/api/lineage`) |
| `RASK_MEDALLION_API_URL` | `http://127.0.0.1:8002` | medallion producer (`/api/produce`, `/api/ingest-iiif`, `/api/train`) |
| `RASK_MEDIA_VIEWER_URL` | `http://127.0.0.1:8101` | media viewer (`/api/media`) |
| `RASK_MEDIA_SEARCH_URL` | `http://127.0.0.1:8102` | media search (`/api/media/search`) |
| `RASK_MEDIA_ANNOTATOR_URL` | `http://127.0.0.1:8103` | annotator (`/api/media/annotations`) |

## ray — `services/ray_api`

Port **:8804**. Ray dashboard introspection (`/api/ray/*`) + the
`/api/serve/*` proxy. Thin shell over `ray-kit`. No DB. Deps: `service-kit` +
`ray-kit` + `httpx`.

!!! note "Named `ray`, packaged `ray-api`"
    The k8s Deployment/Service, dapr app-id, image, and gateway row are all
    `ray` (R20 — the `-api` suffix died with the R6/R20 wave). The uv workspace
    member stays `ray-api` (import package `ray_api`) because a Python package
    named `ray` would shadow the PyPI `ray` that `ray-kit` depends on.

Endpoint groups:

| Group | Routes |
|---|---|
| health | `GET /health` (process liveness) |
| ray | `GET /ray/health`, `/ray/jobs`, `/ray/jobs/{id}/logs`, `/ray/cluster`, `/ray/actors`, `/ray/tasks`, `/ray/overview`, `/ray/logs` |
| serve proxy | `/api/serve/*` passthrough (mounted at root, outside `RASK_API_PREFIX`) |

## controlplane — `services/controlplane`

Port **:8820**. Project provisioning over the k8s API (`/api/projects`). See
`docs/architecture/lance-ns-merge.md` for its role in the merged estate.

## The media plane — `services/{viewer,search,annotator}`

Ports **:8101/:8102/:8103**, public under `/api/media/*` through the gateway.
The **viewer** additionally serves the **S3 object browser** ported from the
retired volumes-api (`/api/media/objects`, `/api/media/object`,
`/api/media/object/download` → viewer `/api/object*`) — the lakehouse zone's
storage browser backend. It reads the two fixed rask buckets via
`storage.s3_client` (env: `RASK_S3_ENDPOINT_URL` + `AWS_*`).

Retired capabilities and where they re-land (R6):

| Retired | Replacement |
|---|---|
| search-api lines FTS (`/api/v1/search`) | a catalog-governed `lines` Lance table served at `/api/media/search?dataset=lines&mode=fts` (re-lands with P7b gold) |
| core-api EAD catalog search (`/api/v1/catalog`) | a catalog-governed `archive_catalog` table behind `/api/media/search` (ingest job refit of `harvest_ead`) |
| volumes-api page/ALTO viewing | media-plane Blob-V2 viewing over the P7b datasets; ALTO becomes a P7c `exporter` projection |
| volumes-api `/objects` S3 browser | **ported now** into the viewer (above) |

## Endpoint summary

| Group | Service | Selected routes |
|---|---|---|
| health | gateway | `GET /healthz` (unproxied) |
| ray | ray | `GET /api/ray/health`, `/api/ray/jobs`, … + `/api/serve/*` proxy |
| projects | controlplane | `/api/projects/*` |
| lakehouse | catalog / lineage / medallion | `/api/catalog/*`, `/api/lineage/*`, `/api/produce`, `/api/ingest-iiif`, `/api/train` |
| media | viewer / search / annotator | `/api/media/*`, `/api/media/search`, `/api/media/annotations`, `/api/media/object*` |
