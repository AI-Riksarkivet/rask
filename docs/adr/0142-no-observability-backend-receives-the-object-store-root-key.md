# 0142. No observability backend receives the object store's root key (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (LH-161 #209; same seam as XC-003 #107; R8 in ADR 0085; XC-118).

## Context

The `rask-greptimedb-standalone` StatefulSet takes `envFrom: rask-observability-s3`, a Secret ESO builds from
`minio.accessKey` and the `minio-secret-key` property (`chart/templates/external-secrets.yaml:166-190`): the object
store's root key pair. R8 puts everything downstream of the OTel Collector out of scope, and XC-118 replaces
GreptimeDB.

## Decision

- The rule is stated at the seam rask owns: the chart never hands any observability backend the store's root key
  pair. A backend gets a bucket-scoped credential, and the chart states that contract.
- LH-161 closes when no backend pod renders the root key. There is no GreptimeDB-specific storage work, and D7 is
  moot.

## Consequences

- The secrets rule holds whichever backend replaces GreptimeDB.
