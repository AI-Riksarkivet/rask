# open_controller — the control plane, the Project CR, and whether rask needs an operator

Working plan, **2026-09-02**. Unsettled work; **delete this file when the items below land or are
dropped.** `docs/` is for settled architecture only. Companion register:
`open_lakehouse_diff_left.md` (the governed-lakehouse backlog); this file owns only the control-plane
question so that register stays on the lakehouse.

## Why this exists

The owner asked whether https://github.com/lakekeeper/lakekeeper-operator — especially its
`docs/architecture.md` — gives rask a better way to view project / control plane / data plane. It
was read over a local clone (HEAD `27fa1e1`, 2026-06-10) by five readers (architecture doc, CRD
types, controller, chart + RBAC, tests), compared against rask's own control plane, and **eight
adversarial verifiers were run; all eight claims stand.** Every claim below cites the operator's
`file:line`; the per-agent record is workflow journal `wf_bcbf138a-efd`.

## 1. The verdict

**The operator does not change rask's split. It confirms it.** It ships exactly ONE CRD,
`Lakekeeper`, which models a *server instance* (`docs/architecture.md:35-38`,
`api/v1alpha1/lakekeeper_types.go:653-669`). Project, warehouse and namespace stay inside the
catalog's own `/management/*` API; their CRDs are "Planned" only (`architecture.md:15-27, 39-48`).
Its two design rules — catalog-assigned ids live in `.status`, never `.spec` (`.claude/CLAUDE.md:31`),
and the reconciler *validates, never provisions* Postgres/Vault/OpenFGA (`architecture.md:9`) — are
the shape rask already has: a project is a registry record minted with its FGA tuples in one door
(`catalog/services/projects.py`, `POST /v1/projects`), the only Kubernetes-side Project is a CR a
separate `rask-operator` would publish, and the chart must not ship that CRD without its controller
(`docs/DECISIONS.md:919-943`).

On the one hard question rask actually has — how a Project CR reconciles INTO a catalog record plus
tuples — the operator offers no evidence, because it has not built an entity reconciler
(`architecture.md:390-392`).

**Consequence: do not build an operator now.** Nothing in the lakehouse plan (A→B→C→D in the
register) needs one, and `services/controlplane` already answers 501 naming the unregistered type
when no operator is installed, never an empty list.

## 2. Decided

| # | Decision | By |
| --- | --- | --- |
| C-D1 | **Direction of truth for a Project CR: CR → catalog.** When `rask-operator` exists, its reconciler calls `POST /v1/projects` — the one door that mints the record and the `project#admin` tuple atomically — with a service identity, and writes the catalog-assigned id into `.status`. The catalog record remains the estate's source of truth; the CR is intent. Validate-don't-provision against rask's own control plane. | owner, 2026-09-02 |
| C-D2 | No operator is built until something in the lakehouse plan needs one. | owner, 2026-09-02 |

## 3. Cheap, in-scope now — no operator needed

| # | Item | Why | Where |
| --- | --- | --- | --- |
| C1 | **A render invariant that rask's chart emits no CRD in `platform.rask.io`.** | The 2026-08-16 ruling ("no CRD without its controller") is enforced today by nothing but the absence of a file; `tests/unit/test_invariants.py:1626` is the only CRD-aware scan and it merely skips CRD documents. | `tests/unit/test_invariants.py` |
| C2 | **The estate bootstrap as a recorded latch.** The bootstrap Job writes `_control/bootstrap.json` `{subject, store_id, model_id, at}` with `records.create_json` (create-iff-absent) after the seed and reads it before; 409-on-exists is success. | rask's `bootstrap-admin.yaml:128-146` is check-then-write, and `fga.provision()` rewrites the model on every unpinned boot (`governed/fga.py:319-349`). The operator's latch (`lakekeeper_controller.go:702-801`) is the shape: observe unconditionally, act only when enabled, treat 409 as success. Decide with it whether `provision()` gates on a model-content hash or `RASK_FGA_MODEL_ID` is the accepted pin. | `chart/templates/bootstrap-admin.yaml`, `service_kit/governed/fga.py` |
| C3 | **Typed conditions on the controlplane's Project boundary model**, additive to `phase`: `conditions[]` with `observedGeneration`, plus `catalogProjectId` and `namespace` as external facts. | Today `Pending` covers both "no status yet" and "status with an empty phase" (`controlplane/schemas.py:59-64`); nothing distinguishes "not yet reconciled" from "reconciled and failed". `phase` stays because the home zone renders it (`controlplane/service.py:186-197`). | `services/controlplane/src/controlplane/schemas.py` |

Not adapted: the operator's migration-Job-keyed-by-content-hash. Without status-as-memory a
hash-named Job is re-created after its TTL anyway, so rask keeps `Release.Revision` keying on its five
bootstrap Jobs and C2 is the honest version of the idea.

## 4. The operator contract — when `rask-operator` is written

Its spec, verified against what the reference operator does well and badly:

**Adopt**
- The CRD ships **inside the controller's own chart**, templated so `helm upgrade` updates it, gated
  by `crd.enable`, kept by `helm.sh/resource-policy: keep`, generated from the same source as the
  kustomize CRD (`charts/…/templates/crd/…yaml:1-8`, `values.yaml:82-86`, `PROJECT:9-11`). rask's chart
  never templates it (C1 is the gate).
- **Status is typed conditions** (`Ready`/`Degraded`/`ConfigWarning`), each with
  `observedGeneration`; external ids (`catalogProjectId`, `fgaStoreId`, `namespace`) in `.status`;
  intent (team, an OPAQUE workload label — the platform knows no workload names) in `.spec`; printer
  columns for what a human glances at (`lakekeeper_types.go:622-660`).
- **Invariants as CEL `x-kubernetes-validations`**, pinned by an envtest that boots a real apiserver
  and asserts the bad shapes are refused (`api/v1alpha1/lakekeeper_types_test.go`). No admission
  webhook, no cert-manager.
- **`ConfigWarning`**: a spec field the schema accepts but the reconciler ignores is reported by JSON
  path (`lakekeeper_controller.go:1462-1505`), never silently.
- **Validate, don't provision** — the reconciler's one write is `POST /v1/projects` (C-D1).
- Tenant identifier in every resource name and a tenant-scoped ServiceAccount — the JOIN the
  operator's multi-tenancy section makes explicit, without its one-instance-per-namespace model,
  which multiplies catalogs and authz stores and is the weaker fit for rask.

**Avoid — what the reference does badly (all verified)**
- An **unauthenticated plain-HTTP bootstrap POST inline in the reconcile loop**, base URL hard-coded
  `http://<name>.<ns>.svc…` (`lakekeeper_controller.go:716, 747-762`). rask's controlplane never
  calls the catalog; the reconciler must call it with a service identity and only through the door.
- **Cluster-wide `get/list/watch` on core Secrets** with an unscoped manager cache, for a namespaced
  CR (`config/rbac/role.yaml:7-14`, `cmd/main.go:181-199`). rask's controlplane ClusterRole reads
  exactly `projects` and `ingresses`; keep it that narrow.
- **A finalizer that does no work** (`:139-147, 325-346`) — it only guarantees CRs hang in
  Terminating whenever the operator is down. Load-bearing for rask: the catalog's project DELETE is
  bottom-up and refuses 409 while warehouses exist, so a finalizer either does that real work or
  must not exist.
- **"Halt until the spec changes" that is really a 10-minute retry loop** through the Job TTL, with
  failure read from counters, not conditions (`:232-243, 1513, 1616-1627, 1696-1697`); the unit test
  masks it by faking `Status.Failed = 3`. Failure must be a condition, and a halt must halt.
- **Whole-spec `CreateOrUpdate`** with no pod-level contract on the workload (`:435-525`): cannot run
  under restricted PSA, private registries or a sidecar; manual patches are reverted. rask renders
  the full contract on every app container and must keep doing so.
- `Ready` as pod-health only (`readyReplicas > 0`, no rollout-generation check).
- Half-wired values toggles (`prometheus.enable` renders a ServiceMonitor pointing at a Secret
  nothing creates); a migration identity keyed on an image STRING with samples at `:latest`; two
  deploy paths (kustomize + a chart generated from a gitignored file) with no parity gate; docs that
  drifted from code inside a one-commit repo. rask's existing gates (`test_no_dead_chart_env_vars`,
  per-component pins with digest-beats-tag, the single-artefact rule, the comment-history gate) cover
  the same classes and must stay.

**Not applicable**: Iceberg REST specifics; Postgres-bound `lakekeeper migrate`; Vault-KV2/Postgres
secret-backend selection; the `cedar` authorizer and `LAKEKEEPER+` fields; OpenFGA carried as opaque
connection config; kubebuilder/OLM scaffolding, Go SDK, docker/buildx + Kind e2e; the 55-name
`LAKEKEEPER__*` env facade.

## 5. Still open

| # | Question |
| --- | --- |
| C-Q1 | Does the deferred chart split (`docs/DECISIONS.md:812, 834-849`) become the home for rask-operator's chart — infra (operators + CRDs, rarely installed) vs app (upgraded constantly)? |
| C-Q2 | `fga.provision()` rewrite-on-every-boot: gate on a model-content hash, or accept `RASK_FGA_MODEL_ID` as the pin? (decide with C2) |
| C-Q3 | Which controlplane DTO fields freeze once conditions exist — `phase` is what the home zone renders today (decide with C3) |
