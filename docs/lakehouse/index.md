---
title: Lakehouse Platform
description: Index of the lance-ns lakehouse documentation estate merged into rask.
---

# Lakehouse platform

The **lance-ns lakehouse** (Lance namespace catalog, medallion pipeline, lineage,
viewer/annotator surface) was merged into rask on the `feat/lance-ns-merge` branch.
Its documentation estate lives here. The canonical merge rulings are in
[Architecture → Lance-NS Merge](../architecture/lance-ns-merge.md).

## Design records

The load-bearing records, in reading order:

- [Architecture & Status](../ARCHITECTURE.md) — what exists, what state it is in
- [End-to-End Flow](../FLOW.md) — the implemented pipeline, in order
- [Data Contract](../DATA-CONTRACT.md) — what it is, how it is enforced
- [Decisions](../adr/README.md) — consolidated architecture decisions
- [Medallion Pipeline](../MEDALLION.md) — event-driven bronze → silver → gold
- [Lineage](../LINEAGE.md) — OpenLineage → Apache AGE
- [Authorization](../AUTHZ.md) — who can see and do what, per zone
- [DuckDB Access](../DUCKDB.md) — querying lance-ns tables from DuckDB
- [Ray Compute Seam](../RAY.md) / [Ray Train](../RAY-TRAIN.md) — real-cluster compute
- [Operators & Submit Seam](../OPERATORS.md) — what we adopt, what rask supplies
- [API Surface](../API.md) — the HTTP surface
- [rask Integration](../RASK-INTEGRATION.md) — the merge checklist

## Operations

- [Deploy](../DEPLOY.md) · [Durability](../DURABILITY.md) · [Resilience](../RESILIENCE.md) · [CNPG + AGE](../CNPG-AGE.md)
- Runbooks: [On-call](../runbooks/RUNBOOK-oncall.md) · [Backup & Restore](../runbooks/RUNBOOK-restore.md)

## API snapshots (OpenAPI)

Frozen OpenAPI snapshots of the merged services, kept next to the design records:

- [`catalog-openapi.json`](../catalog-openapi.json) — the catalog service surface
- [`lineage-openapi.json`](../lineage-openapi.json) — the lineage service surface

## Diagrams

Markdown companions are in the nav; the interactive HTML originals are served as-is:

- [System diagram](../system-diagram.md) ([interactive](../system-diagram.html))

## Reports

- [Coverage](../COVERAGE.md)

## Lance format & SDK reference

The vendored upstream Lance documentation (format, SDK, namespace spec) has its own
section: [Lance Reference](../lance/index.md).
