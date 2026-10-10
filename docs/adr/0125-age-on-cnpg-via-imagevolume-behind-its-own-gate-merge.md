# 0125. AGE on CNPG via ImageVolume, behind its own gate (merge decision 1, 2026-07-24)

Source: `docs/architecture/lance-ns-merge.md:48,82,332,560` (at `44b354f3`) (proposed decision 1 of the merge plan, 2026-07-24, restated with survey evidence and later settled).

## Context

The lineage graph runs on Apache AGE in Postgres. lance-ns ran it as a CNPG `Cluster` with the AGE extension
mounted from an image (`deploy/cnpg-age-cluster.yaml`); rask already shipped the cloudnative-pg operator.

## Decision

AGE runs on CNPG via an ImageVolume extension image: `chart/templates/age-cluster.yaml` (a CNPG `Cluster`
plus the extension image) replaces `age-postgres.yaml`. The survey strengthened it: the AGE cluster rides an
operator rask already ships, with no new operator. The caveat is the CSI-mount leg, which needs Kubernetes
1.33+ and must be verified on the target node before cutting over.

## Consequences

- The cutover has its own gate, separate from the operator's: `age.cnpgCluster.enabled` defaults `false`
  (`chart/values.yaml:3476-3477`), because it needs a built extension image
  (`.docker/cnpg-age-ext.dockerfile`), Kubernetes 1.33+ and CNPG >= 1.27. Until it is on, AGE is served by
  the `rask-age` StatefulSet, and the two paths are mutually exclusive (`CLAUDE.md`, State surface).
- `cnpg.enabled: true` installs the operator with nothing to reconcile; an enabled operator toggle is not
  evidence that the `Cluster` exists.
