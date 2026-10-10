# 0118. common merges into service-kit (R19, 2026-07-27)

Source: `docs/architecture/lance-ns-merge.md:454` (at `44b354f3`) (owner ruling R19, 2026-07-27).

## Context

lance-ns brought `packages/common` (auth, FGA and audit middleware for its governed services); rask had
`service-kit` (the app factory, `Settings`, the injectable lifespan). Two platform libraries would give every
service two places to look for one concern.

## Decision

`common` merges INTO `service-kit`, now rather than later. There is one platform library, named
`service-kit`: its factory/Settings/lifespan skeleton is the base, and common's auth/FGA/audit middleware ports
in as the governed layer. Every `common` importer is rewritten and `packages/common` is deleted.

## Consequences

- `packages/` holds `lineage-kit`, `ray-cluster-env`, `ray-kit`, `service-kit`, `storage` and `validate`; there
  is no `common`.
- The governed layer lives at `service_kit.governed` (for example `service_kit.governed.fga`,
  `service_kit.governed.audit`, `service_kit.governed.oidc`, imported by
  `services/medallion/src/medallion/api/produce_auth.py:43-45`).
