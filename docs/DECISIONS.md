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
  `index_catalog.py`/`search_api` pattern); today's `/search` is metadata-only by design. Stays behind the
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
`team#member` directly — `services/common/auth/model.fga`), so identity administration is a *membership*
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
docs/DURABILITY.md + docs/RUNBOOK-restore.md):
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
