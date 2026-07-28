# Assessment 2026-07-15 — catalog bench, rask-merge readiness, production readiness

> Historical report (2026-07-15). Some documents it cites — `KIND-RUNBOOK.md` among them — were
> retired on 2026-07-27 and live in git history; citations below are left as written.

Three parallel assessments (multi-agent, evidence-grounded) requested alongside the #42
model-registry UI ship. **Only §3 survives as live reference.**

!!! abstract "§1 and §2 were cut on 2026-07-28 (P8) — both discharged"

    **§1 (catalog bench)** was superseded by [`BENCH-2026-07-22.md`](BENCH-2026-07-22.md), which
    re-grounds the list against current code and applies the Lance-only scope decision.

    **§2 (rask-merge work-list)** described work that the merge then performed; it enumerated a
    pre-merge tree (`services/common`, the orchestrator, the `-api` services) that no longer exists.
    Residual items were carried into [`OPEN-WORK.md`](OPEN-WORK.md), which is the live backlog.

    Both sections remain in git history. **§3 below is intact** — it is the only in-tree
    gap-by-gap production-readiness enumeration, and `OPEN-WORK.md` C4 plus
    `runbooks/RUNBOOK-oncall.md` ("ASSESSMENT gap #5") both depend on it.

## 3. Production-readiness gaps

# Production-readiness assessment — lance-ns

Method: rendered `helm template lance-ns ./chart -f chart/values.yaml -f chart/values-prod.yaml` with the required prod overrides (image tags, `dapr.appToken`, `age.password`, `rustfs.secretKey`, `observability.edgeAuth.htpasswd`, ingress host) — the fail-closed render guards all work as documented — then inspected the full 5.7k-line manifest plus `chart/values*.yaml`, `chart/templates/*`, `docs/{DURABILITY,OPERATORS,DEPLOY,KIND-RUNBOOK}.md`. Render saved at `/tmp/claude-1000/-home-blackwell-Desktop-lance-ns/88508a85-af5d-44c1-9ef1-92d04ece7015/scratchpad/prod-render.yaml`.

What is already genuinely good: render fails closed on every placeholder prod secret; 0 plaintext secret *values* in env/args (only access-key IDs; secret keys are all `secretKeyRef` → `lance-ns-infra-credentials`); app tier fully hardened (runAsNonRoot, drop-ALL, seccomp, readOnlyRootFilesystem, /livez probes ×42, preStop drain); daprd sidecars resource-bounded; Dapr mTLS pinned on; audit stream ON in prod; `/produce` and `/demo` correctly gated off; PDBs + 2 replicas for the stateless tier; 14d observability TTL; pg-dump + VolumeSnapshot CronJobs exist; `age.externalHost`/`rustfs.externalEndpoint` externalization keys verified actually rewiring env (the values-prod header saying they "need follow-up hooks" is stale doc-drift).

## CRITICAL

1. **Dex is a demo IdP in the prod render** — `chart/templates/dex.yaml`: `storage: type: memory` (sessions lost on restart, hardcoded `replicas: 1`), static users alice/bob with bcrypt("password") hardcoded in the template, static client secret `lance-catalog-secret` in a plain ConfigMap, issuer `http://lance-ns-dex:5556/dex` (in-cluster HTTP — unreachable by real browsers), redirect `http://localhost:8080/callback`. `values-prod.yaml` doesn't touch dex at all, yet the entire governance layer (auth.enabled=true) rests on it. **Unnoticed** (no written deferral found). Fix: prod overlay must set an externally-reachable HTTPS issuer, a real storage backend (dex supports postgres — AGE is already there), connector to the org IdP (or at minimum remove the static demo users), and move the client secret into the infra-credentials/OpenBao path.

2. **No alerting engine at all** — confirmed: zero PrometheusRule/Alertmanager/rule-evaluator anywhere; the only trace is a Perses panel literally titled "alertable pair" (`perses-dashboards.yaml:148`). Nobody is paged when NATS dies, OpenBao seals, the outbox stops draining, or the DLQ fills. **Unnoticed.** Fix: GreptimeDB speaks PromQL (`/v1/prometheus`) — deploy vmalert (or Prometheus in rule-only mode) + Alertmanager with a real route; seed rules from the panels already labeled alertable (outbox depth/oldest-age, DLQ rate, error rate, NATS consumer lag, pod restarts).

3. **NetworkPolicy: zero objects in the prod render** — the full L3 layer (default-deny in/egress, DNS allow, exclusive client lists for openbao/age/rustfs) is *built* in `network-policy.yaml` but `networkPolicy.enabled` stays false and values-prod doesn't flip it. Compounding: the values file itself documents that lance-ray's in-cluster `/produce` ClusterIP route "stays reachable + unauthenticated" — with no L3 layer there is no compensating control against in-cluster cascade forgery. **Documented deferral** (kind CNI doesn't enforce; §7a runbook exists) — but the prod overlay is exactly where the flip belongs. Fix: `networkPolicy.enabled: true` in values-prod (+ `extraEgress` for any externalized backend), and note the policy-enforcing-CNI prerequisite there.

4. **Single-instance data/authz tier = platform-wide SPOF stack** — in the prod render: AGE Postgres 1 replica / 1Gi PVC (values-prod never sizes it) serving BOTH the lineage graph AND OpenFGA's datastore → AGE down = every governed request in the platform fails closed; NATS 1 node / `streamReplicas: 1` / 1Gi (bus down = no lineage ingest, no cascade — the parked **#20**); OpenFGA `replicaCount: 1` (stateless over PG — trivially scalable, not flipped); RustFS 1 replica on one 200Gi PVC = the entire lakehouse + observability store; OpenBao 1. **Documented deferral** ("stateful HA is the operators' job in rask — CNPG/rustfs-operator"; NATS externalize stanza commented in values-prod) — legitimate, but until the rask merge the self-contained prod path has no HA story. Fix now: `openfga.replicaCount: 2` (free), size `age.storage`, and treat the externalize stanzas (managed PG, clustered NATS `streamReplicas: 3`, managed S3) as the actual prod gate, not an optional footnote. Also: the auto-derived external DSNs keep `sslmode=disable` — externalizing PG today would go plaintext unless the openfga DSN is manually overridden.

5. **OpenBao sealed-on-restart = boot deadlock** — devMode=false is correct, but every pod restart/node drain leaves OpenBao sealed until a human runs `bao operator unseal`; apps consume secrets fail-closed via Dapr *at startup*, so app pods hang at "waiting for application startup" (the documented two-sided deadlock, `docs/OPERATORS.md` §5). No auto-unseal. **Documented deferral** (ESO / bank-vaults / vault-operator is "the destination"). Routine prod events (node drains, OOM) trigger it — do the operator adoption before calling this tier prod-ready, and alert on seal status (see gap 2).

## IMPORTANT

6. **Kind-only image assumptions baked into the chart** — app images render as bare local names (`lance-rest-catalog:v1.0.0`, no registry) and the chart has **no imagePullSecrets support anywhere** — unpullable on any real cluster except via node preloading. All images tag-pinned (good), none digest-pinned; one mutable third-party tag in the render (`ghcr.io/cloudoperators/greenhouse-extensions-integration-test:main`, openfga subchart helm-test pod). **Unnoticed.** Fix: registry-qualified `image.*.repository` in values-prod + `imagePullSecrets` plumbing in `_helpers.tpl`/pod specs; digests for the two first-party images.

7. **Resources: one-size-fits-all app tier, unbounded infra tier** — every app container gets `resources.default` (50m/128Mi → 1cpu/512Mi), including RustFS (the whole S3 data plane capped at 512Mi) and AGE Postgres (512Mi). Meanwhile GreptimeDB, NATS, Vector (a DaemonSet — unbounded on every node), OpenFGA, Perses, and the entire Dapr control plane have **no requests/limits at all**. **Unnoticed.** Fix: per-component resource keys for the first-party infra; pass resources through the subchart values (greptimedb-standalone, nats, vector, openfga, dapr).

8. **2-replica services have no spread/anti-affinity** — the only podAntiAffinity in the render belongs to the Dapr scheduler. catalog/lineage/gateway/web (replicas=2, PDB minAvailable=1) can co-schedule on one node → one node failure still takes the service to 0. **Unnoticed.** Fix: `topologySpreadConstraints` on `kubernetes.io/hostname` in the shared pod template.

9. **Dapr control plane left non-HA in prod** — values-prod doesn't set `dapr.global.ha.enabled=true`: operator/sentry/injector/placement all 1 replica. Placement is *hard-required by daprd 1.18 at boot* (your own values comment) — a placement restart during a rollout stalls every new sidecar; sentry down stops mTLS issuance. **Unnoticed** (one-line flip).

10. **Backups: destination shares fate with the primary, no retention, no restore runbook** — the pg-dump CronJob uploads lineage+openfga dumps to `s3://lance-catalog/_backups/pg/` — the *same RustFS PVC* the VolumeSnapshot protects, so a PVC/cluster loss takes primary and pg backups together; nothing prunes `_backups/` (unbounded growth inside the lakehouse bucket) or old VolumeSnapshots (the CronJob only creates); `snapshotClassName: ""` will fail on clusters without a default class; and there is **no restore procedure anywhere in docs** (grep "restore" in DURABILITY.md backup section = 0 hits). The externalize-to-operators posture is a **documented deferral**, but the retention/fate-sharing/restore gaps in the shipped mechanism are **unnoticed**. Fix: point pg dumps at an off-cluster bucket, prune both artifact kinds, set the class, write and *drill* a restore runbook.

11. **No TLS at the edge** — the rendered Ingress has no `tls:` block (values-prod leaves it empty, no cert-manager annotation): OIDC tokens, the edge-auth basic credentials, and vended S3 credentials would traverse plaintext HTTP. In-cluster, Dapr mTLS covers service-invocation/pubsub only; direct app→infra hops are plaintext (`sslmode=disable`, http RustFS/Greptime) — acceptable in-cluster, but the edge is not. **Unnoticed.** Fix: `ingress.tls` + cert-manager annotation in values-prod.

12. **Built-and-live-proven hardening switches silently omitted from values-prod** — `security.serviceAccounts` (every app pod runs as SA `default` with token automount on), `security.infraContexts`, `dapr.sidecarRestricted` all false in the prod render, despite memory recording them "left ON and proven" on the 2026-07-13 live pass. PSA `restricted` enforce is separately **parked** (OTel Collector hostPath — documented, KIND-RUNBOOK §6.4). The values-prod omission of the proven switches looks like an oversight, not a decision. Fix: flip all three in values-prod (with the documented `dapr_sidecar_injector.sidecarDropALLCapabilities=true` companion).

13. **`deployment.environment.name=kind` ships in the prod render** — values-prod never overrides `observability.environment`; every OTel resource attr will claim prod telemetry is kind. **Unnoticed.** One line.

14. **Manual out-of-band install-order footguns** — values-prod itself warns: flip `medallion.fgaEnabled` without first running `scripts/seed_medallion_fga.sh` and the movers fail closed / pipeline stalls; OpenBao needs manual init+unseal; the namespace PSA label is a manual kubectl step. There is no prod install runbook — DEPLOY.md is kind-framed ("how it all works on kind"), OPERATORS.md is strategy. Fix: a PROD-RUNBOOK.md (ordered: secrets → seed FGA → unseal → flip governance → verify), plus consider a seed Job/hook for the FGA grants.

## NICE

15. `moverReplicas: 1` throughput/availability ceiling — **documented deferral** (process-local single-flight lock; raise after a cross-pod lock ships). Fine as-is; NATS redelivery + idempotence bound the damage.
16. nats-box debug shell Deployment ships in the prod render — set the nats subchart's `natsBox.enabled=false` in values-prod.
17. Dapr scheduler STS: 3×16Gi PVCs for actors/workflows/jobs the stack doesn't use — shrink the PVC size via subchart values.
18. Retention odds: `runRetentionDays: 0` (Run nodes grow forever — prod has the reconcile pruner deployed but the knob off), `compaction.lineageEmit: false` (the compaction FAILURE lineage surface stays dark in prod), `freshnessBudgetHours: 0`. Audit-stream retention sharing the 14d observability TTL is a **documented deferral** — note 14d is short for a compliance trail.
19. HPA off (documented optional, needs metrics-server) — fine; enable once resource requests are truthful (gap 7), since HPA math depends on them.
20. values-prod header's claim that the externalize stanzas "do nothing without follow-up hooks" is stale — verified `age.externalHost` rewires lineage DSN, openfga DSN, wait-init and pg_dump correctly. Fix the comment so operators trust the mechanism.

## Deferred vs unnoticed — the roll-up
- **Written deferrals (don't re-litigate, schedule):** NATS HA/externalization (#20 parked), stateful HA via rask operators (CNPG/rustfs-operator), OpenBao auto-unseal via ESO/bank-vaults (OPERATORS.md §5), audit retention = observability TTL, PSA restricted enforce (OTel Collector hostPath), L3 default-deny known un-flipped (§7a runbook), moverReplicas=1, mode_b vending.
- **Genuinely unnoticed:** Dex demo-IdP posture in prod, no alerting engine, backup fate-sharing/retention/restore, no registry/imagePullSecrets plumbing, unbounded infra resources, no anti-affinity, dapr.global.ha unflipped, values-prod omitting the live-proven SA/infraContexts/sidecarRestricted switches, environment=kind attr, no edge TLS, no prod install runbook.
