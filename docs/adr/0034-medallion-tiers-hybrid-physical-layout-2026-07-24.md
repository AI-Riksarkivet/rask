# 0034. Medallion tiers — hybrid physical layout (2026-07-24)

**Decision.** The medallion tiers get a **hybrid** physical layout per tenant: **raw/bronze/silver are
namespaces** (prefixes, `<work-root>/medallion/<stage>`) inside the tenant's **work** warehouse, while
**gold is a separate per-tenant SERVING warehouse** — a normal registry record created through
`POST /v1/warehouses` with the optional `"serving": "gold"` field (only `"gold"` is accepted for now;
absent = a work warehouse). `common/warehouse_registry.py` resolves the two classes independently:
`project_root` matches only work records, `project_gold_root` mirrors it matching only
`serving == "gold"` records (same lowest-id determinism, same TTL cache, partitioned by class — so
registering a gold warehouse can never hijack stage routing via the lowest-id rule). Behind
`MEDALLION_GOLD_WAREHOUSE_ENABLED` (chart `medallion.goldWarehouse`, default false, rendered ONLY onto
the terminal silver→gold stage runner), a tenant trigger's **target** root becomes the project's gold root when
one exists; absent gold warehouse or flag off → byte-identical work-warehouse behavior, and the
projectless path never retargets.

**Rationale.** Three forces pick the split point at gold, not "every stage its own bucket" or "all
prefixes":

- **Consumer blast-radius.** Gold is the tier external consumers read; raw/bronze may hold unvetted or
  PII-bearing data mid-scrub. A consumer read credential scoped to the gold **bucket** (bucket-level
  cred scoping is what object stores do well) can never traverse into raw/bronze the way a
  prefix-policy mistake on a shared bucket can.
- **Lifecycle/storage-class separation.** Serving data wants different retention, replication and
  storage-class policy than scratch stages; object stores apply those per bucket.
- **The recorded gold-sink intent.** The data-zone architecture note already records gold as an
  external SINK zone; a per-tenant serving warehouse is that intent expressed through the existing
  warehouse control plane instead of a new mechanism.

Interior stages stay prefixes because they share one producer/consumer (the stage runners), one lifecycle, and
one FGA cascade — separate buckets there would triple the per-tenant provisioning surface for no
isolation gain (the stage runners hold one credential either way).

**FGA.** The gold warehouse is a **normal `warehouse:` object** with the standard `project project:<p>`
parent tuple (seeded by warehouse-create like any other) — so project grants cascade into it naturally
and consumer read grants scope to `warehouse:<gold-id>` alone; no new FGA type, relation, or seed shape.
The `<p>-gold` namespace tuples from the per-tenant enablement seed (`seed_medallion_fga.sh <p> <zone-wh>`)
are unchanged: lineage/FGA identities are project-qualified names, not roots, and only the physical
target root moves.
