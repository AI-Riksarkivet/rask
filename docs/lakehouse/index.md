---
title: Lakehouse Platform
description: "Index of rask's lakehouse documentation: the catalog, tier governance, lineage and authorization."
---

# Lakehouse platform

The lakehouse is the catalog (Lance Namespace REST over Lance tables), the bronze → silver → gold tier governance in
its write path, lineage, authorization, notifications and maintenance. Compute, ingest and orchestration are the
user's ([ADR 0154](../adr/0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md),
[ADR 0155](../adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)). The rulings behind it are the
[decision records](../adr/README.md).

## Design records

The load-bearing records, in reading order:

- [Architecture & Status](../ARCHITECTURE.md) — what exists, what state it is in
- [End-to-End Flow](../FLOW.md) — what happens to a write, in order
- [Data Contract](../DATA-CONTRACT.md) — what it is, how it is enforced
- [Decisions](../adr/README.md) — consolidated architecture decisions
- [Tier Governance](../TIER-GOVERNANCE.md) — bronze → silver → gold, enforced in the catalog's write path
- [Lineage](../LINEAGE.md) — OpenLineage → Apache AGE
- [Authorization](../AUTHZ.md) — who can see and do what, per zone
- [DuckDB Access](../DUCKDB.md) — querying governed tables from DuckDB
- [Operators](../OPERATORS.md) — what the platform runs, and rask's own procedures
- [API Surface](../API.md) — the HTTP surface

## Operations

- [Deployment](../architecture/deployment.md) · [Durability](../DURABILITY.md) · [Resilience](../RESILIENCE.md) · [CNPG + AGE](../CNPG-AGE.md)
- Runbooks: [On-call](../runbooks/RUNBOOK-oncall.md) · [Backup & Restore](../runbooks/RUNBOOK-restore.md)

## API snapshots (OpenAPI)

OpenAPI snapshots of the lakehouse services, checked by `make openapi-check`:

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
