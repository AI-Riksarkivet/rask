# Ray TRAIN vs Ray DATA — the training-workload design (task #115)

**Status: DESIGN DECIDED 2026-07-10; #115a (head + topic + submit-and-ack consumer, D1/D2), the
#115c seed grants (D5), and #115b (`scripts/ray_train_job.py` + the D4 registry publish + lifecycle
lineage) ALL LANDED — code-complete + adversarially reviewed at the unit tier. The 2026-07-10 review
caught an FGA-wiring bypass, floating-version admission, and an unforwarded config; the 2026-07-11
review (lineage/registration/Dapr lenses) caught a missing JetStream stream for `training.jobs` (the
deployed bus would have rejected every publish — now provisioned), a masked-error window in the
registry create-vs-append branch, N+1 sequential FGA round trips (now ONE `batch_check`), a missing
`dataSource` facet (the reconcile back-fill key), and head/consumer name-validation asymmetry (the
head now 422s what the consumer would DROP). Open: the chart values passthrough (deferred until helm
render-verification is possible) and the live kind drive — see `docs/DECISIONS.md` #115a-c.** The platform must host BOTH batch/ETL
(today's medallion cascade — the Ray *Data* shape) and TRAINING workloads (Ray *Train*). They are
different workload classes and get different runtime treatment — but ONE provenance model, ONE
authz model, and ONE storage substrate.

| | Ray DATA (today's cascade) | Ray TRAIN (this design) |
|---|---|---|
| Duration | bounded stage transform (seconds–minutes) | long-running (minutes–hours), often GPU |
| Trigger semantics | stage hop: mover blocks until the job lands, then fires the next stage | fire-and-track: submit + ack; the JOB reports its own lifecycle |
| Output | the next stage's Lance dataset (+ version) | a MODEL artifact |
| OpenLineage `jobType` | `ETL` / `TRANSFORMATION` | `TRAINING` |
| Failure policy | RETRY (redelivery re-runs the transform) | terminal FAIL (no auto-resubmit — GPU-hours) |

---

## D1 — Head shape: a separate `POST /train` endpoint + its OWN topic (not a field on the stage trigger)

- **`POST /train`** on lance-ray (the compute-adjacent head service, same `require_dapr_token`
  guard as `/produce` and `/ingest-media`). Body:
  `{"model": "<name>", "features": [{"dataset": "...", "version": <int|null>}], "config": {...}}`
  → `202 {"token": ...}`. An omitted `version` pins to the LATEST at submit time — the pin is
  resolved at the head and threaded through, never left floating (reproducibility).
- **Own topic** (`training.jobs` via `MEDALLION_TRAIN_TOPIC`), own durable consumer + queue group.
  NOT a workload-type field on the medallion trigger: `{token, dataset, namespace}` is a
  stage-hop contract with mover semantics (block-until-done ≤ ackWait, RETRY on failure).
  Training is long-running and terminal-on-failure — overloading the stage trigger would couple
  it to mover ack windows and redelivery policy. A separate topic is a separate resiliency
  profile, and a slow training submit can never head-of-line-block a stage mover.
- Trigger payload: `{token, model, features: [{dataset, version}], config}` — pointers only,
  never data (the claim-check invariant). Name shapes are enforced SYMMETRICALLY: `model` is a
  path-safe slug, every `dataset` is exactly `stage$name` (the names become S3 key prefixes, Lance
  URIs, and lineage namespaces), ≤ 16 features, config ≤ 8 KiB — the head 422s violations, and the
  consumer independently DROPs them (the bus is a wider trust surface than the token-guarded head).
- The topic gets its OWN JetStream stream (`TRAINING`, subjects `training.>`, provisioned by the
  chart's nats-stream Job next to `LINEAGE`/`MEDALLION`) — Dapr's jetstream component does not
  auto-create streams, and a separate stream keeps durable-consumer names per-stream and training
  backpressure isolated from the stage cascade.

## D2 — The trainer consumer: SUBMIT-AND-ACK, never block-and-poll

`services/medallion/services/ray_submit.py` documents its own limitation: the mover blocks until
the job finishes, so anything outliving the redelivery window exhausts it (default resiliency-ON deploy: sidecar-owned retries + a 720s broker crash-recovery ackWait; the `resiliency=false` escape hatch reverts to the broker-only ~2.5 min).
That pattern is CORRECT for bounded stage transforms and WRONG for training. The trainer handler
therefore:

1. FGA-gates (D5) — deny → **DROP** (attributable, like the movers), outage → RETRY; the per-input
   checks go through ONE `batch_check` round trip regardless of feature count, so the gate cannot
   stack per-check retry budgets past the 30s ack window;
2. submits the training job via the shared Ray Jobs REST seam with a **deterministic
   `submission_id = ray-train-<token>`**;
3. **acks SUCCESS immediately after the submit** (or after re-attaching to an already-running
   job on redelivery — the deterministic id is the idempotency key, per the §0 bus rule).

The handler never observes completion. **The training JOB ITSELF emits its OpenLineage
lifecycle** (D3) — the same driver-emits pattern `medallion_demo.py::_emit_step` proved — so job
duration is decoupled from bus semantics entirely. Redelivery of a trigger whose job already
FAILED does **not** resubmit (unlike the stage path): training compute is expensive and a failed
run is terminal until a human (or future automation) POSTs `/train` again with a fresh token.

## D3 — Lineage shape: official facets only, spec-true

- **Job**: namespace `ray-jobs`, name `train.<model>`; the official **`JobTypeJobFacet`** with
  `processingType=BATCH, integration=RAY, jobType=TRAINING` — the discriminator vs the cascade's
  `ETL`/`TRANSFORMATION`. (`jobType` is a free-string field in the published facet schema —
  `TRAINING` is spec-legal, no invented facet needed.)
- **Run**: deterministic `runId = run_id_for("train-<token>")`; lifecycle
  `START → RUNNING×N → COMPLETE|FAIL`. Progress rides the existing custom `progress` run facet
  (`{done: epoch, total: epochs}`) so the live status board renders training progress with zero
  new UI work. FAIL carries the standard `errorMessage` facet and a BARE output (no version — no
  fabricated lineage), exactly the medallion/compaction FAIL contract.
- **Inputs**: each feature dataset with the standard `DatasetVersionDatasetFacet` pinning the
  EXACT Lance version read — the reproducibility key. The graph then answers both directions:
  `upstream(models$m)` = "what data (at which versions) trained this model";
  `downstream(silver$features)` = "which models did this bad batch contaminate" (model recall).
- **Output**: `models$<model>` with the version facet + blob-aware schema facet on COMPLETE; the
  standard `dataSource` facet (the registry URI) rides on EVERY event type — it is location
  metadata, not a success claim, and it is what lets the B4 reconcile back-fill recover a model
  version whose COMPLETE emit was lost (without it the models node has no `source_uri` and the
  sweep can never repair it).
- **Emission transport**: Ray pods carry no Dapr sidecar, so the job POSTs to the lineage HTTP
  ingest (`LINEAGE_URL`, default the in-cluster service) — best-effort, two attempts, never crashes
  training. **Accepted trace break (audit 2026-07-15)**: distributed traces end at the Ray Jobs API —
  the job propagates no `traceparent`, so the trainer is trace-dark by design; job-level visibility is
  the Ray dashboard + its OpenLineage RunEvents + the `lance.training.*` metrics. Revisit at the
  KubeRay merge alongside the runtime_env secret tightening. **Governed deployments — the SERVICE-DOOR credential (closed 2026-07-13).** With
  `auth.enabled` the ingest requires a verified caller, and until this fix every training RunEvent
  401'd → **all training provenance was silently lost** (the job logs `HTTP 401` per attempt,
  distinguishable from an outage). The fix follows the trust model already in the code: the Dapr
  subscription route (`lineage/api/dapr.py`) does **not** use OIDC/FGA — it is guarded by the shared
  app token and trusts the producer-stamped author; OIDC is the *external/human* door. The Ray job is
  an internal producer that merely lacks a sidecar, so it authenticates as the **service it already
  is**: `LINEAGE_SERVICE_TOKEN` (the app token the submitter injects into the Ray `runtime_env`) +
  `LINEAGE_SERVICE_ID` = its bare FGA subject `service-trainer`. Lineage
  (`ServicePrincipal`, `lineage/api/security.py`) verifies the token, checks the subject against the
  `LINEAGE_SERVICE_SUBJECTS` allowlist (chart: only `service-trainer`), stamps it as author, and
  **still FGA-checks `can_write_data` on every output** — so the trainer records provenance only for
  what D5's `writer`-on-`namespace:models` rung permits. This does NOT mint a Dex user (which would be
  the "second identity axis" D3 argues against), and it is *stricter* than the mover path it mirrors
  (movers self-assert an unverified `MEDALLION_AUTHOR` config string; the trainer's outputs are
  authorized). The allowlist is what stops an app-token holder speaking as a human. `LINEAGE_TOKEN`
  (an OIDC bearer) is still honoured for external producers/tests; the demo tier runs auth-off (no
  app token → the service door stays shut → the ingest is open).
  > **Residual exposure (tracked):** the app token rides in the Ray `runtime_env`, which the Ray Jobs
  > API echoes back — the SAME exposure the S3 credentials already have. Tighten both together with a
  > secret mounted on the Ray pods at the KubeRay `RayJob` merge (D6).

## D4 — The model REGISTRY is a Lance dataset; the artifact BYTES are plain S3 objects (DECIDED,
## sharpened 2026-07-10 after design review)

The one-liner is NOT "the model is stuffed into a table". Split the registry *record* from the
artifact *bytes*:

1. **Bytes first, plain paths**: the training job writes ordinary S3 objects serving tools can
   load directly — `s3://<bucket>/models/<model>/<token>/{weights.safetensors,config.json,
   metrics.json}` — keyed by the run token so a retried job overwrites its own paths
   (idempotent).
2. **Registry record second, one atomic commit**: `models$<model>` — one Lance dataset per model
   in the `models` namespace, one row per artifact, `payload` = an **external blob pointer**
   (`Blob.from_uri`) at those plain paths:

   | column | type | content |
   |---|---|---|
   | `artifact` | string | `weights` \| `config` \| `metrics` \| `card` |
   | `payload` | blob v2 (2.2, stable row ids — the shared write helper) | **pointer** to the plain-path object (inline ONLY for small models, ≲ a few MB, where self-containment is worth it) |
   | `meta` | JSON | framework, metric values, epochs, the pinned feature `{dataset: version}` map |

   Publishing a model version IS this commit — the CAS-validated Lance commit is the atomic
   registration step. A crash between (1) and (2) leaves orphan artifact files but never a
   half-registered model; the token-keyed paths make the retry converge. Two operational rules the
   implementation pins: the artifact base is registered as the dataset's external-blob base AT
   CREATE (create-time-only) — so **the base must stay stable per model** (pointers outside it are
   refused by Lance on append, loudly); and a create that loses the concurrent first-publish CAS
   race converges as an append instead of terminally failing the run.
3. **Versioning + promotion for free** (SHIPPED, #17, 2026-07-15): model version N = Lance version N
   of the registry dataset (time-travel = full model history); promotion (candidate→**blessed**) = a
   moving catalog **tag** on it, gated by the `validator` rung (`can_promote`), reusing the silver→gold
   promotion story. Built as the catalog `POST /v1/model/{model}/promote` (+ `GET /v1/model/{model}`):
   it opens the registry by explicit URI (the trainer writes it outside the catalog's native namespace
   resolution — see the `models_registry_root` setting), runs a fail-closed **metrics gate**
   (`min_metrics`: each named `meta.metrics` key must be present + ≥ its threshold) BEFORE the
   irreversible tag move, then emits a distinct `promote_model` RunEvent. A plain writer (incl. the
   trainer) is NOT a validator → 403; only a `validator namespace:models` grant (or an owner) may bless.
   `candidate` = the latest version; `blessed` = the tag. The artifact janitor treats the blessed
   (possibly non-latest) version's tokens as live so promotion never dangles a pointer.
4. **Consumption is registry-optional**: serving reads the registry row → gets a plain path →
   loads it directly (no Lance reader in the serving path). Training reads Lance feature tables
   at pinned versions on the input side.
5. **Governance closes the loop**: the `models/` prefix is added to the registered external-blob
   **base allowlist** (#92 — built for exactly this), so pointers are governed; D5's rungs apply
   unchanged. Stated cost: the artifact objects live OUTSIDE the dataset directory, so GC must
   never collect them as orphans — the §9 blob-pointer-lifecycle item is now load-bearing for
   models, and crashed-run orphan artifacts under `models/<model>/<token>/` are cleaned by
   `scripts/model_artifact_janitor.py` (2026-07-11): dry-run by default, deletes only
   unreferenced-AND-past-TTL tokens with an explicit `--delete`, fail-safe direction = keep
   (unreadable registry or any unparseable meta row ⇒ report-only), and the invariant
   "registry-referenced ⇒ never collected" is unit-pinned.

Why Lance-native for the registry layer (and not MLflow/HF infra): every registry decomposes
into artifact bytes on object storage + a metadata/versioning layer; this stack already HAS the
metadata layer (Lance manifests) wired into identity (dataset name == OpenFGA object == lineage
node), atomic commits, and tags. A registry *service* would duplicate that layer with its own
DB, its own auth outside OpenFGA, and a second identity axis the lineage graph must bridge. If
rask later adopts a registry product, only the pointer targets change — the record, lineage, and
authz shapes survive.

## D5 — AuthZ: a dedicated trainer identity + rung, NOT the medallion writer rung

- **`user:service-trainer`**: `reader` on the feature stage namespaces it reads (e.g.
  `namespace:silver`, `namespace:gold`) — per-namespace, NOT warehouse-wide; and `writer` on
  **`namespace:models` only** (→ `can_create_table` there). A trainer must never be able to
  write a medallion stage, and a mover must never be able to write models — the rungs don't
  overlap.
- Seed: `warehouse parent namespace:models`, per-model `namespace:models parent table:models$<m>`
  (so humans' warehouse-reader rung cascades to models), plus the trainer grants — all in
  `scripts/seed_medallion_fga.sh` next to the mover grants.
- The trainer handler checks `can_read_data` on EVERY pinned input and `can_create_table` on
  `namespace:models` BEFORE submitting; deny → DROP before any compute is spent.

## D6 — Ray seam: shared Jobs-REST core now, KubeRay `RayJob` CR at the rask merge

- Now: extract the generic submit/re-attach core out of `submit_stage_job` (it is already
  workload-agnostic: entrypoint + env + deterministic id); training passes
  `scripts/ray_train_job.py` (baked into the ray-lance image) and SKIPS the block-poll. The demo
  tier trains a small CPU model (real features → real weights blob) proving the loop; a real
  `TorchTrainer` needs GPU nodes.
- rask merge: training becomes a KubeRay **`RayJob` CR** under Kueue (GPU quota, gang
  scheduling, long-running lifecycle owned by the operator) while stage transforms stay Ray Jobs
  REST. The trigger contract, lineage contract, and authz contract in this note DO NOT CHANGE —
  only the submit transport swaps, which is why the submit core is extracted behind one seam.
  The full seam-ownership decision (lance-ns = the agnostic Jobs-REST side; rask = the CR
  transport; deterministic submission id → CR name) lives in [`OPERATORS.md`](OPERATORS.md) §3.

## Experiment tracking, verified live (2026-07-16)

The tracking UX MLflow would provide is covered without it: the Ray train job exports its run metrics
(`lance_training_runs_total`, `lance_training_rows_seen`, `lance_training_features`, labelled by
`lance_model`) over OTLP to GreptimeDB (#18), the Perses "Model Training — experiment metrics"
dashboard renders them, and the web app embeds that dashboard at **`/experiments`** — a same-origin
BFF (`/api/experiments`) runs the dashboard's exact PromQL queries against GreptimeDB server-side, so
the browser never sees a credential. Reproducibility is captured across three stores and was verified
live on kind: the input feature versions are **pinned** in the OpenLineage run (`silver$features` at an
exact version), the model node is attributed to `service-trainer` and connected raw→…→model in the AGE
graph, the metrics land in GreptimeDB, and the Lance registry holds the versioned artifact
(model-version N == Lance-version N). No MLflow anywhere in the code (grep-clean).

## MLflow (or any registry product) — optional by design, three integration shapes

MLflow is not blocked; it was made **unnecessary for the governed loop**. If it's wanted later:

- **Shape A (recommended if tracking UX is needed): MLflow as experiment tracking, Lance stays
  the registry of record.** The job additionally logs params/metrics to an MLflow server for the
  human UX (curves, run comparison, sweeps — the part D4 deliberately does NOT cover); the
  governed publish (bytes → registry commit → lineage) is unchanged. MLflow holds no authority
  and is disposable.
- **Shape B: MLflow as the registry of record.** Possible via the external-pointer seam (the
  lineage model node points at the MLflow artifact URI through the #92 allowlist) — but it
  clashes with three pinned theses: a tracking-server backend DB is new relational
  state-of-record (against "Postgres = AGE + OpenFGA only"), OSS MLflow authz sits outside
  OpenFGA (one ungoverned surface in a fail-closed stack), and its tracking DB duplicates run
  metadata the graph can't traverse. Choose consciously, e.g. if rask arrives already operating
  MLflow as an org standard.
- **Shape C: MLflow client conventions only, no server.** The plain-path artifact dirs under
  `models/<model>/<token>/` can simply BE MLflow-format model directories (`MLmodel`,
  signatures, `pyfunc` layout) so downstream tools load them natively — zero infra.

The D4 record/bytes split is what keeps all three cheap: adopting any of them changes pointer
targets and adds a logging call, never the record, lineage, or authz shapes.

## Out of scope (explicitly)

Hyperparameter sweeps / Ray Tune (each trial = one run under a parent — needs the `parent` run
facet ingestion, parked in §9 "lineage at rask scale"); model SERVING (a consumption concern,
parked with the query engine decision); auto-retraining triggers (a `downstream`-watch automation
— future); GPU scheduling itself (Kueue's job at the rask merge).
