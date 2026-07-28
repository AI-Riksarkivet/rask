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
- [System Sketch](../SYSTEM-SKETCH.md) — where we are, the holes, how we differ from Lakekeeper
- [End-to-End Flow](../FLOW.md) — the implemented pipeline, in order
- [Data Contract](../DATA-CONTRACT.md) — what it is, how it is enforced
- [Decisions](../DECISIONS.md) — consolidated architecture decisions
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
- [Event-driven pipeline](../event-driven-pipeline.md) ([interactive](../event-driven-pipeline.html))
- [Image pipeline](../image-pipeline-event-driven.md) ([interactive](../image-pipeline-event-driven.html))
- [K8s event-driven architecture (interactive only)](../k8s-event-driven-architecture.html)

## Design notes, reports & audits

- Design notes: [Annotation Projects](../DESIGN-annotation-projects.md) · [Interactive State](../DESIGN-interactive-state.md) · [UX Reactive Evidence](../GOAL-UX-REACTIVE-EVIDENCE.md)
- Reports: [Assessment 2026-07-15](../ASSESSMENT-2026-07-15.md) · [Catalog Bench 2026-07-22](../BENCH-2026-07-22.md) · [Coverage](../COVERAGE.md) · [Lineage Verification](../VERIFY-LINEAGE-OPENLINEAGE.md) · [Open Work](../OPEN-WORK.md)
- Frontend audits (with screenshots): [Audit index](../audits/README.md) · [MFE Composition](../audits/2026-07-26-mfe-composition.md) · [Routes & IA](../audits/2026-07-26-routes-and-ia.md) · [Svelte 5](../audits/2026-07-26-svelte5.md)

## Process artifacts (superpowers plans & specs)

Plans:
[Observability Stack](../superpowers/plans/2026-06-25-observability-stack.md) ·
[Openable Projects](../superpowers/plans/2026-06-29-openable-projects.md) ·
[Project Picker](../superpowers/plans/2026-06-29-rask-project-picker.md) ·
[Strip Default Spec II](../superpowers/plans/2026-06-29-strip-default-spec-ii.md) ·
[Project-First URLs](../superpowers/plans/2026-06-30-project-first-urls.md)

Specs:
[Observability Stack](../superpowers/specs/2026-06-25-observability-stack-design.md) ·
[Openable Projects](../superpowers/specs/2026-06-29-openable-projects-design.md) ·
[Project Picker](../superpowers/specs/2026-06-29-rask-project-picker-design.md) ·
[Strip Default Spec II](../superpowers/specs/2026-06-29-strip-default-spec-ii-design.md) ·
[Project-First URLs](../superpowers/specs/2026-06-30-project-first-urls-design.md)

## Lance format & SDK reference

The vendored upstream Lance documentation (format, SDK, namespace spec) has its own
section: [Lance Reference](../lance/index.md).
