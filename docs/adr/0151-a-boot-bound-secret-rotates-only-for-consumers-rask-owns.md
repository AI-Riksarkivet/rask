# 0151. A boot-bound secret rotates only for consumers rask owns in production (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (D8, XC-001's infra half #195). Reference: R1 in ADR 0085 (platform services are external in production).

## Context

ESO syncs `rask-infra-credentials` hourly, but these pods read their secret only at startup: MinIO
(`chart/templates/minio.yaml:130-150`), the Ray head (`_ray-cluster-config.tpl:176-181`), the Collector's
`DAPRSTATE_PASSWORD` (`otel-collector.yaml:614`), AGE (`age-postgres.yaml:141`) and OpenFGA's datastore URI
(`values.yaml:3398-3406`). Under R1, S3, OTel and ESO are platform-run in production, so the MinIO and Collector cases
exist in dev only.

## Decision

- A rotation mechanism is built only for consumers rask owns in production: AGE, OpenFGA and the Ray head.
- For those, a runbook step now (ALTER ROLE, then the OpenBao write, then a rollout restart), and a narrow CronJob
  later if production keeps them. MinIO and the Collector are dev stand-ins and get no machinery.

## Consequences

- No Reloader and no new controller.
- XC-001's infra half is narrowed to those three consumers.
