# DECISIONS — consolidated architecture decisions

Extracted from the retired `GOAL-prove-it.md` / `DESIGN-catalog-parity.md` progress docs so code + docs
can cite a permanent record. Those two files were goal-tracking logs; the *decisions* they contained are
still load-bearing and are captured below, one section per cited label. Headings preserve the original
labels (`P1.1`, `#38b`, `#3-A`, …) so existing citations resolve to a stable anchor here.

The two source docs recorded a much larger body of progress prose (proof logs, live-drive transcripts,
audit dispositions). Only the parts other files actually cite survive here — the durable decision plus its
rationale, not the day-by-day tracking.

---

## P0.1 — why e2e_stack.sh exists (live-verify honesty)

**Decision.** CI boots a real kind stack and runs the e2e suites (outbox, warehouses, multibase,
client-direct, CAS, governance) via `scripts/e2e_stack.sh`; a condition that cannot be proven by a grep,
a CI test, or a live assertion with a durable artifact is **not a condition, it is a claim**. The runner
additionally **fails if any test SKIPS**.

**Rationale.** Every "live-verified" claim used to rest on manual terminal runs while CI ran
`pytest -m "not e2e"`. The e2e-stack job existed but had never once gone green — a silent
`--set web.enabled=false` on a key that did not exist wedged it in `ImagePullBackOff` on every run. A CI
job that has never been green is a decoration, not a proof; and a green tick over a suite that never ran
(two suites skipped themselves on env-var name mismatches) actively buys false confidence.

## P0.2 — claim-lint (the grep-provable invariants)

**Decision.** The recurring bug classes are pinned as mechanical tests in `tests/unit/test_invariants.py`,
run in CI: no bare lineage publish bypasses the outbox (the #4 uniformity invariant), every chart-injected
env var is read somewhere in `services/`, every FGA relation the code writes/checks exists in the compiled
`model.json`, and every `--set` key our scripts pass is defined in `values.yaml`.

**Rationale.** Each of these was violated silently before it was grep-proven (3 of 4 publishers bypassed
the outbox; a `--set` on a non-existent key made an unconfigured stack *look* configured). The lint is the
writing-python T6 "test every similar case in the same change" rule mechanized, so the class cannot regress.

## P1.1 — outbox observability (the four signals)

**Decision.** The lineage outbox is an external boundary (S3 + pub/sub) and carries the four golden
signals — counters for staged / drained / poison plus gauges for outbox **depth** and **oldest-age** —
exported OTLP-direct to GreptimeDB, with a Perses alert on `depth>0` sustained.

**Rationale.** Without depth/age a leaking outbox is invisible and every durability property is
unobservable. A gauge pinned at 0 is indistinguishable from a *stuck* one, so the alert signal was driven
live (survivors staged → depth rises → relay drains → depth falls) and read back out of GreptimeDB, not
merely asserted to be emitted.

## P1.2 — bounded, oldest-first outbox drain

**Decision.** The reconcile drain caps how many staged events it processes per tick, **oldest-first**,
carrying the remainder to the next tick (`outbox_drain_limit`).

**Rationale.** The drain previously `list()`ed the entire outbox prefix into memory under the single-flight
lock, so a backlog could OOM or stall the tick. Bounding it keeps each tick's memory and work finite while
still guaranteeing every survivor is eventually drained (a unit test drains N > cap across two ticks).

## P2.1 — single-base cascade write

**Decision.** The medallion/Ray cascade writes `mode="overwrite"` to **one** root; Lance multi-base (#3-B)
stays REST-create-only and is deliberately **not** wired through the mover write path — WONTFIX, stated as
a boundary in the `compute.py` mover docstring, not an accidental omission.

**Rationale.** Base registration (`initial_bases`) is create-time-only while the cascade is overwrite-only,
so distributing it would need first-write-vs-overwrite base state threaded through the movers — and a bare
overwrite that doesn't re-send the base silently concentrates fragments in the primary root (a live proof
flaky by construction). The pipeline already distributes at the *zone* level, and no cascade stage table is
at the per-table multi-base scale. Revisit only when a real gold/training table demonstrably exceeds
single-bucket throughput or needs cross-region DR **and** the Ray distributed-write path lands.

## #16 — Dapr Workflow for silver-to-gold promotion

**Decision.** The idempotent batch legs (bronze→silver, silver→silver) need only NATS + Ray, but the
human-ordered, multi-step silver→gold **promotion** uses a Dapr Workflow (durable, resumable). Auth is
checked once at the scheduling edge (OIDC) and again per-activity (OpenFGA, token-independent), with the
verified `sub` captured as durable workflow input.

**Rationale.** A promotion is a long, human-gated, resumable sequence that must survive process restarts and
re-authorize each step independently of the original request token — exactly the durable-workflow fit,
whereas the idempotent batch hops do not warrant it.

## §7a — live-verification residuals

**Decision.** A bounded set of provenance-visibility residuals is tracked (not corruption, not blocking):
overwrite leaves stale column nodes on the reused dataset id; reconcile false-flags a *deliberately* dropped
table as `MISSING_ON_STORAGE` from a stale `source_uri`; column-level lineage is emitted as a facet but not
yet stored as graph nodes/edges. Also tracked: the governed-union live evidence predates the §7a hardenings
and wants a re-run (`make e2e-governed-union`, subsumed once e2e is in CI).

**Rationale.** These are known, bounded lifecycle-emit gaps recorded so they read as deliberate residuals
rather than unproven claims. Rename on the `dir` backend is 501 (emits nothing) — moot, not a gap.

## §9 — feature gaps (the open backlog)

**Decision.** The net-new feature backlog beyond the shipped parity work, kept visible as future work:
per-project **schema declaration** (see below), **claim-check** payload-size guards at every publish site
(P1) and facet-bloat caps for wide tables (P2), the **pointer-aware GC** posture and broader orphan-janitor
drive, the **run-INPUTS API** (a run's input version pins are reachable only via raw Cypher today —
needed for "which feature versions trained this model"), and the **multimodal residuals** below.

**Multimodal residuals** (re-pinned from the retired multimodal tracker so the deferrals stay citable —
`discovery.py`'s tier-2 pin resolves here):

- **Tier-2 content search** — Lance FTS + FLAT exact vector scan over dataset *content* (the rask
  `index_catalog.py`/`search_api` pattern — **both retired in the R6/R20 media wave**; content search
  re-lands as catalog-governed Lance tables behind `/api/explorer/search`); today's `/search` is
  metadata-only by design. Stays behind the
  measured recall gate (decision pin 2026-07-05, firnflow/lance_docs audit): default is FTS + FLAT exact
  scan with **no** ANN/IVF_PQ index on an embedding column unless recall@10 ≥ 0.95 against
  `bypass_vector_index=True` ground truth, re-measured on our stack (external BEIR data shows IVF_PQ
  recall loss grows with corpus size — never copy thresholds), normalized for `num_unindexed_rows`, with
  the query distance type asserted to match the index's training distance type first.
- **Catalog registration of cascade outputs** — the media-lane derived tables exist in lineage and on
  storage but are not registered as catalog tables.
- **Real-encoder deriver** — the shipped embedding deriver is deterministic pixel features (a demo
  stand-in, stated in `media.py`); a model-backed encoder slots in as a `_DERIVERS` plugin.
- **Additional-modality derivers** — audio/video/pdf slot into `_DERIVERS` (stated in `derivers.py`);
  none are built.

**Rationale.** Each is a real capability gap, un-built by explicit decision under the batch+training compass
(no query engine now), logged as tracked work rather than silently dropped.

## §12 — prod-hardening backlog (native switches off)

**Decision.** Several native k8s/Dapr security switches are deliberately **off** in the dev baseline,
deferred to prod in a specific fix-order: **L3 network default-deny** first (today any pod can reach the
OpenBao secret store), then **least-privilege ServiceAccounts** (~13 pods run on `default` with a mountable
API token), then **infra-pod securityContext** (the app tier is already hardened), then **Pod Security
Admission** enforcement.

**Rationale.** The don't-reinvent audit confirmed we reinvent nothing k8s/Dapr owns (zero code to delete);
these are un-flipped native switches, not missing code, and they are footgun-sequenced — default-deny egress
without a kube-dns allow bricks the cluster, and restricted PSA would reject `lineage`/`openfga-migrate`
until their root init containers are hardened. kind's default CNI ignores NetworkPolicy, so they cannot even
be validated in the dev baseline.

## #115a-c — Ray TRAIN vs Ray DATA (one platform, both workload classes)

**Decision.** The platform hosts **both** batch/ETL (the medallion cascade — the Ray *Data* shape) and
long-running **training** (Ray *Train*) as distinct workload classes with different runtime treatment
(bounded stage-transform vs fire-and-track submit+ack; RETRY vs terminal FAIL on GPU-hours; `ETL` vs
`TRAINING` jobType) but **one** provenance model, **one** authz model, **one** storage substrate. `POST
/train` gets its own topic (not a field on the stage trigger). #115a (head + topic + submit-and-ack
consumer), #115b (`ray_train_job.py` + registry publish + lifecycle lineage) and #115c (seed grants) all
landed at the unit tier.

**Rationale.** Training and ETL are genuinely different workload classes, but forking the governance /
lineage / storage model across them would be the wrong seam. Open residual: the chart values passthrough and
the live kind drive.

## blob-pointer-lifecycle GC — never collect referenced artifacts

**Decision.** GC of model/artifact objects (`models/<m>/<token>/` left by crashed runs) must **never**
collect an object still referenced by the registry; only orphaned crashed-run tokens are swept.
`scripts/model_artifact_janitor.py` ships dry-run-by-default with a `referenced ⇒ never-collected` unit pin.

**Rationale.** Pointer-aware GC is the safety property that keeps background maintenance from deleting live
data. The live drive of the broader pointer-aware posture (external-base blobs, AutoCleanupConfig-vs-sweep)
remains a §9 residual.

## schema-declaration + claim-check hardening

**Decision.** Two data-contract hardenings. (1) **Schema declaration** — movers declare `requiredColumns`;
the quality gate asserts the declared columns landed (blocks promotion, the write still commits + audits a
FAIL run) and the reconcile patrol re-checks the same declarations estate-wide, so a dropped/renamed declared
column becomes a *pre-promotion contract violation* instead of a runtime mover stall. Additive evolution is
never blocked; no declaration (default) = byte-identical gate. (2) **Claim-check** — events must be pointers,
not payloads; the train path caps config at 8 KiB (head + consumer), but a payload-size guard at *every*
publish site and a facet-bloat cap for thousand-column tables are still open.

**Rationale.** NATS's ~1 MB message bound is the physical backstop that makes claim-check a constraint rather
than a preference. Breaking-change detection is *our* item to build because Lance's manifest gives immutable
versioning but not Iceberg-style column-ID evolution semantics — the format does not give it to us.

## AGE-on-CNPG vs Lance-native-graph (the lineage-store decision)

**Decision.** The lineage graph needs the Apache **AGE** extension, but CNPG runs stock Postgres — so the
rask fold-in must pick one of: (a) point CNPG at a custom Postgres-with-AGE image, (b) keep AGE as a separate
operand, or (c) execute the pivot to move lineage to a **Lance-native graph**, which drops the AGE/Postgres
dependency entirely.

**Rationale.** This is the load-bearing pre-merge decision — it blocks the chart flip and shapes the CNPG
database list. The Lance-native-graph pivot is the option that *removes* an operand rather than adding a
custom image-build; it must be decided before/early in the merge.

---

## Control-plane vs data-plane split (the prod cut)

**Decision.** *Authorize the manifest commit and the provisioning ops; let bytes go direct to the store under
scoped, expiring vended creds — never through the server.* Four planes:

| Plane | Operations | Authorized by |
|---|---|---|
| **Admin / provisioning** | create tenant/team, create warehouse (provision bucket, register `base_uri`, stamp 2.2 + stable-row-ids), create/drop namespace, manage FGA model/tuples | platform admin (`project` / `warehouse` / `namespace` admin relations) |
| **Control / coordination** | the manifest-version commit (the single serialization point), rename, declare/deregister, branch/tag, restore, clone, credential vending, DDL | table-scoped FGA (`can_commit`/`can_promote`/…) — **authorize the commit call, not the bytes** |
| **Data** | `write_fragments` (client→bucket direct), scans/query, insert/merge/update/delete, MV refresh, blob read | data-scoped FGA (`can_write`/`can_read`) — bytes flow client↔store under vended, expiring creds |
| **Eventing** | lineage outbox → Dapr publish → consumer → AGE | trusted internal channel (Dapr → NATS) |

**Rationale.** This is exactly the Lakekeeper/Polaris cut, and the FGA model already encodes it. What a prod
control plane still lacks: a managed admin API/UI to *provision* tenants + warehouses and manage grants
(today grants are enforcement-only, no managed surface) and the physical bucket-per-warehouse to back it —
see #3-A.

## #3-A — per-warehouse bucket (physical multi-tenancy)

**Decision.** A warehouse is a runtime-provisioned, **physically separate bucket** (one tenant → one bucket;
isolation — Lakekeeper parity), provisioned + governed through an admin control-plane API, not the shared
`lance-catalog` bucket by prefix. Warehouse-create provisions the bucket, registers it as the warehouse
`base_uri`, seeds FGA (`warehouse:<id>` parent `project:<project>`, caller = owner), and stamps create-time
policy (`data_storage_version=2.2` + stable-row-ids) at the fresh-bucket boundary. Warehouse-aware routing
resolves the request's top-level namespace binding to that warehouse's rooted connection, **falling back to
the default root when unbound** (backward compatible).

**Rationale.** A dataset is self-contained under one root (relative refs), so bucket-per-warehouse needs zero
manifest surgery, and the fresh-bucket boundary is the clean seam to enforce the 2.2 + stable-row-id policy.
Shipped + audit-hardened (a CRITICAL cross-tenant takeover fixed among 5 isolation holes), live-verified on
kind (distinct buckets; table in A physically absent from B; non-project-admin 403).

## #3-B — Lance multi-base (throughput, tiering, DR)

**Decision.** Expose `data_bases` so **one table can span N buckets** via `base_paths[]` + `base_id`
(round-robin writes, fan-out reads) while staying strictly relative-path portable and governed per-base. The
security crux: `data_bases` is restricted to an **allowlist** (`LANCE_MULTIBASE_DATA_BASES`) — an off-list
base is rejected 400 — so a caller cannot point at an arbitrary bucket to exfil/write; `base_store_params`
are runtime-only (no credential persistence).

**Rationale.** This is the differentiator (the Uber pattern): Iceberg (absolute paths) and Delta (hybrid,
loses portability on shallow-clone) can't do it cleanly; Lance keeps relative-path portability **and**
multi-location. #3-B is throughput/DR/tiering and is **orthogonal** to #3-A's isolation — do not conflate the
two axes. Shipped + audit-hardened; a single small create redirects its fragment into a data base (not the
primary root) and round-robin spread grows with fragment count.

## #38b — MV-lineage is WONTFIX (no source_tables)

**Decision.** The materialized-view path emits **no** OpenLineage, and this is WONTFIX with the current code.
Do **not** fabricate an MV lineage edge from the view's own id/output_schema — that names the OUTPUT, not its
sources, a false provenance claim.

**Rationale.** The MV receives its source only as an opaque `source_query` blob the namespace server stores
without interpreting; there is no structured list of source tables to name in a lineage event (unlike the
cascade, where the source is known from mover settings). Unblocking requires **either** a SQL/plan parser to
extract source tables (the repo has none) **or** an API/contract change adding a structured
`source_tables: list[str]` alongside `source_query`. Parked until an MV consumer needs it. The governance
half is already done: `create_materialized_view` seeds FGA ownership on the `materialized_view` type.

## Lance-spec landmines

**Decision.** Format-spec constraints any catalog/pipeline code must honor (each a silent footgun):

- `enable_stable_row_ids` is **create-time-only** (silently no-ops later) → verify the `FLAG_STABLE_ROW_IDS`
  bit rather than trusting the request.
- `data_storage_version` is **immutable per dataset**; 2.2 is required for blob-v2 (why blob-create stays
  server-side / centralized).
- Secondary indices reference **row address, not `_rowid`**; compaction invalidates them
  (stable-row-id-for-index is experimental).
- The conflict matrix is **per-op**: `Append`↔`Append` auto-rebases, `Overwrite`/`Restore` do not — the
  commit retry loop must classify the error, not blindly retry.
- **Ref-plane mutations (tag/branch create) emit no version** → invisible to a version-tailing outbox.
- Implement to the **model files, not the prose** (`RenameTableRequest.new_table_name` /
  `new_namespace_id`, never `new_id`).

**Rationale.** Each is a case where the wrong assumption passes tests but corrupts a property — a wrong-version
table must be recreated, an invalidated index returns wrong rows, an un-tailed ref mutation is lost lineage.
Recorded so nobody "cleans them up" back into the trap.

## FEATURE-GAP §1 (serving) — blob serving is a governed proxy, not presigned URLs

**Decision.** Credential-less consumers (browser, notebook) fetch blob bytes back through the catalog:
`GET /v1/table/{id}/blobs?column=&row=[&version=]` streams the bytes with RFC 9110 Range support — a
`Range: bytes=…` request reads only the window from storage via the lazy `BlobFile` (206 +
`Content-Range`; 416 when unsatisfiable) — governed at reader-tier `can_read_data` like `/query`.
Deliberately a governed proxy, **not** presigned URLs: a signed URL bypasses ReBAC for its TTL.
Blob modes managed/inline/packed/dedicated (bytes copied in) always work; **external-pointer**
(`Blob.from_uri` outside the dataset root) is gated behind `vending.allowExternalBlobs` (default off —
an external object's lifecycle is outside Lance's version-aware GC) and rejected with a clean 400 when off.

**Rationale.** The catalog vends storage access; handing out a URL that answers without an FGA check for
its lifetime would punch a ReBAC hole exactly at the highest-value bytes (media blobs). Range support
keeps the proxy viable for large blobs (a viewer reads a window, not the object).

## FEATURE-GAP minor deviations #1–#7 — the spec-deviation register

**Decision.** The catalog's conscious deviations from `ns_catalog/spec.yaml`, recorded so each is a
decision rather than drift (originally the retired `FEATURE-GAP.md` §1 table; #1/#3/#5/#7 since fixed):

| # | Deviation | Spec says | Status |
|---|-----------|-----------|--------|
| 1 | ~~Path/body `id` mismatch silently overrides~~ | 400 when both present **and differ** | ✅ fixed (#43) — every body-carrying `{id}` route reconciles via `core/identifiers.reconcile_body_id`; a differing body id is a 400 (the path id is what the authz gate checked, so silently picking either is wrong) |
| 2 | Unsupported → HTTP **501** | `UnsupportedOperationErrorResponse` is **406** | body `code:0` is correct; only the HTTP status diverges (501 is arguably cleaner) — kept |
| 3 | ~~`exists` → 204~~ | 200 no-content | ✅ fixed (spec 0.9) — both `exists` endpoints return 200 |
| 4 | CreateTable ignores `x-lance-table-location` + `storage_options` | caller-chosen location/options | conscious: the catalog vends storage access (fine for single-root; a completeness gap) |
| 5 | ~~MergeInsert param set~~ | full param set | ✅ conformant since the pylance-8/spec-0.9 upgrade; residue: the FastAPI signature keeps `on` optional so the backend's own 400 answers a missing `on` (tightening would trade a spec-true 400 for a 422 — consciously left) |
| 6 | List ops omit per-request `delimiter` (`include_declared` shipped) | those params | **consciously skipped** — delimiter is deploy-fixed via `LANCE_NS_DELIMITER`; honoring it per-request would have to thread through the router-level FGA gate too (endpoint-only support would let the gate authorize a differently-parsed object — an authz-drift hazard); the native backend also cannot honor the `ListAllTables` response-joining half |
| 7 | ~~`insert` emits versionless lineage~~ | insert bumps a Lance version | ✅ fixed (GOAL 3) — `insert` reopens the dataset and stamps the real version on the WROTE edge |

**Rationale.** Each open row (#2, #4, #6) trades spec-letter conformance for a safety or architecture
property (clean 501 semantics, catalog-vended storage, authz-gate/parse coherence); recording them keeps
a future "cleanup" from reintroducing the hazard the deviation avoids.

## Gateway checks — where auth lives (2026-07-23)

**Decision.** No gateway-level authorization, ever; no gateway-level authentication today. AuthN = the IdP
issues JWTs, every service verifies signatures locally (JWKS, cached); authZ = the owning service resolves
the object (path/body/SQL → canonical id) and checks OpenFGA — the gateway routes and knows nothing.
Three planes, three answers: (1) **browser → zones (MFE):** a gateway check is *impossible* — the browser
carries the sealed session cookie only the zone BFFs can decrypt; the shared `makeSessionHandle` in every
zone IS the edge checkpoint (BFF = per-slice gateway). (2) **east–west (service↔service, sidecar
deliveries):** no gateway sees it; JWT-verify-in-service + the `dapr-api-token` delivery guard cover it.
(3) **public API plane (external clients → catalog REST with a Bearer):** *when* that endpoint exists in
prod, add a ~15-line JWT-filter policy + rate limits at the edge as a cheap pre-check — config-only, zero
service changes, services keep verifying (defense in depth).

**Rationale.** Enforcement lives where the object is known: `Check(user, relation, object)` needs the
canonical object id, which only the owning service can produce (delimiter parsing, request body, SQL plans —
a future query engine parses `SELECT … FROM db1.t` into `table:db1$t` itself; a gateway sees an opaque
string). This matches OpenFGA's guidance (Check() from the application "at the proper level"; gateway =
optional coarse layer), Lakekeeper (no authorizing gateway; pushes FGA into Trino via its OPA bridge), and
even the keycloak-openfga workshop (its gateway does route-shaped role checks AND the app still checks FGA —
gateway-PLUS-app, never gateway-instead-of-app). Adopting kgateway/Traefik/etc. therefore changes routing
objects only — zero authorization lines move. Related future adoption: an IdP→FGA tuple sync (Keycloak event
listener → `team#member`/`role#assignee` tuples) when a real IdP replaces Dex at rask-merge time; identity-
shaped tuples become event-synced, resource-shaped tuples stay app-written.

## UI-operability boundaries — what deliberately has NO browser surface (2026-07-23)

**Decision.** The planes-vs-UI completeness sweep (every mutating backend op vs its MFE surface) closed with
two lists. The following are **WONTFIX — no UI surface, by design**, each for the stated reason:
- **Credential vending** (`POST /v1/table/{id}/credentials`) — client/API-only: the browser talks through
  the BFF and must never receive S3 credentials.
- **Bare namespace create** (`POST /v1/namespace/{id}/create`) — the warehouse-**bind** flow
  (`POST /v1/warehouses/{id}/namespaces`, in WarehouseAdmin) is the governed creation path; a second,
  unbound create surface would fork it.
- **Client-direct write protocol steps** (commit, version create/delete, batch-create/commit, alter
  transaction) — internal steps of the SDK/tooling write lifecycle (#28); a browser session never holds
  staged fragments or an open transaction. Version reclamation stays governed via maintenance
  preview/run with tag-pin protection — a raw version-delete button would bypass that framing.
- **`merge_insert` / create-with-data / register-external** — Arrow-IPC bulk paths and raw-URI registration
  (SSRF-adjacent) are pipeline/SDK/operator acts; the browser data surface is append-via-insert + declare.
- **Materialized-view create/refresh** — the backend is dormant (501); prior decision
  (feedback-no-speculative-features) forbids UI on unproven capability.
- **Lineage ingest / media ingest** (`POST /lineage`, `/ingest-media`) — service-identity seams
  (OpenLineage fidelity: humans never author lineage; media ingest has no user-bearer path by design).

The 10 buildable gaps the sweep found (table drop/deregister/rename + declare-empty, row update/delete +
backfill_column, a namespace-detail page reusing GrantsPanel/policy) are **tracked in task #85** — neither
silently dropped nor silently built.

**Related tooling verdicts:** **nats-surveyor** — deferred with parked task #20; it targets
multi-cluster/$SYS observation with Grafana dashboards, while this estate is single-cluster on
GreptimeDB/Perses; the admin UI reads `/jsz` live and time-series would come from scraping the NATS
exporter into the existing stack. **NACK** — adopt when #20 unparks (CRD-managed streams replacing the
imperative nats-stream-job; pairs with clustering). The **official nats-io helm chart is already in use**
(vendored subchart nats-2.14.2). The JetStream admin panel is **read-only** and reaches NATS only through
an admin-gated BFF proxy — the browser never connects to NATS (same posture as the audit viewer's
GreptimeDB access).

## Workflow history has no browser surface — the ALERT is the surface (2026-08-26, owner ruling)

**Decision.** No zone shows how much Dapr workflow history exists, how old it is, or what the retention
policy is, and **none will**. The operator concern is carried by
`DaprWorkflowHistoryNotCollected` + `DaprWorkflowStateMetricsMissing` in `chart/alerting/rules.yml`.
Extends the 2026-07-23 UI-operability boundaries above; same reasoning, later question.

**Why.** The estate's answer to an operator concern is an alert, not a page — a dashboard rendering a
gauge nobody acts on is a surface to maintain, not a feature. The question "is retention working?" is
answered continuously by a rule; a page would answer it only when someone remembered to look, which is
precisely the state this work replaced (both measurements the estate had of workflow-history volume —
1367 rows on 2026-08-10, 7239 on 2026-08-26 — happened because a person went looking).

**What was considered and also dropped.** A per-run line on `compute/ingest/[run_id]` stating how long
THAT run's history survives given its terminal state. Defensible — it is a per-run fact on a page that
already exists, not a new observability surface — and dropped as a nice-to-have nothing depends on. If
it is ever wanted, the values are `dapr.workflowRetention` in `chart/values.yaml` (168h completed,
720h failed/terminated) and the page already renders that run's terminal state.

**Where the reasoning lives now.** `open_workflow_retention.md` was deleted with this ruling; nothing
was lost, because each durable part had already been written to where it is enforced:
`chart/templates/observability.yaml` (the policy, why there is no application-side purge, and what
actually enforces it — a per-app `retentioner` actor on a scheduler reminder, NOT the scheduler);
`chart/templates/otel-collector.yaml` (the measurement, and why the documented `wf-history-` key prefix
matches **zero** rows in the Postgres state store); `chart/alerting/rules.yml` (why the rule is on AGE
rather than volume, and why the `absent()` companion is not decoration);
`docs/runbooks/RUNBOOK-oncall.md#workflow-history-not-collected` (diagnose + purge procedure); and four
gates in `tests/unit/test_invariants.py` binding rule ↔ receiver ↔ threshold ↔ key shape.

**The lifecycle controls are now verified on BOTH paths (2026-08-26).** They shipped in `4e44584c`
proven only on the DISABLED path, because no live ingest run existed to click Terminate on. Closed by
starting one: `acme/u2verify` over `acme-bucket`, run `c146ea3b`, started through the ETL form as a
signed-in user rather than by curl.

Every link exercised in the browser: Terminate and Pause rendered ENABLED on a RUNNING run (Resume
disabled, with its reason) → click → the door's own 202 wording appeared verbatim ("further scheduling
stops, but work already in flight may still complete, so this is not immediate") → the state flipped
RUNNING → FAILED **with no manual reload**, which is the single-flight `.refresh()` doing its job → all
three buttons re-rendered disabled with correct new reasons. The run recorded `terminated by operator
with 6636 units enumerated`. Zero console messages.

**No provisioning or grant was needed, and the earlier attempt failed for a reason worth recording.**
It targeted project `demo` — which is `RASK_INGEST_SERVICE_PROJECT`, the SERVICE-token project, not a
tenant. Its bronze namespace does not exist and the signed-in user is not its admin, so the run died on
`namespace 'demo-bronze' is not provisioned` and the UI could not read it. The estate already had five
projects the user administers with provisioned bronze namespaces. The lesson is the diagnosis: two
distinct 403s (cross-project service token, and a user lacking `can_administer`) plus a missing
namespace, all reachable from one wrong project name.

**One legibility finding, not fixed.** An operator-terminated run lands in `FAILED`; the ingest model
has no `TERMINATED` terminal state (`TERMINAL = ['COMPLETE', 'COMPLETE_WITH_ERRORS', 'FAILED']`). The
REASON is honest and on screen — `terminated by operator with …` — but in a run LIST a deliberate stop
is indistinguishable from a crash without opening it. Worth a distinct state; not changed here.

## Team/role administration — WONTFIX until the Keycloak sync (2026-07-23)

**Decision.** No UI or API surface for administering the *identity-shaped* tuples — `team:<t>#member`,
`role:<r>#assignee`, `project:<p>` `team`/`member` — is built. Per the gateway-checks entry above, these
tuples become **event-synced from the IdP** at rask-merge time (a Keycloak event listener writes
`team#member` / `role#assignee` tuples as group/role membership changes in the IdP); building a manual
admin surface now would create a second writer that fights the sync from day one. Resource-shaped tuples
(warehouse/namespace/table rungs) stay app-written and already have the GrantsPanel surface.

**Interim runbook** — until the sync lands, an operator administers identity tuples with the `.localbin/fga`
CLI directly (the same invocation `scripts/e2e_stack.sh` and `scripts/seed_medallion_fga.sh` use;
`SID` = the store id those scripts resolve, api-url = the port-forwarded OpenFGA):

```sh
# put a user on a team (model.fga: team.member accepts [user])
fga tuple write --api-url http://localhost:8081 --store-id "$SID" user:alice member team:eng
# assign a role to a user, a whole team, or another role (role.assignee: [user, team#member, role#assignee])
fga tuple write --api-url http://localhost:8081 --store-id "$SID" user:bob assignee role:validators
fga tuple write --api-url http://localhost:8081 --store-id "$SID" team:eng#member assignee role:validators
# make a team own a project (project.team: [team] — members inherit project admin)
fga tuple write --api-url http://localhost:8081 --store-id "$SID" team:eng team project:acme
# revoke = the same triple with `tuple delete`
fga tuple delete --api-url http://localhost:8081 --store-id "$SID" user:alice member team:eng
```

**Rationale.** The model deliberately routes team access through roles (resource rungs do not accept
`team#member` directly — `packages/service-kit/src/service_kit/governed/auth/model.fga`), so identity administration is a *membership*
concern, which is exactly what an IdP owns. Writing it twice (manual surface now, sync later) buys a
reconciliation problem for a capability the CLI already covers.

## /streams on a medallion-off governed stack answers 503 — fail-closed, correct (2026-07-23)

**Decision.** The admin JetStream panel's BFF (`frontend/microfrontends/lakehouse/src/routes/api/
jetstream/+server.ts`) reuses the medallion produce door's side-effect-free `GET /authorize` as its
admin gate. On a governed stack with `MEDALLION_API` unset (medallion disabled), the route answers
**503 "jetstream admin authorization is unavailable"** rather than falling back to session-only auth.
This stays as-is — no fallback gate is added.

**Rationale.** Fail-closed is the correct posture: stream/consumer topology describes the whole estate's
event fabric, and answering with a weaker gate would mean "medallion off" silently *widens* who can read
it. And the configuration is hypothetical — a governed estate without the medallion admin authority is
not a deployed configuration (medallion is the cascade; every governed profile ships it). If a real
medallion-less governed profile ever appears, it must bring its own admin authority, not a downgrade here.

## CATALOG_CONTROL wildcard masking — accepted at replicas:1 (2026-07-23)

**Decision.** The /streams dead-subscription detector matches expected consumers by Dapr deliver group
(`queueGroupName` = the subscriber app-id), but the catalog's `catalog.control.v1` subscription is
deliberately **group-less** (broadcast: every replica buffers every event), so the BFF keys it as `"*"`
— *any* bound group-less ephemeral on the `CATALOG_CONTROL` stream satisfies the expected catalog entry
(`+server.ts`, the `serviceLabel` / `key` logic). Known nit, accepted: an operator's `nats` CLI
inspection consumer (also group-less, also ephemeral) can **mask a dead catalog broadcast** for as long
as it is attached.

**Rationale.** There is nothing group-shaped to match on — the broadcast semantics *require* the absence
of a deliver group, and Dapr's ephemeral consumer names are generated, so no stable identifier exists
today. The window is small (an inspection consumer detaches when the operator's terminal closes) and the
blast radius at `replicas: 1` is one refresh-hint feed whose durable record is the audit trail anyway.
**Tighten-when-it-bites:** give the catalog's control subscription a *named ephemeral prefix* (Dapr
component `consumerID`/name plumbing) and match on the prefix instead of `"*"` — do this the first time
a masked dead broadcast survives past an operator session.

## control-events — broadcast + ring buffer

**Decision.** The control-plane change-event feed (shipped + live-proven 2026-07-23,
`scripts/verify_control_events.sh`) rides a **dedicated** Dapr pub/sub component
(`catalog-control-pubsub`, topic `catalog.control.v1`) that the catalog subscribes to **without a
`queueGroupName`** — with JetStream, no deliver group means **every** catalog replica receives **every**
event (broadcast, not competing-consumer) — and with `deliverPolicy: new` on an **ephemeral** consumer:
a restarting replica does not replay retained history into its buffer, it starts fresh at the stream head.
Each replica appends events into a bounded, in-memory, drop-oldest ring buffer
(`services/catalog/core/control_buffer.py`) with a monotonic cursor and `event_id` dedupe, served by
`GET /v1/events?since=<cursor>`.

**Rationale.** The catalog has no NATS client and must not grow one (the `lineage_emit.py` no-broker-
client principle); a per-connection JetStream ephemeral consumer was rejected in the 2026-07-22 review
because Dapr subscriptions are app-level/startup-registered. The no-queueGroup broadcast is the
multi-replica-correct fan-out with zero new dependencies. `deliverPolicy=new` + ephemeral is correct
here (where it would be a bug for the cascade movers) because events are **refresh hints**, not the
durable record — the audit trail is — so replaying history into a fresh buffer would only re-announce
stale changes; a client bridging a restart just sees `reset` and re-reads authoritative state.

## control-events — per-replica cursor boundary

**Decision.** The ring buffer **and** its monotonic cursor are **per-replica** (each broadcast subscriber
buffers independently, in process memory). This is correct at the default `services.catalog.replicas: 1`.
Scaling the catalog past one replica requires **session affinity** (a client's polls stick to one
replica) **or a shared buffer** — a NATS KV-backed buffer is the natural candidate when task #20
(NACK/CRD-managed streams) unparks — otherwise a load-balanced poll hits different replicas, sees
inconsistent cursors, and degrades to noisy `reset`s.

**Rationale.** Safe-by-construction degradation: because an event is only a hint and the consumer
(`admin.remote.ts`) dedups by `event_id` and clears on `reset`, a multi-replica catalog degrades
*noisily, never wrongly* — the cost is redundant re-reads, not wrong data. Accepting the boundary keeps
the shipped feature dependency-free (no shared store) at the deployed replica count, with the scaling
path named rather than silently missing.

## control-events — estate-admin scope

**Decision.** `GET /v1/events` is gated by a real **catalog-side** FGA check of `can_observe_events` on
the fixed root object (`settings.fga_root_object` = `warehouse:lance_catalog`), an owner-tier
**platform** privilege — a mere project admin gets 403, and the client treats 403 as terminal. A
*meaningful* poll (events delivered or a reset) is audited (`event_stream_opened`); empty ticks are not,
so a 5s-polling console does not flood the audit trail.

**Rationale.** The feed is **estate-wide** — the buffer holds every project's governance changes
(broadcast subscription, no per-tenant partition) — so authorization scope must equal data scope. The
first draft's per-project `can_administer` param let any project admin read the whole estate (the #12
review fix, 2026-07-23); and the `/audit` "admin bar" precedent lived only in the BFF, so this feature
had to add the catalog-side gate itself. Honest limitation, accepted: live refresh is admin-only — the
non-admin whose *own* access just changed does not get a live refresh; the benefit is for an admin
observing the estate.

## control-events — query.live supersedes SSE

**Decision.** The originally planned P3 — a hand-rolled catalog SSE endpoint
(`GET /v1/events/stream`) — is **superseded, not deferred**: the console consumes the feed through
SvelteKit's **`query.live`** remote function
(`frontend/microfrontends/lakehouse/src/lib/admin/remote/admin.remote.ts`). The generator runs on the zone
(Bun) server — it holds the cursor, a bounded recent window, and `event_id` dedup, polls the catalog
`GET /v1/events` with the signed-in admin's bearer, and yields whenever the window changes — while the
framework owns the browser↔zone stream and reconnect (backoff + `navigator.onLine`). The zone→catalog
leg stays a plain ~5s poll.

**Rationale.** Poll-first was already the right default for a small admin audience ("refreshed within
~5s" is enough for governance changes), and the SSE upgrade carried a hazard checklist — nginx
`proxy_buffering`/`X-Accel-Buffering`, Bun's 10s adapter `idleTimeout` vs heartbeat cadence,
terminal-on-403 without `EventSource` reconnect hammering — plus a hard block on the zones being
charted. The P5 MFE migration charted the zones and `query.live` gave the browser-stream half for free,
so there is no hand-rolled SSE to build; the hazard list survives only as the streaming-config checklist
the live drive verifies (ingress no-buffer, adapter-bun `idleTimeout`).

## control-events — fail-open emit contract

**Decision.** Every control-plane mutation endpoint `await`s the emit (`core/control_emit.py`)
**after** the backend/FGA mutation succeeds — so a change that did not happen is never announced — and
the emitter **swallows every error**: a bus outage degrades to "no live refresh + the audit trail still
records it", never a failed mutation. The **audit trail is the durable compliance record**; the event
stream is only the live-notify layer, and an event is a refresh hint, never authoritative data — on
receipt the UI re-reads state through the normal FGA-governed path, so the feed can never disclose more
than the caller may already read, and a dropped/duplicated/late event only costs a redundant (or
slightly delayed) re-read. Actor is the **verified** OIDC subject, never self-asserted.

**Rationale.** This mirrors the `lineage_emit` fail-open principle: eventing must never be able to fail
a mutation. Splitting durability (audit, GreptimeDB) from liveness (bus, ring buffer) is what makes the
in-memory drop-oldest buffer and best-effort publish acceptable — nothing that matters is *only* in the
stream.

## P3b — alerting: rule logic proven hermetically; the live transport is a drill

**Decision.** (Extracted from the retired `GOAL-production-readiness.md`.) The alert rules
(`chart/alerting/rules.yml`) are *proven to fire* on synthetic series by `chart/alerting/rules_test.yml`
via `promtool test rules` (`make alert-rules-check`, in the CI test job) — a hermetic proof render-checking
alone cannot give, since a render can be valid while the PromQL never trips. The evaluator
(`chart/templates/alerting.yaml`: vmalert querying GreptimeDB's `:4000/v1/prometheus`, notifying
Alertmanager) is render-verified and gated on `observability.alerting.enabled` (on in prod). The one
deliberately-unproven piece is the **live vmalert→GreptimeDB query round-trip plus a real Alertmanager
receiver** (`webhookUrl` → Slack/PagerDuty): that needs a live cluster and remains an open prod drill.
Only the transport is unproven — the alert logic is not.

**Rationale.** Splitting the proof this way keeps the part that can regress silently (the PromQL logic)
pinned in CI, while the part that depends on a real cluster + a real paging endpoint is an explicit,
documented acceptance step instead of a pretended green.

## P4/P7 — backups + structural SPOFs: the prod answer is externalize, not in-chart HA

**Decision.** (Extracted from the retired `GOAL-production-readiness.md`.) The two big structural SPOFs —
RustFS and AGE-Postgres single-replica — are deliberately *not* solved in-chart: that would need an
object-store operator / CloudNativePG, the same class as the parked items. The chart instead wires the
handoff — `rustfs.externalEndpoint` / `age.externalHost` — and `prod-render-check` leg 10 asserts the
RustFS handoff is atomic with the GreptimeDB object-store endpoint (either both set or neither). The
AGE-on-CNPG path is documented and proven (docs/CNPG-AGE.md; CNPG physical PITR supersedes the pg_dump
path). Adopting either = flip the value.

**The open backup gaps that follow** (accepted loss windows until externalized; operational detail in
docs/DURABILITY.md + docs/runbooks/RUNBOOK-restore.md):
- the pg_dump lands on RustFS, so a total RustFS loss loses both the Lance data *and* the DB dumps
  (fate-sharing) — ship the dumps off-cluster, or externalize to CNPG PITR;
- the OpenBao file-backend PVC has no backup path (back up the unseal material out-of-band);
- a documented RPO/RTO and verification that the VolumeSnapshot actually succeeds (the empty
  `snapshotClassName` is a per-cluster value) are still owed;
- lesser SPOFs stay documented, not fixed: the movers' single-flight lock is process-local (caps each
  stage at 1 mover; a distributed lock is parked until throughput demands it), and Dex is a
  single-replica in-memory IdP (externalize for prod).

## Medallion tiers — hybrid physical layout (2026-07-24)

**Decision.** The medallion tiers get a **hybrid** physical layout per tenant: **raw/bronze/silver are
namespaces** (prefixes, `<work-root>/medallion/<stage>`) inside the tenant's **work** warehouse, while
**gold is a separate per-tenant SERVING warehouse** — a normal registry record created through
`POST /v1/warehouses` with the optional `"serving": "gold"` field (only `"gold"` is accepted for now;
absent = a work warehouse). `common/warehouse_registry.py` resolves the two classes independently:
`project_root` matches only work records, `project_gold_root` mirrors it matching only
`serving == "gold"` records (same lowest-id determinism, same TTL cache, partitioned by class — so
registering a gold warehouse can never hijack stage routing via the lowest-id rule). Behind
`MEDALLION_GOLD_WAREHOUSE_ENABLED` (chart `medallion.goldWarehouse`, default false, rendered ONLY onto
the terminal silver→gold mover), a tenant trigger's **target** root becomes the project's gold root when
one exists; absent gold warehouse or flag off → byte-identical work-warehouse behavior, and the
projectless path never retargets.

**Rationale.** Three forces pick the split point at gold, not "every stage its own bucket" or "all
prefixes":

- **Consumer blast-radius.** Gold is the tier external consumers read; raw/bronze may hold unvetted or
  PII-bearing data mid-scrub. A consumer read credential scoped to the gold **bucket** (bucket-level
  cred scoping is what object stores do well) can never traverse into raw/bronze the way a
  prefix-policy mistake on a shared bucket can.
- **Lifecycle/storage-class separation.** Serving data wants different retention, replication and
  storage-class policy than scratch stages; object stores apply those per bucket.
- **The recorded gold-sink intent.** The data-zone architecture note already records gold as an
  external SINK zone; a per-tenant serving warehouse is that intent expressed through the existing
  warehouse control plane instead of a new mechanism.

Interior stages stay prefixes because they share one producer/consumer (the movers), one lifecycle, and
one FGA cascade — separate buckets there would triple the per-tenant provisioning surface for no
isolation gain (the movers hold one credential either way).

**FGA.** The gold warehouse is a **normal `warehouse:` object** with the standard `project project:<p>`
parent tuple (seeded by warehouse-create like any other) — so project grants cascade into it naturally
and consumer read grants scope to `warehouse:<gold-id>` alone; no new FGA type, relation, or seed shape.
The `<p>-gold` namespace tuples from the per-tenant enablement seed (`seed_medallion_fga.sh <p> <zone-wh>`)
are unchanged: lineage/FGA identities are project-qualified names, not roots, and only the physical
target root moves.

## Runner deployment — the CPU-viable subset is real, the rest is an honest GPU list (2026-07-24)

**Decision.** Of the folded `runners/` tree (the lance-audio model homes), exactly one runner deploys on
this GPU-less estate: **`runners/assist`** — a new ONLINE FastAPI model server (its own sealed env +
committed `uv.lock` + its own image, `.docker/assist-runner.dockerfile`) serving the annotator's
`MEDIA_ASSIST_URL` contract with **real CPU inference**: GroundingDINO-tiny (open-vocabulary text-prompted
detection, ~2.5 s/frame) + SAM-ViT-base (box/point segmentation → simplified polygon, ~1.8 s/frame).
Weights are baked into the image at build (HF cache layout, `HF_HUB_OFFLINE=1` at runtime); frames are
fetched from the viewer service only (relative `image_url` joined to `ASSIST_FRAME_BASE` — absolute URLs
rejected, no SSRF surface). The chart gains a `runners.enabled` flag (default **false**) rendering the
assist Deployment/Service (`component: assist`, appProbes, its own `resources.assist` tier — the default
request-pod tier would OOM a warm two-model torch process) and, on the annotator only, `MEDIA_ASSIST_URL`
→ the assist Service. Because the assist wire payload carries no `producer` field, the server routes by
what the user gave: prompt ⇒ detection (region narrows to a crop), region-only ⇒ segmentation (click ⇒
point prompt). `MEDIA_JOBS_URL` renders only when `runners.jobsUrl` is explicitly set — no batch deriver
exists yet, so the annotator keeps its honest submit/poll mock rather than a fake queue.

**The GPU-needed list (not deployable honestly on this box).**

- `asr` (whisper-large/wav2vec2, torch **cu128** pins) — CUDA env by construction; corpus is already
  transcribed, so a degraded CPU deployment would also be pointless.
- `diarize` (pyannote community-1, cu128) and `voiceprint` (WeSpeaker via pyannote, cu128) — same CUDA
  envs; offline Ray Data actors, not online services.
- `topics` (Toponymy) — CPU-tolerant clustering but requires live LLM endpoints (namer + embedder) that
  do not exist on this estate; also corpus-global batch (its own actor.py refuses per-batch use).
- `kg` (LightRAG) — needs an OpenAI-compatible LLM; batch pipeline, not a service.

**Rationale.** The assist seam is the one runner-shaped gap a 64-core GPU-less box can serve for real —
interactive single-frame inference where seconds-per-frame is acceptable — and it converts the annotator's
in-repo mock into live model predictions with zero annotator code change (the mock/remote seam was built
for exactly this drop-in). Everything else in `runners/` either hard-pins CUDA wheels or depends on LLM
serving we don't run; deploying those as CPU stand-ins would be the speculative-feature anti-pattern
(claiming a capability the estate cannot exercise). The subset boundary is therefore *honest by
construction*: real half deployed and live-proven, GPU half recorded here as the merge-time backlog.

## Ingest orchestration — Dapr Workflow IS adopted; the estate is event-driven now (2026-08-03, owner ruling)

**Superseded the same day by the owner, and the reason is not a flaw in the analysis below — it is
that the analysis answered the wrong question.** The entry that follows argued the `OPERATORS.md` §4
reopen criterion (*"a step that cannot be made idempotent by any caller-chosen key"*) and concluded
correctly that the ingest run has no such step. The owner's ruling: that criterion belongs to the
orchestrator-and-polling era it was written in. The estate has since gone event-driven with Dapr
Workflow, so "can we avoid an engine?" is no longer the question being asked — the engine is the
estate's chosen shape for durable multi-step work, and ingest is multi-step durable work.

**Ruling: Dapr Workflow orchestrates the ingest run.** `dapr-ext-workflow` is added; the workflow
owns run lifecycle (enumerate → dispatch chunks → await drain → finalize → emit), executing in the
daprd sidecars the estate already runs, on the actor state store that already exists
(`dapr-statestore.yaml:62`, `actorStateStore: "true"`) with `ingest` already scoped to it.

**And it dissolves the per-unit ledger.** The design below leaned on `packages/tracker` — a per-file
transfer ledger harvested from ra-hcp — as the unit ledger. With Dapr Workflow adopted, three
existing mechanisms cover what it was for, and none of them is a side ledger:

| question | answered by |
|---|---|
| delta *between tiers* (bronze→silver→gold) | **Lance CDF** — `_row_created_at_version` / `dataset.delta`, verified working in `open_ingest.md` §7.11 row 2. This is D4's own ruling: *"Delta bookkeeping is data, not state… never a side ledger"* |
| which units are still outstanding *during* a run | **JetStream `WorkQueuePolicy`** — a consumed-and-acked message is gone; un-acked redelivers. The stream IS the outstanding-work set |
| the fragment list to commit at finalize | **Dapr Workflow fan-out/fan-in** — chunk activities return their `FragmentMetadata` durably, replayed after a crash |

The plan's objection to activities ("millions of persisted+replayed activity results would melt the
state store") is answered by CHUNKING, which it already prescribed: a child workflow per ~1–10k keys
returns one compact result, not a million. Once chunked, the workflow's own durable state is the
ledger. `packages/tracker` therefore gained no NATS-KV backend and never acquired a consumer.
**The cleanup call was made 2026-08-30 (owner): the package is DELETED** — src, tests, its root
workspace rows, its `make tracker-postgres` / `dagger call tracker-postgres` pair, and `sqlmodel`,
`sqlalchemy` and `pytest-postgresql` out of the root lock with it. Nothing here needs resurrecting:
JetStream `WORK_QUEUE` retention plus the workflow's durable state ARE the ledger.

Residual to place when the run is built: a POISON unit is acked (so gone from the stream) yet must
still surface as `error` in run status. It rides on the chunk activity's returned result plus the
DLQ entry — not on a reinstated ledger.

---

### Superseded reasoning, kept for the trail — "Dapr Workflow stays un-adopted" (2026-08-03)

`open_ingest.md` proposed Dapr Workflow as the ingest run's orchestrator ("estate-native, zero new
infrastructure") and recorded it as a resolved open decision. It was not resolved: it contradicted
`docs/OPERATORS.md` §4, which rules **Dapr Workflow stays un-adopted**, was re-examined against the
annotation publish saga and **upheld 2026-07-28**, and is repeated at `chart/values.yaml:1142`. That
section carries an explicit reopen criterion, so the question is not whose document wins but whether
the criterion is met.

**The criterion, verbatim:** *"If a multi-step path appears whose steps cannot be made idempotent by
any caller-chosen key — the honest signal is finding yourself wanting to generate an id mid-saga — the
argument above stops applying and a workflow engine earns its dependency."*

**Ruling: the ingest run does not meet it. The pin stands.** Every step is idempotent by a key the
caller chose or the source supplied, and nothing is minted mid-run:

| step | key | why it converges |
|---|---|---|
| run identity | the caller's `Idempotency-Key` → `run_id_for("<project>-ingest-<key>")` | a retry resolves to the same run resource |
| unit task | the unit key from source enumeration | tracker `done_keys()` skips completed units; redelivery is a no-op |
| fragment write | the unit key | the tracker keys `FragmentMetadata` by unit key — it must anyway, because pre-commit fragment ids all collide at 0 |
| finalize / commit | `id = stable hash(source_uri)` | `merge_insert` re-run measured at 0 inserted / 5 updated / 10 rows (open_ingest.md §Empirical) |
| publication | the `published` tag | a tag advance is idempotent; a repeat fires one event that E2 absorbs |

The "honest signal" never fires. Contrast the publish saga, which genuinely had a
not-naturally-idempotent step (creating a table) and was saved only by minting
`pending_publish_id` at the transition and deriving the table id from it. Ingest has no equivalent:
its row identity is a hash of the source URI, which exists before the run does.

**The strongest argument for adopting anyway, and its answer.** A workflow buys a durable
fan-out/fan-in with an external wait — dispatch N units, suspend on `drained`, finalize exactly once —
and the honest gap it addresses is **enumeration**: if the API pod dies at unit 5,000 of 10,000, the
remaining units were never published, and unlike every other step that failure is not a message
anything will redeliver. But the fix is chunking, not an engine: publish enumeration itself as
work-queue messages (a chunk per ~1–10k keys, which `open_ingest.md` already describes as the
workflow's own shape), and a dead pod becomes redelivery like everything else. Nor is
"exactly-one finalize" load-bearing — finalize is `merge_insert` on a stable id, so at-least-once
finalize converges; the drained event is an optimisation over the fallback timer, not a correctness
requirement. A workflow engine would buy convenience here, and convenience is not the criterion.

**Consequences.**
- `open_ingest.md` §7.6 is overturned; its §0 C13 already flagged the contradiction. `dapr-ext-workflow`
  is not added to any `pyproject.toml`.
- The ingest run is orchestrated by the estate's existing parts: a JetStream durable work queue for
  units *and* for enumeration chunks, `packages/tracker` as the unit ledger, last-worker CAS on the
  remaining-counter publishing `ingest.run.<id>.drained`, and one dead-man timer — the single
  in-process timer A13 permits.
- **`stateStore.scopes` therefore does not gain `ingest` for workflow state.** The plan's Phase-0
  condition assumed adoption. If the ingest service ends up owning no Dapr state at all, scoping it
  would be dead config of exactly the kind `chart/values.yaml:776-777` exists to prevent.
- Reopen if enumeration chunking proves insufficient in practice, or if a later hop — silver→gold
  quality promotion is the candidate OPERATORS.md itself names — needs per-attempt run identity.

---

## The outbox is application-side, and Dapr's transactional outbox cannot replace it (2026-08-15)

**Decision.** `service_kit.lakehouse.outbox` — stage the event to object storage, publish, drop on ack,
relay any survivor — stays. Dapr's transactional outbox is not an available alternative, and the
proposal to reach it by "writing a transactional marker to Dapr state after the Lance write" does not
work. Recorded here because the working notes that posed it as an open question are deleted, and a
decision must outlive them.

**Why Dapr's cannot apply.** Its outbox is a property OF A DAPR STATE STORE: it publishes the message
inside the transaction Dapr is already running, which requires the write to go through Dapr's
transactions API into a transactional state store. The docs scope it exactly that way and add that the
guarantee stops at Dapr's own API boundary — *"direct queries of the state store are not governed by
Dapr concurrency control … Writes should be done via the Dapr state management or actors APIs."*
rask's authoritative writes do not go through it: a Lance dataset is written by the Lance library, and
`public.lineage_events` + the AGE graph by lineage's own `psycopg` pool. No state store in the estate
carries `outboxPublishPubsub`/`outboxPublishTopic`, and setting them would change nothing, because
Dapr is never asked to perform those writes.

**Why the marker variant does not rescue it.** A marker written to Dapr state AFTER the Lance commit
leaves the identical crash window: the commit succeeds, the process dies, no marker is written and no
event is published. It RELOCATES the gap rather than closing it. The same flaw kills the sibling
proposal to put cascade triggers behind Dapr Workflow — Workflow's durability begins only once the
schedule call RETURNS, so `write → start workflow` carries the same window as `write → publish`, and
the architecture page separately warns Workflow "may not be appropriate for latency-sensitive
workloads".

**The bound that makes all of this a trade rather than a bug.** Atomicity between object storage and a
message broker does not exist — no distributed transaction spans "commit a Lance dataset to S3" and
"publish to NATS", in Dapr or anywhere. So the goal is not atomicity; it is NO SILENT LOSS: every gap
either closes itself or announces itself.

**What the application-side outbox actually buys, stated without overclaiming.** It shrinks the window,
it does not close it: `stage_event` runs AFTER the Lance commit, so a crash in the commit→stage gap
still loses the event. That ordering is deliberate — staging after the commit means every surviving
object is a real committed write, so there are no phantom events. The trade bought *no phantoms* and
paid with *a small gap*. It is also not "transactional" in Dapr's sense and should not be described as
such.

**The long-term direction, if this is ever revisited.** A commit-EMBEDDED record: Delta CDF commits
change data inside the transaction log, and Lance already writes a per-commit transaction file. An
event derived from the commit cannot diverge from it. Today's staged object is a defensible interim.

---

## Helm release storage: the SQL driver stands; the chart is NOT split (2026-08-15)

**Decision.** Keep `HELM_DRIVER=sql`. Do not split the chart into infra + app now.

**What forced the choice.** `helm upgrade` began failing outright:

```
Secret "sh.helm.release.v1.rask.v35" is invalid: data: Too long: may not be more than 1048576 bytes
```

Helm embeds the WHOLE CHART in every revision, and ~880 KB of `chart/charts/*.tgz` is
already-compressed subchart archives that gzip cannot shrink further. Measured: v28 964 KB → v34
1,046.9 KB → v35 1,048.5 KB against Kubernetes' 1,024 KB object limit. It had been creeping for
months; v35 simply crossed it. Nothing could be deployed — not `make k3s-up`, not CI.

**Four alternatives were measured and rejected, so nobody re-tries them:**

| attempt | result |
| --- | --- |
| convert rendered YAML comments to Helm template comments | broke the render TWICE — ate content inside block scalars (`- \|`, ConfigMap SQL). Reverted both times |
| drop an unused subchart | all ten are genuinely enabled |
| unpack `charts/*.tgz` so gzip can compress them | packed 871.9 KB vs unpacked 882.8 KB — **costs 11 KB** |
| `kueue.enabled=false` | buys 184 KB, and Kueue is provably idle (0 workloads; the only `queue-name` reference in the repo is a comment) — but deletes an operator installed on purpose, to dodge a storage limit, at the wrong layer |

**Why not split the chart now.** It is the architecturally correct answer and remains the intended end
state — infra (operators + CRDs, installed rarely) separated from app (upgraded constantly) would put
the app release back under the Secret limit and delete the operational cost below. It is also a large,
risky change to the one artefact that deploys both local k3s and production, and the SQL driver
removed the blockage in one command with zero resource churn.

**The cost this accepts, stated plainly.** Every `helm` invocation must carry `HELM_DRIVER=sql` and the
DSN, or Helm reads the empty Secret backend, reports the release ABSENT, and `upgrade --install`
re-installs over a live estate without erroring. That is mitigated — not removed — by routing every
call through `scripts/helm.sh`, which fails closed when it cannot resolve the address rather than
falling back. CI still needs the same treatment.

**The trigger that reopens this.** Split the chart when any of these holds: CI needs to deploy and
cannot carry the driver config; the Postgres holding the release becomes a dependency for a
disaster-recovery path that must not require it; or a second estate needs the chart and inherits the
env-var contract. Until then the driver is the answer and the split is a known, costed improvement.

## Lineage records what happened to DATA; an authorization denial is not a data event (2026-08-16)

**Ruled** while closing the notifications coverage register, which proposed emitting an OpenLineage
FAIL when a medallion mover is denied by FGA (`transform.py`'s `medallion_stage_denied` path). It
should not, and the existing behaviour — drop, count, log — is right.

**An OpenLineage `RunEvent` describes a RUN of a JOB over DATASETS.** `FAIL` asserts that a dataset's
production was attempted and failed. When the mover is denied, nothing is read and nothing is
written: no data is touched. A FAIL there mints provenance for a non-event, and a graph that records
runs which never ran answers every later question wrongly — *"where did version 7 come from"* and
*"what has touched this table"* both degrade, which is the one thing provenance exists to answer.
Gaps in provenance are recoverable; fiction in it is not.

**A denial is also a STEADY STATE, not an incident.** A mover that is permanently un-granted would
emit a FAIL on every trigger, forever, turning the provenance graph into an alert stream. The
observability rule is the ordinary one: a repeating operational condition is a METRIC, not an event —
bounded cardinality, alertable, cheap. The estate already does exactly this
(`record_denied` -> `_stage_denied`, labelled by transition, plus a `medallion_stage_denied` warning),
and `test_mover_denied_when_not_authorized` pins the silence deliberately.

**The user-facing gap the proposal was really about is REAL, and belongs on the CONTROL lane.** The
person who started a cascade should learn that it stopped. But "your run was blocked because a mover
lacks a grant" is a GOVERNANCE fact about a principal, not a fact about data — the same distinction
that keeps grants off the lineage lane today. It names a person, so it is a `NAMED_ACTIONS` control
event, and `lance.originator` (added 2026-08-16) is the identity that makes it addressable at all.

**The line, stated once:** lineage answers *what happened to this dataset and who produced it*;
the control lane answers *what changed for this person*; metrics answer *how often is this
happening*. A fact that names a principal and touches no data is never lineage.

## The publication verdict rides the run FACET, not the inbox pointer (2026-08-16)

**Decision.** A refused publication is stamped on the terminal run event's `lance` facet
(`published` / `publish_reason` / `publish_error`). The inbox ROW that a person sees keeps saying
"Complete", and making it say otherwise is DEFERRED — deliberately, not by omission.

**What was actually broken.** `finalize_run` has always computed the verdict correctly, and the run's
own page has always rendered it ("Committed, but not published — &lt;reason&gt;"). It died crossing one
boundary: `RunOutcome` is a plain `BaseModel`, so pydantic's default `extra="ignore"` silently
discarded every publication key at `emit_terminal`'s `model_validate`. What reached the graph was
byte-identical to a run that published. That is worse than silence — `RunListResponse` names the graph
as the authoritative run history, so the durable record asserted the opposite of what happened. Fixed
by DECLARING the fields and forwarding three of them onto the facet the originator already rides.

**Why the event TYPE does not change.** A refused gate is a COMPLETE run whose DATA was declined: it
fetched, wrote and committed exactly what it was asked to. The medallion's `/bronze-arrival` head
fires only on `eventType == "COMPLETE"`, so reporting the refusal as a FAIL would cancel the entire
bronze→silver→gold cascade for a run whose rows are committed and durable — and would lie in the other
direction about the run itself. `published` is TRI-STATE for the same reason: `None` means no version
existed to gate, which is not a refusal, and collapsing it to `False` would report a gate that never
ran.

**Why the BELL is deferred.** The row's label is derived from the `@STATE` suffix of
`notification_id`, and the pointer carries no other phase information. Both ways to change that are
worse than the nuance they buy:

- **A field on `InboxPointer`** is durable actor state under `extra="forbid"`. That is the exact
  surface that took the whole inbox down on 2026-08-16 — a rollback turned unreadable rows into
  `InboxUnreadable` and a 503 for every notification the subject had, because list validation is
  all-or-nothing. A label nuance does not justify re-entering that class of risk.
- **A new state suffix** would fork the `(run_id, event_type)` dedupe identity the pointer shares with
  lineage's feed, giving one run two notification ids.

The pointer is a claim-check by design: it names the object and the REASON to look, never the outcome
detail. The person is already told about this run (ingest stamps `lance.originator`), and the run's
own page is where the verdict is read. What this change creates is the FACT on the wire — which did
not exist at all before, and which any future inbox work must read.

## Watch enrolment does not wait for the `platform.rask.io` CRD (2026-08-16)

**Decision.** The notifications page reads its project list from `me.projects`, not from the
controlplane. The `platform.rask.io` Project CRD is **ABANDONED as a rask-repo concern** — it lives in
the separate `rask-operator` repo, and landing the CRD here alone would be a regression rather than a
fix.

**What was broken, and it was worse than "no list".** The page loaded projects with
`getProjects()` → `/api/projects/` → controlplane → the Kubernetes API. (This paragraph named
`/capi/v1/projects` until 2026-08-29; that path proxies to the CATALOG and was never the one at
fault — `@rask/api`'s deleted `projects.ts` fetched `/api/projects/`, the gateway's controlplane
row.) Verified live: `kubectl get crd` returns 71 CRDs and none is `projects.platform.rask.io`, so
an in-cluster `GET /api/projects/` answered `503 {"detail":"cannot reach kubernetes api"}` — since
2026-08-29 it answers `501` naming the unregistered type instead, which is the same refusal told
truthfully. `onMount` awaited
`Promise.all([readWatches(), getProjects()])`, so that 503 rejected the pair, `watches` stayed `null`,
and the page rendered *"Watching is unavailable on this stack… the notification service is not
reachable"*. An outage in the CONTROLPLANE, reported as an outage in NOTIFICATIONS, on the one surface
a person uses to turn watching on. WATCH is one of the plane's targeting sources and its **only**
enrolment door, so the estate could not enrol anybody — for a reason it named wrongly.

**Why the CRD is not the fix.** Installing the CRD without its controller gives unreconciled CRs, which
render exactly like a project stuck mid-provision — trading an honest 503 for a page that lies more
quietly. The controller is not in this repo and cannot be. Meanwhile the identity plane already
answers the question the page is asking: `/v1/me` carries `projects`, it is the frozen contract every
zone's layout already resolves, and it is what `$lib/gallery.ts` reads for the project gallery. The
page was asking the wrong service.

**The third state comes with it.** `me === null` means BOTH "signed out" and "signed in but `/v1/me`
did not answer", and those are opposite situations: one is fixed by signing in, the other cannot be.
`hasSession` separates them, exactly as `gallery.ts` already does, so a user whose identity the catalog
could not confirm is told that — not sent round a sign-in loop that cannot help.

This does not close the OTHER half of the controlplane gap: a watch is still keyed by a project id
whose namespace nothing joins to the FGA tenant id. That remains open and is not addressed here.

## The compute service gets no emitter yet, and the blocker is identity (2026-08-16)

**Decision.** `services/compute` stays a read-only observability surface. A Ray job reaching FAILED
still notifies nobody, and that is DEFERRED with a stated blocker rather than left as an open gap
nobody named.

**Why not now — the blocker is not effort.** Every route in the service is a GET
(`compute/routes.py`), it publishes nothing, and `ray-kit` has no emitter. Adding one is mechanically
possible. What makes it pointless today is that **compute holds no identity for any job**: `RayJob` has
no author field, so an emitter here would produce events naming nobody — undeliverable by the plane's
own rule ("a state change that names nobody is not under-delivered, it is UNDELIVERABLE"), and the
notification plane would correctly discard every one of them. Building a producer whose output the
consumer is designed to drop is worse than building nothing, because it looks like coverage.

**The two lanes that DO have an identity already emit**, which is why this is a narrow gap rather than
a hole: a medallion stage job is watched by `medallion.workflow`, which emits a FAIL carrying
`lance.originator`; a training job emits its own OpenLineage lifecycle carrying the same field. Both
were closed on 2026-08-16. What is left is jobs submitted OUTSIDE those doors — by hand, or by a
KubeRay lane whose `runners/htr` emits no lineage at all.

**What would make it worth building, stated so the condition is checkable.** A submitter identity on
every Ray job, not just the medallion's. The mechanism now exists and is proven — Ray's own `metadata`
carries `rask.originator`, readable from `GET /api/jobs/<id>` after the job has died — but it is
stamped only by `medallion.services.ray_submit`. Requiring it at the CLUSTER's door (refusing a
submission that names no originator) is the change that turns compute into a producer worth having.
Until then an emitter here would need a terminal-transition detector and durable "already reported"
state — a new stateful surface in a service whose whole design is that it holds none — to publish
events addressed to no one.

## The bell cannot carry the publication verdict, and the reason is the claim check (2026-08-16)

**Decision.** The inbox row for a refused publication keeps reading "Complete". Not deferred on cost
this time — **REFUSED on the plane's own invariant**, which the earlier deferral note got wrong.

**What the earlier note said, and why it was insufficient.** It framed this as a compatibility hazard:
`InboxPointer` is `extra="forbid"` over a list validation, so a field an older build cannot parse
raises for the whole page — `InboxUnreadable`, a 503 on the entire inbox. All true, and it is why a
staged two-deploy rollout was proposed. But a staged rollout only answers *when* you may add the
field. It does not answer *whether* you may.

**The answer is no, and two of the estate's own adversarial tests say so.** Attempting phase A —
declaring `published: bool | None` and writing it nowhere — failed
`test_a_stored_record_carries_no_field_a_reader_could_not_already_ask_for` and
`test_a_stored_pointer_names_no_field_the_event_did_not_have_to_earn`, which pin the stored field set
exactly and state the rule: *"a row is an id, a reason, ONE object name, a run link, a lane sequence
and an instant — adding anything read off the payload would make this store a second, ungoverned copy
of lineage."* `published` is read off the payload (`lance.published`). The module docstring is the same
rule stated positively: **pointers, never payload copies** — the body is fetched at render time
through the governed path, *"which is what keeps a revoked grant from being readable out of somebody's
inbox"*.

That last clause is the concrete harm, not an abstraction. A stored `published` flag is a fact about
DATA sitting in per-subject state that no governed read gates. After a revoke, the subject would still
be able to read "that table's publication was refused" out of their own inbox — precisely the leak the
claim-check design exists to prevent.

**So the honest options are two, and neither is the field.** Either the bell ENRICHES at render time,
fetching the verdict from lineage under the `can_get_metadata` check the render path already runs —
architecturally correct, and an N+1 on the inbox render that has to be designed rather than slipped in
— or the row keeps saying "Complete" and the run's own page carries the verdict, which it already does
correctly ("Committed, but not published — <reason>"). The durable graph is honest as of 2026-08-16;
what remains is only how loudly the bell says it.

**The generalisable rule:** when a guard refuses a change, check whether it is refusing the TIMING or
the IDEA. A staged rollout answers the first. Nothing answers the second except a different design.

---

## Comments carry rationale and provenance, never a changelog of the prose (2026-08-30, owner ruling)

**Decision.** Module prose sorts into three kinds. Two stay; one is banned in NEW code and left alone
where it already stands.

| Kind | Example | Verdict |
| --- | --- | --- |
| **(1) RATIONALE** — why the shape is load-bearing; the invariant; the failure the design avoids | *"SORT AND CAP FIRST, VALIDATE SECOND. The old order — validate every job, then sort — built a list of every job in the cluster before the cap could apply."* | **STAYS.** This is the most valuable prose in the estate. |
| **(2) PROVENANCE** — a dated measurement, a named pin, an owner ruling with its date | *"Measured 2026-08-26: the ray-cluster export alone takes 238 s."* / *"Pinned by `tests/unit/test_invariants.py::test_…`"* | **STAYS, and keeps its measurement.** Stripping the date turns a fact into a claim. |
| **(3) HISTORY OF THE PROSE** — a comment whose subject is a previous comment | *"This docstring used to say the lookup was cached."* / *"The comment above claimed the lock was held here, which was wrong."* | **BANNED IN NEW CODE.** |

**The ban is FORWARD ONLY.** The 35,279 prose lines already in `packages/` + `services/` are not to be
drained. A retroactive sweep is a large unreviewable diff whose most likely casualty is exactly the
kind-(2) measurements the prose exists to preserve, and the estate has no way to review a diff that
size line by line.

**What replaces a kind-(3) comment.** The estate already has a rule for prose a change falsifies:
**rewrite the sentence, do not append to it** — a correction bolted on below leaves two claims standing
and readers take the wrong one. Kind (3) is what that rule produces when it is half-applied: the old
sentence gets annotated instead of replaced. So the fix is always the same — state what is true now.
If the *past shape is the reason* for the present one, say that about the **code** ("the old order
built the whole list before capping"), never about the **comment** ("this line used to say").

**Rationale.** Measured 2026-08-30 with a tokenize pass (comment + docstring lines against non-blank
code lines) at `621cd1a8`: `packages/` + `services/` source is **46.1% prose** (35,279 / 76,471), and the
`services/ingest` scope the audit flagged is **60.8%** across its seven core modules — up from the 60%
measured 2026-08-28 and the 52% measured 2026-08-24. Quote that figure with its scope attached: the
whole of `services/ingest/src` is **57.0%** (4,656 / 8,174 over 29 files), and "ingest is 61%" is true
only of the seven modules the audit named, not of the service.

Kinds (1) and (2) earn their share: this estate's recurring defect is a claim nobody can check, and
a dated measurement or a named pin is the cheapest possible check. Kind (3) earns nothing. It is a changelog of a file that already has one — git — and
it grows monotonically, because every correction adds a layer without removing the one beneath it.

**The gate, and its honest limit.** `scripts/comment_history_gate.py` checks **added lines only**
(`--staged` in `prek.toml`, `--base <ref>` in CI). It is diff-aware by design: a whole-tree lint would
be red on its first run against prose nobody is permitted to change, and a gate that is red by
construction is one everybody learns to ignore.

It fires on exactly one thing — **prose whose subject is prose** ("used to say", "no longer claims",
"this docstring was wrong"). That form is mechanically safe to ban because deleting it loses nothing
about the code.

It **cannot** catch the other half of kind (3), a changelog of past *code* decisions, and does not try.
Nothing in the wording separates these two:

```python
# The old order — validate every job, then sort — built a list of every job before the cap.   # KEEP (1)
# Sorting moved above validation on 2026-08-12 when the profile came back.                    # BAN  (3)
```

Only a reader can tell them apart, so they stay a review concern. **Passing the gate is not
conformance with this rule** — it is a floor under it. Its advisory whole-tree count
(`--report`) exists to measure the standing population, never to gate a change.

**Its measured error rate, so nobody has to take the above on trust — and this figure was wrong the
first time it was published.** `--report` over every tracked gated file on 2026-08-30 returns **53**
lines. The original ruling said "52 are genuine, one is a false positive"; an adversarial re-read of
all 53 found **at least two** false positives and a third that is right for the wrong reason:
`ObjectBrowser.svelte:50` ("the only sentence that said what was actually wrong" is the server's
problem+json detail, not a comment); `models/e2e/shell.spec.ts:14` (a present-tense statement of what
the test asserts about the UI — `no longer` alone carried the match); and `test_publish_saga.py:491`,
where the flag is defensible but the subject is a fixture literal, so the gate's stated reason ("a
comment whose subject is a previous comment") is false.

Budget roughly **one in twenty**, and treat that as a FLOOR rather than a measurement: separating
kind-(3) history from kind-(1) rationale is exactly the reader's judgement the gate cannot make, which
is why it is advisory and forward-only. None of the three is worth chasing — each narrowing overfits
the pattern to one site, and forward-only the gate never reads these lines again.

Recording the correction rather than quietly restating the number, because a gate that overstates its
own precision is the specific failure this section elsewhere calls the estate's recurring one.

Over the 100 commits before this ruling the gate would have flagged **15** added lines, every one of
them genuine — so the habit it addresses is live, not hypothetical, and the gate will have work to do
from its first commit.

## The lakehouse cloud-native cutover (2026-09-03/04)

The cloud-native cutover plan asked one question: can the lakehouse stop doing unbounded work in request
handlers and stop signing writes with a root object-store key. It is closed, and the durable findings
are here because the plan doc is deleted — a finished plan left in the tree reads as outstanding work.

**The catalog exposes Lance's own distributed compaction protocol, not a hand-rolled Rewrite.** The
plan called for widening the commit door to accept `LanceOperation.Rewrite` built from
`write_fragments` output. Running it found that is not awkward but IMPOSSIBLE on this estate's tables:
Lance refuses with `All fragments must have row ids`, and every medallion table is written with stable
row ids. `Compaction.plan` → `CompactionTask.execute` → `Compaction.commit` are all `.json()`
serializable, so the split by credential is the protocol's own: plan and commit are metadata-only under
the catalog's key, execute moves every byte under a vended one.

**Maintenance discovers work by LISTING BUCKETS, and must.** Lakekeeper's catalog-directed task queue
is sound because every Iceberg commit goes through the catalog — the commit pointer lives in it. rask
deliberately does not have that (Lance puts the CAS in the object store, which is why this estate needs
no relational DB), and the medallion movers call `lance.write_dataset` directly, so a catalog-directed
decider would be blind to the highest-churn writer in the estate. Two supporting facts: the selection
function is whole-estate (`_protected_roots` must open every manifest in every bucket, because a shallow
clone in bucket B is the only thing that knows bucket A's dataset must not be rewritten), and datasets
carrying no policy have no record to poll. Note also that Lakekeeper performs zero compaction (no
`rewrite_data_files`, no `OPTIMIZE`) — so on the DATA-REWRITING half it is no reference and Lance's own
`Compaction.plan`/`.execute`/`.commit` is the guide. It is NOT, however, a catalog that only does cheap
work, and an earlier version of this sentence said so: its orphan-file removal "performs a full
recursive listing of the table's storage location, which can be expensive for tables with many files",
and its scheduling is adaptive rather than a timestamp comparison — the next run is timed to reclaim a
target number of bytes at the last run's observed rate, clamped to [1 day, ceiling]. Where that work
runs it answers explicitly: "we recommend running expire snapshots workers in dedicated pods to avoid
impacting REST API performance", with the API pod's worker count set to zero. See
docs/DECISIONS.md "Cascade repair" — checked against docs.lakekeeper.io 2026-09-04.

**A vended credential must use the `aws_`-prefixed storage-option spellings.** Every fleet pod exports
AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, and with the bare spellings object_store BLENDS the two
sources and signs with a pair belonging to neither identity. Measured: identical options, identical
read — ALLOWED with no AWS_* env, `403 SignatureDoesNotMatch` with it, ALLOWED again under the prefixed
spellings with it still set. No test process has an ambient AWS_* environment, so the spelling that
fails in every pod passes every unit test. `test_vending.py` once forbade the prefix, for a real reason
(an e2e had read boto3's parameter names back out of the payload); the concern was right and the
conclusion was wrong — what matters is ONE vocabulary, and measurement chose it.

**Vending coverage is per warehouse, not estate-wide.** `warehouse:lance_catalog` is described as the
root whose grant cascades estate-wide; measured against the live store it parents 8 namespaces, among
~130 warehouses and 2810 tuples. A `can_write_data` check for the maintenance subject on a table under
another warehouse returns `allowed:false` with that grant in place. So vending hardens the granted
warehouses and falls back — correctly, and audibly per dataset — everywhere else. Closing that gap is
an authorization-model decision (a platform-subject rung, or a grant seeded at warehouse creation), not
a longer tuple list.

**The AMBIENT credential was demoted too, not just the vended path.** Vending covers the granted
warehouses; everywhere else a rewrite falls back to whatever key the service holds, so hardening only
the vended path would have left the fallback as the tenant root. `services/maintenance` now runs as a
scoped `rask-maintenance` RustFS user provisioned by `chart/templates/rustfs-scoped-users.yaml` —
which is the step that did not exist before: `values.yaml` carried `rayComputeAccessKey` with the
admission that "`scripts/` has no provisioning step yet", so the estate's one scoped-user precedent was
a knob only a hand-run `mc` session could turn, and a fresh install came up on the tenant root.

Six probes signed for real against the deployed store — the three ALLOWs matter as much as the denies,
because a credential that cannot do the service's job is an outage rather than hardening:

| Probe | scoped `rask-maintenance` | tenant root `rustfsadmin` |
| --- | --- | --- |
| LIST the warehouse (discovery) | ALLOWED | ALLOWED |
| GET a data object | ALLOWED | ALLOWED |
| PUT into a data prefix (compaction) | ALLOWED | ALLOWED |
| PUT into `_projects/` (the registry) | **AccessDenied** | ALLOWED |
| PUT into `_protection/` (the guard) | **403** | ALLOWED |
| LIST the observability store | **AccessDenied** | ALLOWED |

Maintenance can no longer rewrite the records that govern maintenance — including the protection guard
it consults before every compaction. Both halves had to move together: repointing the access key alone
gives `SignatureDoesNotMatch` on every operation, so the secret field moved to its own
`maintenance-s3-secret-key` rather than a second value on `rustfs-secret-key`, which other services
read and which overwriting would have repointed the whole estate at a maintenance-scoped credential.

**N5's premise was falsified: the sweep lock cannot simply be retired.** The row read "retire the
process-local locks and the `replicas: 1` pin once the ack is the lease". The ack IS the lease for the
EXECUTOR — `api/work.py::handle_unit` takes no lock, and redelivery is safe because compaction and GC
are convergent. It is not the lease for the PLANNER: `bindings.cron` fires on every replica with no
coordination anywhere in the path (Diagrid, verbatim: "No coordination – each replica runs the schedule
independently"), so two replicas would both plan and both enqueue the whole estate. Scaling the
executor therefore needs a SPLIT — a second Deployment with its own app-id, subscribed to the work
topic and not scoped to the cron binding — not a larger replica count. That is a pure chart change,
because Dapr component scoping decides which lanes a replica set receives. It is not urgent: the
estate is tens to hundreds of datasets, and a whole-estate tick planned and published 20 units in
1.76s.

---

## A rename moves a POINTER, not bytes (2026-09-04)

`rename_table` copied the dataset root in-process and deleted the source. It answered 200, which is
why nothing flagged it, but the cost of a rename was the DATASET's size, paid inside a request
handler — unbounded work no pod sizing fixes, the same class as the `maintenance/compact` door before
it became a 202.

**lance-ns already answers this.** `RenameTableRequest` carries `id`, `new_namespace_id` and
`new_table_name` — identifiers, no location — so the operation is defined as a remap and nothing in
the request could describe a relocation. What makes that servable here is the V2 storage layout: a
table's directory is `<hash>_<object_id>` while the authoritative name→location mapping lives in the
`__manifest` table, and the spec says the `object_id` suffix "ensures uniqueness and aids debugging"
(`lance_docs/namespace.md` § *Manifest Table Directory*). It is a label, not a resolution path. The
hash prefix exists for object-store throughput and for create/delete/recreate conflict prevention,
not for addressing.

So the rename is: register the destination id at the source's existing location, deregister the
source. **O(1) in the dataset.**

MEASURED on the `dir` backend the chart runs (`LANCE_REST_IMPL=dir`, pylance 10.0.0):

| | before | after |
| --- | --- | --- |
| destination resolves to | — | the SOURCE's own location, unchanged |
| rows / versions | 4 / 2 | 4 / 2 |
| `list_tables(ns1)` | `['old']` | `['new']` |
| bytes moved | the whole dataset | none |

Three failure modes go with the copy: the half-copy on a failed relocation, the non-atomic source
delete that could strand a partially-deleted source, and the read-rewrite that would have collapsed
version history (which the copy existed to avoid). What replaces them is one window: a failure
between the two calls leaves BOTH ids resolving to ONE dataset — no data duplicated, no data at risk,
recoverable by deregistering either id. `_copy_dataset` / `_delete_dataset` are deleted.

**Two consequences worth stating, because both look like regressions and are not.**

The source's terminal lineage marker is now `DEREGISTER_TABLE`, not `DROP_TABLE`. A DROP says the data
is gone — `dropped_at()` fires and the reconciler stops expecting the location — which would be a lie
about a dataset that is still there, still governed, and still the destination's own history.

Protection still gates a rename exactly like a drop, even though no byte moves. The bytes surviving is
not the point: the protection record, every policy and every grant are keyed on the ID, and a rename
takes the table out from under all three.

**One shape is REFUSED rather than served.** A V1 root-namespace table is stored under compatibility
naming (`<name>.lance`), where the location IS the name, and the spec's rule is that renaming one
"transitions to the V2 hash-based path naming" — a relocation. Serving that with a byte copy would put
the unbounded work straight back, so it is an `InvalidInputError` naming the reason. rask's own
`require_parent` guard refuses root tables, so no table reachable through these doors has that shape.

The INDEX-BUILD half of docs/DECISIONS.md "A rename moves a POINTER, not bytes" is untouched by this and still stands: index builds
run wherever they are invoked, and `create_index_uncommitted` / `commit_existing_index_segments` give
them the same plan-elsewhere / commit-here split compaction now uses.

---

## Cascade repair — detection, and the repair verb (2026-09-04)

A missed cascade hop was undetectable and unrepairable: the only remedy was re-publishing the
upstream table, which re-drives every consumer of it rather than the one edge that failed. Five
pieces closed it (C1, C3a, C3b, C3, C4, C2); what survives here is the reasoning that would otherwise
be re-derived.

**C3 and C4 cover DISJOINT failures and neither substitutes for the other.** A refusal counter can
only see a hop that ARRIVED and was declined — `_preflight` DROPs, and a DROP is an ACK, so Dapr
neither redelivers nor dead-letters and `medallion_stage_refused_total` is the only evidence. It is
structurally blind to a hop that never happened. `medallion_cascade_lag` measures the other side: how
many source versions a destination has not consumed, which rises whether or not anything was refused.

**The re-run verb: `POST /api/movers/stages/rerun`.** Edge-addressed, so it re-drives ONE hop.

*The token is OPTIONAL.* It is the `table_published` event id, which the control outbox drops on ack
and no durable store retains, so a verb that required one could not be built. Supplied, the trigger is
verbatim and the mover's deterministic instance id reattaches at no extra call; absent, a fresh one
and a full recompute, which is the common case for the never-ran shape anyway.

*The rung is the EDGE's own* — `can_promote` on `namespace:<project>-gold` for silver→gold, exactly
what the mover asks when it runs the hop itself. `/produce`'s `can_administer` is coarser AND
different and would lock out the non-admin validator the rung exists for. Its sibling `terminate`
stays on `authorize_produce`: two verbs, two rungs, because stopping is not re-driving.

*No 409, and no forward.* The draft's liveness check needed a Ray job LISTING, and `GET /api/jobs/`
accepts no parameters at all — measured on this estate at 81,155 jobs / 164.7 MB in one response,
1179 MiB RSS against a 1536 MiB limit. The stage write is `mode="overwrite"`, overwrite-convergent, so
a racing fresh-token re-run reaches a correct final state and wastes only compute; the response says
so rather than implying a guarantee the listing could not make. Dropping the check dissolved the only
reason to forward to the mover, so the producer mints the trigger itself — which its own
`table_published` subscription already does, through the same `build_stage_trigger`.

**C3 shipped non-functional for weeks, and the chain is worth keeping.** Driven in-cluster for the
first time on 2026-09-04 it reported every edge failing on every tick. Seven layers, each hidden by
the one in front: the destinations env absent (stale deployment); `/api/v1/runs` where lineage serves
`/runs`; no credential at all; the shared token where the door binds a privileged subject to
`service-token-<identity>`; `can_get_metadata` missing because the root-warehouse `reader` grant does
not reach the medallion tiers' warehouse; lineage's subject ALLOWLIST, a different mechanism from the
grant; and finally the dedicated token that does not exist because `dedicatedServiceCredentials` is
false — which is `open_estate-verification.md` row 35 (B), not this work.

**Why six of those survived is the durable lesson.** A reader that cannot read reports `known=False`,
which publishes NOTHING and reports nothing wrong — so an empty series reads as a healthy cascade.
That is the same silent-loss shape the whole cascade-repair effort was about, committed inside the
detector written to catch it. A detector's failure path must be as loud as its finding.

---

## The compute plane is decoupled: a port, three adapters, and no engine in the platform (2026-09-04)

docs/DECISIONS.md "The compute plane is decoupled" (§7.4), closed. What the platform holds and what an adapter holds are now
different things, and the boundary is measurable rather than asserted:

```
ray IMPORT in packages/service-kit/src : 0      ray DEPENDENCY (uv tree)        : 0
ray IMPORT in services/catalog/src     : 0      engine nouns in the OpenAPI     : 0  (was 6)
```

**The port.** `WorkOrder` says what must happen, `Executor` says how the platform asks and learns the
outcome, `task_registry` says which engine may be asked, `attestation` says what a conforming output
is. All in `service-kit`, none naming an engine. `credential_ref` NAMES a credential and never carries
one, so an order is safe to log, queue or replay.

**Three adapters, all outside the platform.** `inprocess_executor` (synchronous — proving the port
does not assume a poll), `ray_submit` (the Jobs API), `rayjob_executor` (a `RayJob` CR + Kueue). A
fourth needs no platform change: register tasks for it, host something that runs them. The catalog
validates a declaration against the registry without learning any engine's vocabulary, because
`command` is a string it forwards and never parses.

**WHO decides is the RECORD, not a flag.** A transform names a task; the task's registration names the
engine; `engine_choice` routes. A task registered for an engine this deployment does not host is
REFUSED, never run on whatever happens to be configured — that is how the wrong program rewrites a
tenant's data while every status says success. An estate that has declared nothing still follows
`MEDALLION_RAY_ENABLED`, so the opt-in default is unchanged.

**`DURABLE_RECORD` is the capability that earns the CR.** Ray's GCS is not fault-tolerant here, so the
Jobs-API watcher carries `MAX_UNSEEN_POLLS`/`MAX_RESUBMITS` and a poll ceiling. A `RayJob` is an etcd
object, so the adapter advertises durability and `may_resubmit` refuses to resubmit an `UNKNOWN`
handle. The machinery is switched off by a capability rather than deleted, which is the whole reason
that rule lives on the port.

**One premise of the plan was falsified.** It read "steps 1-3 are required for a second engine". A
second engine landed at step 2 with no `RayJob` anywhere. Step 3 is required for KUEUE and BYO
maintenance compute — admission and quota, not plurality.

**Step 5 is deferred with a precedent, not a shrug.** A `Transform` CRD belongs to `rask-operator`:
a CRD without its controller renders unreconciled CRs as objects stuck mid-provision, ruled 2026-08-16
for the `Project` CRD and re-verified live. It is the change that would also retire the dual source a
mover row still carries (`stageJob` beside the declaration that supersedes it); both rows live in
`open_lakehouse_diff_left.md`.
