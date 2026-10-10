# 0123. Storage is registered with a role (R28, 2026-07-28)

Source: `docs/architecture/lance-ns-merge.md:463` (at `44b354f3`) (owner ruling R28, 2026-07-28).

## Context

Three defects shared one root cause: the lakehouse storage page was reachable only by typing its URL; the
object browser's bucket set was a hardcoded two-value `Literal` (an import sink and an export sink) mirrored by
hand in the zone's TypeScript, while the governed tier storage was not selectable at all; and nothing declared
what a store was FOR, so the UI had to guess.

## Decision

Storage is REGISTERED with a ROLE, never hardcoded, and the tiers are storage too. The catalog owns a storage
registry with explicit roles; the storage browser DISCOVERS the list and groups by role (a tier view lists
Lance datasets with versions and row counts, a sink view lists objects); bucket names disappear from both the
viewer endpoint and the zone's mirror; Storage becomes a first-class lakehouse area in the nav.

## Consequences

- The role vocabulary that shipped is `raw`, `bronze`, `silver`, `gold`, `derived` and `observability`
  (`packages/service-kit/src/service_kit/schemas/storage.py:21-45`), not the ruling's `import-sink`,
  `export-sink`, `tier-store`, `observability`: the three governed tiers are separate roles (`GOVERNED_TIERS`),
  external raw is `raw` per [0121](0121-raw-is-not-a-catalog-tier-and-ingest-is-its-own-service-r23.md), and
  an export is `derived`.
- The catalog serves the registry at `GET /stores`
  (`services/catalog/src/catalog/api/v1/endpoints/stores.py:83`); the lakehouse zone renders it at
  `/lakehouse/catalog/stores` and `/lakehouse/catalog/storage/tiers`.
