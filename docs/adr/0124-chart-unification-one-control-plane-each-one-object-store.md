# 0124. Chart unification: one control plane each, one object store, every hook pod labelled (P4, 2026-07-24)

Source: `docs/architecture/lance-ns-merge.md:325-354` (at `44b354f3`) (phase P4, "Chart unification", in the merge plan authored 2026-07-24) and risk 4 at `:422`. Chart comments cite these as "P4" or "lance-ns-merge P4".

## Context

Both repositories shipped a Helm chart with overlapping infrastructure (NATS, Dapr, OpenFGA, CNPG, an object
store, observability) and overlapping object names under one release. Two control planes for one concern, two
stores for one lakehouse, or two templates rendering one object name are each a silent failure mode.

## Decision

- **One control plane each.** Every shared subchart appears once. rask's dependency versions are kept; lance-ns
  **values** win where richer (credentialed, NetworkPolicy'd NATS; OpenFGA on `datastore.engine: postgres`
  with a migrate hook, replacing rask's memory toggle). lance-ns does not reinstall CRDs rask already carries.
- **rask's chart is the base.** lance-ns templates are grafted in; its nginx `gateway.yaml` is deleted
  ([0117](0117-nginx-is-retired-and-the-fastapi-gateway-is-the-in-cluster.md)).
- **One object store, one identity, with a keep-PVC posture**: the store's volumes survive pod rolls and
  `helm uninstall`.
- **Every Job/CronJob pod template carries an explicit component label.** Default-deny NetworkPolicies select
  by component, so a hook pod without one is invisible to every allow rule (the netpol landmine, audit
  2026-07-24).
- **Render-time collision guard.** No object name may render from two templates under any values combination.
- **No hostPath ships.** The media corpus resolves to a PVC or an object-store-backed corpus before k3s or
  prod.

## Consequences

- The subchart dedupe was lossless: the five subcharts lance-ns declared were already in rask at identical
  versions and repositories (`chart/Chart.yaml:26-34`); OpenFGA runs `engine: postgres`
  (`chart/values.yaml:3397`).
- The one store is a MinIO StatefulSet, not the plan's RustFS operator Tenant (LH-133, XC-075): RustFS gates
  STS `AssumeRole` to root. A StatefulSet's `volumeClaimTemplates` keeps the keep-PVC guarantee by a different
  mechanism (`chart/templates/minio.yaml:1-18`, `chart/values.yaml:2291-2300`).
- `scripts/prod_render_check.sh:197-211` fails the render on a Job/CronJob pod template with no component
  label.
- The collision guard is structural: `chart/templates/fleet.yaml:3-10` excludes the lakehouse services by
  name so no service renders from two templates.
- The corpus defaults to `emptyDir`, with `pvc` for prod and `hostPath` opt-in only
  (`chart/values.yaml:2073`, refused otherwise by `chart/templates/explorer.yaml:10-14`).
