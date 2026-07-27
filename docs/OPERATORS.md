# Operators & the submit-seam boundary — what we adopt, what rask supplies, what we never build

**Status: DECIDED 2026-07-11** (user question: "what is the most logical operator for this platform,
and can rask help"). Grounded in [`RASK-INTEGRATION.md`](RASK-INTEGRATION.md) (rask's actual chart),
[`RAY-TRAIN.md`](RAY-TRAIN.md) D6, and [`RAY.md`](RAY.md). One-line answer: **KubeRay first — and
rask helps with everything, because rask already operates every operator on the list.** The merge
is "flip values at rask's operators", never "deploy operators ourselves".

## 1 · Adoption order (most logical first)

| # | Operator | Replaces (ours, hand-rolled) | Why this rank |
|---|---|---|---|
| 1 | **KubeRay + Kueue** | raw Ray head Deployment + our submit/re-attach/poll logic | Replaces the WEAKEST thing we own. A `RayJob` CR is the missing *lifecycle owner* the whole TRAIN design (D2 submit-and-ack) works around: the operator watches the job to a terminal state, owns retry policy + TTL cleanup; `RayService` gives rolling cluster upgrades; Kueue adds GPU quota + gang scheduling for training. Already pinned as the rask-merge step (D6). |
| 2 | **CloudNativePG** | hand-rolled AGE StatefulSet + OpenFGA datastore | Managed Postgres `Cluster` (HA, backups, failover) for the ONLY relational state of record (AGE + OpenFGA). The AGE-extension question is **SOLVED + PROVEN**: AGE reached PG18 (v1.7.0), so it mounts as a CNPG **ImageVolume extension** on a STOCK Postgres image — verified locally (`CREATE EXTENSION age`/`create_graph`/cypher all work via `extension_control_path`). Full how-to + the custom-full-image bridge for pre-1.33 clusters: **`docs/CNPG-AGE.md`** (`.docker/cnpg-age-ext.dockerfile`, `deploy/cnpg-age-cluster.yaml`). |
| 3 | **rustfs-operator** | hand-rolled RustFS Deployment | S3 `Tenant` CR with a declarative `buckets:` list — our lakehouse + observability buckets become list entries. |
| 4 | **NACK** (NATS operator) — *optional* | the imperative `nats-stream-job.yaml` provision Job | Declarative `Stream` CRDs would replace the shell Job that today creates `LINEAGE` / `MEDALLION` / `TRAINING` (Dapr's jetstream component does not auto-create streams). Nice-to-have: the Job works and is idempotent; NACK removes a boot-ordering foot-gun, nothing more. |
| 5 | **Secrets operator** (External Secrets Operator, the Vault/OpenBao operator, or bank-vaults) — *interim: no operator, just a values flip* | the `server -dev` in-memory OpenBao + the `openbao-seed` post-upgrade hook | Dev-mode OpenBao (`server -dev`) holds secrets **in memory**: any pod restart wipes `secret/lance`, and the ONLY re-seed path is the helm post-upgrade hook — so an *out-of-band* restart leaves every app's `apply_dapr_secrets` retrying a Dapr `500` on the missing key **forever** (lifespan never completes → pod stuck `0/2` → daprd waits on the app that never listens). **Observed live 2026-07-14** (§5) when an interrupted helm upgrade restarted OpenBao mid-churn. The **acute fix is NOT an operator** — it is `openbao.devMode=false` → `server -config` on the existing PVC, which the chart already supports and which makes secrets survive restarts. The operator earns its place only at prod tier: **auto-unseal** (retire manual `bao operator init`/unseal), **declarative secret sync** (retire the seed Job entirely), plus rotation/PKI. |
| 6 | **NVIDIA DCGM operator** — *net-new, arrives with #1* | nothing (no GPU in-chart today) | GPU telemetry (`dcgm-exporter` DaemonSet + driver/toolkit) for GPU-backed training. The demo `train_demo_model` is CPU-only; a real `TorchTrainer` on GPU workers lands as a **KubeRay `RayJob` under Kueue** (RAY-TRAIN.md D6), so DCGM rides in with #1. The exporter's `/metrics` is node-level infra scraped by the **OTel Collector's `prometheus` receiver** (the same one that takes the Dapr sidecars — see the OTel row); the only additions at that point are a `dcgm-exporter` scrape target and `nvidia.com/gpu` limits + `num_gpus` on the Ray worker/job. Nothing to flip in this chart until GPU workers exist. |
| 7 | **OpenTelemetry Collector** (OTel operator) | **Vector (removed)** + the redundant vmagent (removed) | OTel-first: a **single in-chart OTel Collector** (`observability.otelCollector`, templates/otel-collector.yaml) is now the one telemetry hub — `otlp` receiver ← apps, `filelog` ← no-SDK infra-pod logs (**replaced Vector**, which was deleted: the subchart, its config, and the vendored tarball), `prometheus` receiver + k8s SD ← Dapr `:9090` (and DCGM later). It exports OTLP → GreptimeDB (db-name header on all signals; trace-pipeline header on traces via a second exporter). Apps are backend-agnostic (plain OTLP, no vendor headers — the Collector adds them). **Prod**: `observability.otelCollector.externalEndpoint=…` renders NO in-cluster Collector; apps point at the operator-managed external one (the OTel operator runs the agent-DaemonSet + gateway). The in-chart Collector is a single Deployment — fine on single-node kind/dev; multi-node prod uses the external operator. `filelog` persists its read offsets via a `file_storage` extension (emptyDir at `/var/lib/otelcol/file_storage`, writable through fsGroup 65534), so a Collector *container* restart resumes where it left off instead of jumping to the tail — the parity with Vector's `data_dir` for the common crash/OOM case (proven live: the `receiver_filelog_` bbolt DB is written on disk by the non-root runtime). Full-*reschedule* offset durability is the external operator's job, and the in-chart Collector renders nothing in prod. **Why OTel and not keep Vector?** dev must run the *same* log pipeline prod runs (the OTel operator's Collector) — Vector-in-dev would mean testing a component you never ship. Vector isn't redundant or bad; it just isn't what prod runs. |

### Handoff wiring status (audited 2026-07-17 — what actually renders vs. what was documented)

An 8-operator handoff audit found the *architecture* documented but three externalize paths claimed "wired"
that weren't. Fixed:
- **OTel / external Collector** — the app OTel SDK wiring was gated on `observability.enabled`, so
  `externalOtlpEndpoint` alone emitted **nothing**. Now gated on `lance.otelEnabled` (= `enabled` OR
  `externalOtlpEndpoint`); the `lance-tracing` Dapr config renders on the same predicate (no dangling
  `dapr.io/config`). This is the seam the OpenTelemetry Operator's Collector plugs into.
- **rustfs-operator (#3)** — externalizing RustFS left `greptimedb-standalone.objectStorage.s3.endpoint` at
  the deleted in-cluster service (a static subchart value that can't follow the helper). The `values-prod`
  EXTERNALIZE block now pairs them atomically and `prod-render-check` leg 10 fails if only one is set.
- **External Secrets Operator (#5)** — `externalSecrets.enabled=true` is now exercised by `prod-render-check`
  leg 11 (SecretStore + ExternalSecret CRs render, static Secrets skipped, fail-closed guard satisfied with
  no plaintext `age.password`/`rustfs.secretKey`).
- **CloudNativePG (#2)** — the AGE-extension blocker is **solved + proven** (`docs/CNPG-AGE.md`): AGE now
  ships for PG18 (v1.7.0), so it mounts as a CNPG **ImageVolume extension** on a stock Postgres image (no
  fork). Proven locally end-to-end (`.docker/cnpg-age-ext.dockerfile` builds it; a stock PG18 loads it via
  `extension_control_path` and runs `create_graph`/cypher). Needs K8s 1.33+/containerd 2.1/CNPG 1.27; for
  older clusters the custom-full-image (PG16) bridge is documented. Physical PITR replaces the pg_dump path
  (safer for AGE). OpenFGA (plain SQL) is unaffected.
- **KubeRay (#1)** — the submit seam is already agnostic; the only chart handoff is repointing
  `medallion.rayAddress` at the RayCluster head's dashboard service (an EXTERNALIZE stanza is now shown).

**What we never build: a custom lance-ns operator.** An operator earns its complexity when state of
record lives in CRDs and needs reconciling. Ours does not: catalog/lakehouse state lives in **Lance
manifests on S3**, authz + lineage in **Postgres** (OpenFGA, AGE), and every control loop we need is
either an existing service (compaction sweeper), an existing Job (stream/bucket/seed provisioning),
or one of the operators above. A "lance operator" would be a second reconciler over state that
already has an owner.

## 2 · How rask helps (short version: with all of it)

rask **already operates every operator above** — KubeRay, Kueue, CloudNativePG, rustfs-operator —
plus NATS, Dapr, OpenFGA-server, Traefik, and the GreptimeDB observability stack
(`RASK-INTEGRATION.md` §Pre-flight). So the adoption plan is NOT "install operators here"; it is the
merge itself: flip `age.enabled`/`rustfs.enabled` off, point the externalization values at
`<release>-postgres-rw` / the rustfs `Tenant`, and retire the raw Ray head into rask's KubeRay
cluster. In exchange we contribute what rask lacks entirely: lineage, actual OpenFGA *enforcement*
(rask provisions the server but wires nothing to it), and the event-driven medallion estate.
Interim posture in THIS repo: keep the hand-rolled infra — it is demo-tier scaffolding that the
merge deletes (`RASK-INTEGRATION.md` §5), so operator-izing it here would be double work.

## 3 · The Ray submit seam: lance-ns is the agnostic side (DECIDED, mirrors D6)

Question: does the Ray-submit belong in rask or in lance-ns — which is more agnostic for the jobs
we submit? **Ours, deliberately:**

- `services/medallion/services/ray_submit.py` is **httpx-only against the Ray Jobs REST API** — no
  `ray` package, no Kubernetes API, no KubeRay CRDs. Its whole contract is *entrypoint + env + a
  deterministic submission id*. That runs unchanged against a raw Ray head on kind, a KubeRay
  `RayCluster`/`RayService`, or a managed Ray — anything speaking the Jobs API.
- The jobs themselves (`ray_lance_job.py`, `ray_stage_job.py`, `ray_train_job.py`) are equally
  agnostic: plain env-driven scripts baked into the ray image, zero submission-side coupling.
- rask's path (`ray-kit`/orchestrator → Kueue-admitted `RayJob` CRs) is the more *operationally
  capable* transport — and the more coupled one (Kubernetes API + KubeRay + Kueue present).

So the roles split, and the split IS the design (RAY-TRAIN.md D6):

- **lance-ns owns the seam + the contracts**: trigger payloads, the deterministic idempotency key
  (`ray-<stage|train>-<token>`), the lineage contract, the FGA gates. These survive the merge
  untouched.
- **rask owns the production transport**: at the merge, add a second transport behind the SAME
  `submit_stage_job`/`submit_train_job` signatures that creates a `RayJob` CR instead of POSTing
  `/api/jobs/`. Idempotency maps 1:1 — use the deterministic submission id as the **CR name**;
  "create of an already-existing CR" becomes the re-attach branch.
- Anti-goals: do NOT port our submit logic into rask, and do NOT adopt ray-kit here. The seam is
  the boundary; it is already in the right repo.

## 4 · Do Dapr or Lance need an operator story? (asked 2026-07-11)

**Dapr — already covered, nothing to adopt.** Dapr's control plane *is* an operator
(`dapr-operator`, sidecar-injector, placement, sentry — helm-installed here, already running on
rask), and our pub/sub wiring is already declarative: `Component` CRs (`pubsub.jetstream`), a
`Subscription`-via-code model, and per-app scoping. The one imperative residue is stream
provisioning — that is the NACK row above, not a Dapr gap. Also pinned (python-infrastructure
skill + #115 review): **Dapr Workflow stays un-adopted** — every multi-step path we have
(bytes-then-commit registration, stage hops) is token-keyed idempotent, so JetStream's
redeliver-the-whole-message model suffices; a workflow engine would add a sidecar state store
dependency for crash-recovery semantics we already get from idempotency keys.

**Lance — no operator exists, none is missing.** Lance is a format + libraries; its "control
plane" is the manifest on S3, and CAS commits are the reconciler. The operator-shaped concerns it
DOES have are already owned elsewhere:

| Concern | Owner today | Status |
|---|---|---|
| background maintenance (compact/optimize) | `services/compaction` sweeper (cron-driven, FAIL-visible) | shipped |
| orphan-artifact GC (`models/<m>/<token>/` from crashed runs; blob-pointer lifecycle) | `scripts/model_artifact_janitor.py` — dry-run default, referenced⇒never-collected unit-pinned | shipped 2026-07-11 (models lane); live drive + the broader pointer-aware-GC posture remain §9 |
| stream/bucket/grant provisioning | chart Jobs + seed scripts (idempotent) | shipped; NACK could absorb the stream half |
| version/feature pinning (`pylance`, `lance-ray`, data_storage_version) | image pins + probe-before-callsite (§0) + docs/RAY.md landmines | process, not software |

So: nothing to install for either. The single actionable follow-up either way is the **orphan
janitor** (already tracked in docs/DECISIONS.md #blob-pointer-lifecycle-gc--never-collect-referenced-artifacts as blob-pointer lifecycle — GC must never collect
registry-referenced artifact objects, and crashed-run tokens need a sweep).

## 5 · Secrets: the dev-mode fragility, and the operator plan (added 2026-07-14)

**What we hit.** Driving the #4 outbox live meant a helm upgrade that (via an unrelated interrupted
run) restarted the OpenBao pod. OpenBao runs `server -dev` → its KV store is **in memory**, so the
restart wiped `secret/lance`. The `openbao-seed` Job that repopulates it is a **post-upgrade hook**,
so it only fires on a helm release — not on a bare pod restart. Every new app pod's lifespan calls
`apply_dapr_secrets` → Dapr's vault secretstore → `GET /v1.0/secrets/lance-secrets/lance` → **`500`**;
the Dapr client retries with growing backoff, so uvicorn stays at "Waiting for application startup",
never binds `:8000`, and daprd sits "waiting for application to listen" — a two-sided deadlock that
reads like a crash but is a **missing secret**. Root-caused by running `apply_dapr_secrets` inside the
stuck pod under a watchdog; fixed by re-running the seed (the chart's own hook) + recreating the pod.

**The plan (two levels, do the cheap one regardless):**

1. **Interim — a values flip, NOT an operator (cheap, do soon):** set `openbao.devMode=false` so
   OpenBao runs `server -config` against its **already-provisioned PVC**. Secrets then survive pod
   restarts, and the seed Job becomes first-boot-only. Cost: OpenBao then needs a real
   `operator init` + unseal step (no fixed root token) — which is the exact chore the operator
   removes, so this is the bridge, not the destination. Tracked in `docs/DECISIONS.md`.
2. **Prod — a secrets operator (the destination):** adopt **External Secrets Operator** (cloud-agnostic,
   syncs from any backend into k8s Secrets / Dapr), the **Vault/OpenBao operator**, or **bank-vaults**
   for **auto-unseal** (no manual init/unseal) + **declarative secret sync** (retire `openbao-seed`
   entirely) + rotation/PKI. Same "operators later" wave as rows 1–4. **First verify whether rask
   already operates one** (it operates the other four — §2); if so, merge = flip values, not install.
   If not, ESO is the lowest-coupling adopt because it is backend- and cloud-agnostic like our Dapr
   secretstore seam already is.

**Why it was invisible until now:** the seed hook makes the happy path (a clean `helm upgrade`) always
re-seed, so dev-mode's in-memory loss only bites on an *out-of-band* restart. That is rare in a quiet
demo and routine in prod (node drains, OOM, rollouts) — which is precisely why it belongs on the
operator wave and not in the "never build" bucket.
