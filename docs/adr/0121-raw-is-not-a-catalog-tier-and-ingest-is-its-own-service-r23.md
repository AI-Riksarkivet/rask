# 0121. Raw is not a catalog tier, and ingest is its own service (R23, R24, 2026-07-28)

Source: `docs/architecture/lance-ns-merge.md:458-459` (at `44b354f3`) (owner rulings R23 and R24, 2026-07-28).

## Context

The merged cascade modelled a "raw" tier, a raw-to-bronze stage runner and `/raw-arrival` language, as though
the external source were a governed dataset. External sources also come in many formats and places, not one.

## Decision

- **R23 — raw is not a catalog tier.** Raw is the EXTERNAL world (an image API, external object storage),
  something the platform consumes FROM. The governed medallion starts at bronze, the first Lance dataset the
  platform owns. The catalog has exactly three governed tiers: bronze, silver, gold. No raw table is ever
  registered; any code, topic, stage runner name, chart entry or doc modelling raw as a governed tier is a
  defect. Ingest always converts to Lance (media land as the bronze blob-v2 dataset, never passed through) and
  emits the OpenLineage event with the external source URI as the INPUT and the bronze dataset as the OUTPUT.
  The raw-to-bronze stage runner collapses into the bronze ingest head; the cascade is bronze → silver → gold.
- **R24 — ingest is its own service**, owning a pluggable source-adapter seam (one adapter per format/place),
  always converting to Lance at the bronze boundary and always emitting the boundary lineage event. The
  medallion-hosted ingest head is transitional.

## Consequences

- `services/ingest` exists with its adapter seam (`services/ingest/src/ingest/adapters.py`).
- The medallion producer's `POST /produce` and `POST /ingest-media` still write bronze too, so ingestion has
  more than one door today; the R24 extraction is not complete.
- `service_kit.schemas.storage.StorageRole` keeps `RAW` as a store role outside the governed tiers, with
  `GOVERNED_TIERS` exactly bronze/silver/gold (`packages/service-kit/src/service_kit/schemas/storage.py:21-45`).
