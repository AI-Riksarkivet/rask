# Design: Strip platform to front-door only (Spec II)

**Date:** 2026-06-29
**Status:** Approved (brainstorm/decomposition) — pending implementation plan
**Scope:** rask `chart/` only (single repo). Builds on Spec I (per-project frontends/ingress) + the picker. The operator is unaffected — per-project provisioning is unchanged.

## Problem

The platform install (`default` namespace) still bundles the **legacy single-tenant stack**: the 6 domain backend services, the 6 `/default/*` domain frontends, a CNPG `Cluster`, a RustFS `Tenant`, a RayService, NATS, and a DB migration Job. Now that projects are provisioned per-namespace by the operator and reachable via the picker, the single-tenant stack is dead weight and a confusing second way in (the `/default/*` workspace). Remove it so the platform install is **front-door only**.

## Goal and non-goals

**Goal.** The `rask` platform install renders only the front door — the **home picker**, the **controlplane**, the **gateway** (the picker's `/api/projects` upstream), the **Kueue cluster cohort**, and all the **cluster operators** — and nothing single-tenant. The picker + per-project stacks (e.g. `demo.localhost`) keep working.

**Non-goals (this spec):**
- The `/default → /<domain>` URL cleanup (MFE base + `@rask/ui` shell nav rework). The per-project URLs keep the cosmetic `/default/` for now. → a separate follow-up (Spec III).
- Any operator / per-project chart change. The operators **must stay** (per-project stacks depend on cnpg/rustfs/kuberay/kueue/dapr).
- Removing OpenFGA (future control-plane identity — platform-level, keep).

## The key constraint (why this isn't a toggle flip)

The chart **conflates** "operator enabled" with "single-tenant workload enabled": `cnpg-cluster.yaml` is guarded by `.Values.cnpg.enabled`, which **also** gates the cnpg operator subchart (`Chart.yaml` `condition: cnpg.enabled`); same for `rustfs.enabled`. Naively disabling them would remove the operators the per-project stacks need, breaking provisioning. So the strip introduces a **separate `singleTenant` gate** for the default *workloads*, leaving the operator subcharts enabled.

## Design

Add `singleTenant.enabled: false` to `chart/values.yaml`. Gate the single-tenant workload templates on it (operators stay via their own `.enabled`):

| Template | New guard | Effect |
|---|---|---|
| `cnpg-cluster.yaml` | `and .Values.cnpg.enabled .Values.singleTenant.enabled` | default Postgres `Cluster` off; **cnpg operator stays** |
| `rustfs-tenant.yaml` | `and .Values.rustfs.enabled .Values.singleTenant.enabled` | default object store off; **rustfs operator stays** |
| `rayservice.yaml` | `and .Values.ray.enabled .Values.singleTenant.enabled` | default RayService off; **kuberay operator stays** |
| `migration-job.yaml` | `and .Values.migrations.enabled .Values.singleTenant.enabled` | default DB migration off |
| `fleet.yaml` (per-service in the range) | render if `eq $name "gateway"` **or** `singleTenant.enabled` | keeps **gateway**, drops core/search/volumes/ray-api/orchestrator |
| `frontends.yaml` (per-app in the range) | render if `.catchAll` **or** `singleTenant.enabled` | keeps **home** (picker), drops the 6 domain MFEs |
| `ingress.yaml` (the `/default/<domain>` loop) | add `and (not .catchAll) $.Values.singleTenant.enabled` | keeps `/api`→gateway and `/`→home; drops `/default/*` routes |

Also set `nats.enabled: false` (the platform NATS subchart served the single-tenant default; per-project namespaces render their own NATS via the operator chart; the front-door picker/controlplane/gateway don't use NATS).

**Untouched (kept):** the operator subcharts `cnpg/rustfs/kuberay/kueue/dapr/openfga` (their `.enabled` stays true), `kueue-queues.yaml` (the cluster cohort per-project LocalQueues borrow from), `controlplane.yaml`, `configmap`/`secrets`/`serviceaccount` (gateway + controlplane consume the configmap; the now-unused single-tenant secret values are harmless).

## What the front-door install renders after the strip

`home` (picker) + `controlplane` + `gateway` + `kueue-queues` cohort + the operator subcharts + an ingress with only `/api`→gateway and `/`→home. No core/search/volumes/ray-api/orchestrator, no `/default/*` frontends or routes, no default cnpg `Cluster` / rustfs `Tenant` / RayService / migration Job / platform NATS.

## Data flow (unchanged for the user)

`localhost/` → home picker → lists operator projects (controlplane reads `Project` CRs + per-project Ingress hosts) → click `demo` → `demo.localhost/default/overview` (per-project frontends + gateway, operator-provisioned). None of this depends on the single-tenant default stack.

## Testing

- **helm template:** front-door set renders (home, controlplane, gateway, kueue cohort, operator subchart CRDs/deployments); the single-tenant set is **absent** (no `rask-core-api`/`-overview`/… , no `cnpg Cluster`, no `rustfs Tenant`, no RayService, no migration Job, no `/default/` ingress paths); the cnpg/rustfs **operator** Deployments are still present (gated by their own `.enabled`).
- **live e2e (destructive — checkpoint before running):** `helm upgrade rask` with `singleTenant.enabled=false`; confirm the default backend pods (core-api/search/volumes/ray-api/orchestrator, default postgres/rustfs/ray/nats, default MFEs) are gone; the operators (cnpg/rustfs/kuberay/kueue/dapr controllers) remain Running; the picker still lists `demo`; `demo.localhost/default/overview` still returns 200 with demo's data; and provisioning still works (re-reconcile `demo` or apply a fresh `Project` and watch it reach Ready) — proving the operators survived.

## Follow-ups (not this spec)
- Spec III: drop the `/default` base (MFE base `/default/<domain>` → `/<domain>` + `@rask/ui` shell nav rework + operator ingress paths + controlplane entry path) so per-project URLs become `demo.localhost/overview`.
- Per-project generated credentials; durable JetStream; the bare-host `/`→`/overview` redirect (from Spec I follow-ups).
