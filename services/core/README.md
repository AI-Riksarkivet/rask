# core — the transitional domain husk (P7a)

Package `core`. Since the P7a compute-plane cutover (`docs/architecture/lance-ns-merge.md` P7) this is a
**transitional husk**: health + the EAD catalog search over the LanceDB `archive_catalog` table. The
batches table, Alembic lineage, orchestrator loop, chunk submission, and S3-sync are **deleted** —
ingestion is the medallion producer's `POST /ingest-iiif` and HTR runs as event-driven cascade compute.

Composed by the `core_api` thin entrypoint (`:8801`); `main.py` is the app factory tests build. Retires
with the R6/R20 media wave, when lance `search` serves a catalog-governed EAD table.

```
src/core/
├── api/v1/endpoints/  # health, catalog (+ spa static mount)
├── lifespan.py        # httpx + the Lance catalog table
├── schemas/           # catalog + health response models
└── services/discover/ # the EAD catalog FTS service
```

Endpoints (under `RASK_API_PREFIX`, default `/api/v1`):

```
GET /api/health
GET /api/catalog/search?q=…
GET /api/catalog/search/stats
```
