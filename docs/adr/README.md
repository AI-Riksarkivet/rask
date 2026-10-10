# Architecture decision records

One numbered file per decision, each with its rationale, so code and docs cite a permanent record.
The earliest records came out of two goal-tracking logs (`GOAL-prove-it.md`, `DESIGN-catalog-parity.md`);
only the decisions other files cite were kept, not the day-by-day tracking. A record's title keeps its
original label (`P1.1`, `#38b`, `#3-A`, …), so a citation by label finds it through the table below.

Each record is one numbered file, `NNNN-<slug>.md`, numbered in the order the decisions were
recorded; the title keeps the original label. A new decision is a new file with the next number,
and the history the comment rule in `CLAUDE.md` keeps out of code comments is recorded here.
Records 0107–0137 are decisions that were made in architecture documents, audits and working plans
before they had a record here; each names its source document and the date of the ruling.

| ADR | Label | Decision |
| --- | --- | --- |
| [0001](0001-p0-1-why-e2e-stack-sh-exists-live-verify-honesty.md) | P0.1 | why e2e_stack.sh exists (live-verify honesty) |
| [0002](0002-p0-2-claim-lint-the-grep-provable-invariants.md) | P0.2 | claim-lint (the grep-provable invariants) |
| [0003](0003-p1-1-outbox-observability-the-four-signals.md) | P1.1 | outbox observability (the four signals) |
| [0004](0004-p1-2-bounded-oldest-first-outbox-drain.md) | P1.2 | bounded, oldest-first outbox drain |
| [0005](0005-p2-1-single-base-cascade-write.md) | P2.1 | single-base cascade write |
| [0006](0006-16-dapr-workflow-for-silver-to-gold-promotion.md) | #16 | Dapr Workflow for silver-to-gold promotion |
| [0007](0007-7a-live-verification-residuals.md) | §7a | live-verification residuals |
| [0008](0008-9-feature-gaps-the-open-backlog.md) | §9 | feature gaps (the open backlog) |
| [0009](0009-12-prod-hardening-backlog-native-switches-off.md) | §12 | prod-hardening backlog (native switches off) |
| [0010](0010-115a-c-ray-train-vs-ray-data-one-platform-both-workload.md) | #115a-c | Ray TRAIN vs Ray DATA (one platform, both workload classes) |
| [0011](0011-blob-pointer-lifecycle-gc-never-collect-referenced-artifacts.md) |  | blob-pointer-lifecycle GC — never collect referenced artifacts |
| [0012](0012-schema-declaration-claim-check-hardening.md) |  | schema-declaration + claim-check hardening |
| [0013](0013-age-on-cnpg-vs-lance-native-graph-the-lineage-store-decision.md) |  | AGE-on-CNPG vs Lance-native-graph (the lineage-store decision) |
| [0014](0014-control-plane-vs-data-plane-split-the-prod-cut.md) |  | Control-plane vs data-plane split (the prod cut) |
| [0015](0015-3-a-per-warehouse-bucket-physical-multi-tenancy.md) | #3-A | per-warehouse bucket (physical multi-tenancy) |
| [0016](0016-3-b-lance-multi-base-throughput-tiering-dr.md) | #3-B | Lance multi-base (throughput, tiering, DR) |
| [0017](0017-38b-mv-lineage-is-wontfix-no-source-tables.md) | #38b | MV-lineage is WONTFIX (no source_tables) |
| [0018](0018-lance-spec-landmines.md) |  | Lance-spec landmines |
| [0019](0019-feature-gap-1-serving-blob-serving-is-a-governed-proxy-not.md) |  | FEATURE-GAP §1 (serving) — blob serving is a governed proxy, not presigned URLs |
| [0020](0020-feature-gap-minor-deviations-1-7-the-spec-deviation-register.md) |  | FEATURE-GAP minor deviations #1–#7 — the spec-deviation register |
| [0021](0021-gateway-checks-where-auth-lives-2026-07-23.md) |  | Gateway checks — where auth lives (2026-07-23) |
| [0022](0022-ui-operability-boundaries-what-deliberately-has-no-browser.md) |  | UI-operability boundaries — what deliberately has NO browser surface (2026-07-23) |
| [0023](0023-workflow-history-has-no-browser-surface-the-alert-is-the.md) |  | Workflow history has no browser surface — the ALERT is the surface (2026-08-26, owner ruling) |
| [0024](0024-team-role-administration-wontfix-until-the-keycloak-sync.md) |  | Team/role administration — WONTFIX until the Keycloak sync (2026-07-23) |
| [0025](0025-streams-on-a-medallion-off-governed-stack-answers-503-fail.md) |  | /streams on a medallion-off governed stack answers 503 — fail-closed, correct (2026-07-23) |
| [0026](0026-catalog-control-wildcard-masking-accepted-at-replicas-1.md) |  | CATALOG_CONTROL wildcard masking — accepted at replicas:1 (2026-07-23) |
| [0027](0027-control-events-broadcast-ring-buffer.md) |  | control-events — broadcast + ring buffer |
| [0028](0028-control-events-per-replica-cursor-boundary.md) |  | control-events — per-replica cursor boundary |
| [0029](0029-control-events-estate-admin-scope.md) |  | control-events — estate-admin scope |
| [0030](0030-control-events-query-live-supersedes-sse.md) |  | control-events — query.live supersedes SSE |
| [0031](0031-control-events-fail-open-emit-contract.md) |  | control-events — fail-open emit contract |
| [0032](0032-p3b-alerting-rule-logic-proven-hermetically-the-live.md) | P3b | alerting: rule logic proven hermetically; the live transport is a drill |
| [0033](0033-p4-p7-backups-structural-spofs-the-prod-answer-is.md) | P4/P7 | backups + structural SPOFs: the prod answer is externalize, not in-chart HA |
| [0034](0034-medallion-tiers-hybrid-physical-layout-2026-07-24.md) |  | Medallion tiers — hybrid physical layout (2026-07-24) |
| [0035](0035-runner-deployment-the-cpu-viable-subset-is-real-the-rest-is.md) |  | Runner deployment — the CPU-viable subset is real, the rest is an honest GPU list (2026-07-24) |
| [0036](0036-ingest-orchestration-dapr-workflow-is-adopted-the-estate-is.md) |  | Ingest orchestration — Dapr Workflow IS adopted; the estate is event-driven now (2026-08-03, owner ruling) |
| [0037](0037-the-outbox-is-application-side-and-dapr-s-transactional.md) |  | The outbox is application-side, and Dapr's transactional outbox cannot replace it (2026-08-15) |
| [0038](0038-helm-release-storage-the-sql-driver-stands-the-chart-is-not.md) |  | Helm release storage: the SQL driver stands; the chart is NOT split (2026-08-15) |
| [0039](0039-lineage-records-what-happened-to-data-an-authorization.md) |  | Lineage records what happened to DATA; an authorization denial is not a data event (2026-08-16) |
| [0040](0040-the-publication-verdict-rides-the-run-facet-not-the-inbox.md) |  | The publication verdict rides the run FACET, not the inbox pointer (2026-08-16) |
| [0041](0041-watch-enrolment-does-not-wait-for-the-platform-rask-io-crd.md) |  | Watch enrolment does not wait for the `platform.rask.io` CRD (2026-08-16) |
| [0042](0042-the-compute-service-gets-no-emitter-yet-and-the-blocker-is.md) |  | The compute service gets no emitter yet, and the blocker is identity (2026-08-16) |
| [0043](0043-the-bell-cannot-carry-the-publication-verdict-and-the.md) |  | The bell cannot carry the publication verdict, and the reason is the claim check (2026-08-16) |
| [0044](0044-comments-carry-rationale-and-provenance-never-a-changelog.md) |  | Comments carry rationale and provenance, never a changelog of the prose (2026-08-30, owner ruling) |
| [0045](0045-the-lakehouse-cloud-native-cutover-2026-09-03-04.md) |  | The lakehouse cloud-native cutover (2026-09-03/04) |
| [0046](0046-a-rename-moves-a-pointer-not-bytes-2026-09-04.md) |  | A rename moves a POINTER, not bytes (2026-09-04) |
| [0047](0047-the-python-estate-audit-2026-08-07-2026-09-05.md) |  | The Python estate audit (2026-08-07 → 2026-09-05) |
| [0048](0048-a-repeating-condition-is-a-level-not-an-event-2026-08-30.md) |  | A repeating condition is a LEVEL, not an event (2026-08-30) |
| [0049](0049-maintenance-leaves-the-planner-pod-2026-09-04.md) |  | Maintenance leaves the planner pod (2026-09-04) |
| [0050](0050-is-this-governed-at-all-is-a-question-openfga-answers-in.md) |  | "Is this governed at all" is a question OpenFGA answers in one pass (2026-09-11) |
| [0051](0051-cascade-repair-detection-and-the-repair-verb-2026-09-04.md) |  | Cascade repair — detection, and the repair verb (2026-09-04) |
| [0052](0052-the-compute-plane-is-decoupled-a-port-two-adapters-and-no.md) |  | The compute plane is decoupled: a port, two adapters, and no engine in the platform (2026-09-04) |
| [0053](0053-a-stage-runner-runs-a-stage-nothing-was-ever-moved-2026-09.md) |  | A stage runner runs a stage; nothing was ever moved (2026-09-07) |
| [0054](0054-on-a-drifted-estate-omitting-a-value-is-not-a-no-op-2026-09.md) |  | On a drifted estate, omitting a value is not a no-op (2026-09-07) |
| [0055](0055-a-privileged-credential-has-three-halves-2026-09-07.md) |  | A privileged credential has THREE halves (2026-09-07) |
| [0056](0056-a-control-s-name-is-not-evidence-that-it-exists-2026-09-07.md) |  | A control's NAME is not evidence that it exists (2026-09-07) |
| [0057](0057-a-dependency-revert-that-leaves-the-range-open-reverts.md) |  | A dependency revert that leaves the range open reverts nothing (2026-09-07) |
| [0058](0058-a-dedicated-credential-is-a-property-of-the-transport-not.md) |  | A dedicated credential is a property of the TRANSPORT, not only of the service (2026-09-07) |
| [0059](0059-counting-one-plane-and-calling-it-the-estate-2026-09-07.md) |  | Counting one plane and calling it the estate (2026-09-07) |
| [0060](0060-an-operator-toggle-is-not-evidence-the-resource-exists-2026.md) |  | An operator toggle is not evidence the resource exists (2026-09-07) |
| [0061](0061-do-not-add-a-control-before-its-verifier-2026-09-07.md) |  | Do not add a control before its verifier (2026-09-07) |
| [0062](0062-bounding-a-read-changes-what-every-caller-of-the-unbounded.md) |  | Bounding a read changes what every caller of the unbounded contract MEANS (2026-09-07) |
| [0063](0063-the-object-store-is-minio-and-the-reason-is-who-may-call.md) |  | The object store is MinIO, and the reason is who may call AssumeRole (2026-09-11, LH-133) |
| [0064](0064-the-port-s-ray-adapter-is-the-jobs-api-not-a-rayjob-cr-2026.md) |  | The port's Ray adapter is the Jobs API, not a RayJob CR (2026-09-15) |
| [0065](0065-result-is-a-capability-and-both-stage-lanes-go-through-the.md) |  | `RESULT` is a capability, and both stage lanes go through the port (2026-09-17) |
| [0066](0066-four-rulings-on-the-lakehouse-s-unblocked-rows-owner-2026.md) |  | Four rulings on the lakehouse's unblocked rows (owner, 2026-09-19) |
| [0067](0067-the-three-mesh-headers-stay-in-the-spec-document-and-the.md) |  | The three mesh headers stay in the spec document, and the quota has no upstream precedent (owner, 2026-09-20) |
| [0068](0068-the-r-rulings-are-the-lance-ns-merge-s-they-were-accepted.md) |  | The `R#` rulings are the lance-ns merge's, they were ACCEPTED in July, and three rows gated on them anyway (2026-09-20) |
| [0069](0069-the-ray-job-s-id-becomes-the-order-s-key-and-the-executor.md) |  | The Ray job's id becomes the order's key, and the executor port is missing five things (2026-09-20) |
| [0070](0070-four-owner-answers-and-the-reference-implementation-that.md) |  | Four owner answers, and the reference implementation that unblocked three of them (owner, 2026-09-21) |
| [0071](0071-compaction-mode-is-not-a-measure-of-where-bytes-moved-2026.md) |  | `compaction_mode` is not a measure of where bytes moved (2026-09-21) |
| [0072](0072-a-resource-measurement-runs-outside-the-thing-it-measures.md) |  | A resource measurement runs OUTSIDE the thing it measures (2026-09-21) |
| [0073](0073-a-memory-bound-applies-at-every-hop-not-at-the-layer-that.md) |  | A memory bound applies at every hop, not at the layer that happens to set it (2026-09-22) |
| [0074](0074-a-duration-is-not-evidence-of-the-mechanism-that-produced.md) |  | A duration is not evidence of the mechanism that produced it (2026-09-22) |
| [0075](0075-a-producer-signs-for-a-person-by-declaring-it-lh-064-owner.md) |  | A producer signs for a person by DECLARING it (LH-064, owner 2026-09-24) |
| [0076](0076-code-with-no-production-caller-is-deleted-with-the-tests.md) |  | Code with no production caller is deleted with the tests that kept it green (owner, 2026-09-25) |
| [0077](0077-a-producer-door-on-an-existing-resource-authorizes-on-that.md) |  | A producer door on an existing resource authorizes on THAT resource (owner, 2026-09-25) |
| [0078](0078-get-stage-runners-admits-any-signed-in-caller-owner-default.md) |  | `GET /stage-runners` admits any signed-in caller (owner default, 2026-09-26) |
| [0079](0079-2026-09-26-phase-1-s-five-criteria-and-the-backlog-does-not.md) |  | 2026-09-26 — Phase 1's five criteria, and the backlog does not grow without the owner |
| [0080](0080-lineage-s-bus-consumer-is-durable-and-a-graph-rebuild-is-an.md) |  | Lineage's bus consumer is durable, and a graph rebuild is an explicit step (LH-303, owner 2026-09-26) |
| [0081](0081-dropped-at-means-no-longer-catalogued-at-this-id-lh-144.md) |  | `dropped_at` means "no longer catalogued at this id" (LH-144, owner scope 2026-09-27) |
| [0082](0082-the-store-s-newest-authorization-model-decides-nothing-each.md) |  | The store's newest authorization model decides nothing; each component uses the model its image carries (LH-201, 2026-09-28) |
| [0083](0083-a-service-is-the-serviceaccount-its-projected-token-names.md) |  | A service is the ServiceAccount its projected token names (LH-220, D1, 2026-10-02) |
| [0084](0084-lineage-events-are-signed-with-ed25519-the-keys-in-the.md) |  | Lineage events are signed with Ed25519, the keys in the store (LH-064, owner 2026-10-02) |
| [0085](0085-the-bus-doors-verify-lh-064-s-signatures-before-they-act-xc.md) |  | The bus doors verify LH-064's signatures before they act (XC-078 slice 1, owner 2026-10-04) |
| [0086](0086-nats-authenticates-every-client-one-user-per-app-xc-078.md) |  | NATS authenticates every client, one user per app (XC-078 slices 2 and 3, owner 2026-10-04/05) |
| [0087](0087-the-notifications-reconciler-reads-the-whole-run-feed.md) |  | The notifications reconciler reads the whole run feed through a rung of its own (CTL-021, 2026-10-05) |
| [0088](0088-a-ray-stage-run-is-a-plan-closed-by-its-job-s-report-or-the.md) |  | A Ray stage run is a plan, closed by its job's report or the sweep (CP-029 S1, 2026-10-05) |
| [0089](0089-a-warehouse-in-another-object-store-is-refused-until-its.md) |  | A warehouse in another object store is refused until its credential is consumed (LH-205, 2026-10-05) |
| [0090](0090-the-client-direct-commit-holds-each-fragment-to-its-data.md) |  | The client-direct commit holds each fragment to its data files (LH-211, 2026-10-05) |
| [0091](0091-the-promotion-review-goes-through-the-saga-port-lh-226-2026.md) |  | The promotion review goes through the saga port (LH-226, 2026-10-05) |
| [0092](0092-restore-serves-its-branch-and-the-branch-gate-drives-the.md) |  | restore serves its branch, and the branch gate drives the doors (LH-272, 2026-10-05) |
| [0093](0093-a-writer-cannot-rewrite-a-table-s-provenance-lh-208-2026-10.md) |  | A writer cannot rewrite a table's provenance (LH-208, 2026-10-05) |
| [0094](0094-an-external-blob-base-is-authorized-per-table-and-per.md) |  | An external blob base is authorized per table and per object, and no vend grants one (LH-209, 2026-10-05) |
| [0095](0095-a-branch-s-directory-holds-only-that-branch-s-files-lh-203.md) |  | A branch's directory holds only that branch's files (LH-203, 2026-10-05) |
| [0096](0096-a-write-event-names-the-commit-the-write-made-version-ref.md) |  | A write event names the commit the write made: version, ref and branch incarnation (LH-214, 2026-10-05) |
| [0097](0097-one-dataset-location-has-one-holder-and-the-purge-spares.md) |  | One dataset location has one holder, and the purge spares bytes any id resolves to (LH-204, 2026-10-05) |
| [0098](0098-a-namespace-overwrite-replaces-only-an-empty-namespace-and.md) |  | A namespace Overwrite replaces only an empty namespace, and a Skip drop finishes the trailer (LH-037, 2026-10-05) |
| [0099](0099-an-overwrite-is-a-new-version-of-the-same-table-lh-242-2026.md) |  | An Overwrite is a new version of the same table (LH-242, 2026-10-05) |
| [0100](0100-an-erasure-is-verified-on-bytes-index-segments-held.md) |  | An erasure is verified on bytes: index segments, held surfaces and the branch copy cap (LH-263, 2026-10-05) |
| [0101](0101-a-stage-re-run-writes-what-its-upstream-holds-now-and-adds.md) |  | A stage re-run writes what its upstream holds now, and adds a column by `id` (LH-213, 2026-10-05) |
| [0102](0102-the-sweep-is-the-one-reclaimer-lance-s-commit-path-auto.md) |  | The sweep is the one reclaimer: Lance's commit-path auto-cleanup is closed (LH-245, 2026-10-05) |
| [0103](0103-every-committing-door-disarms-the-ref-first-the-sweep-is.md) |  | Every committing door disarms the ref first; the sweep is the backstop (LH-245, 2026-10-05) |
| [0104](0104-a-carried-blob-column-keeps-every-payload-and-its-field.md) |  | A carried blob column keeps every payload and its field metadata (LH-217, 2026-10-05) |
| [0105](0105-a-training-run-is-a-work-order-and-only-an-engine-that-can.md) |  | A training run is a work order, and only an engine that can lose a record is resubmitted to (CP-044, 2026-10-05) |
| [0106](0106-the-stage-write-is-one-module-both-engines-land-through-cp.md) |  | The stage write is one module both engines land through (CP-056 step 2, 2026-10-05) |
| [0107](0107-keep-the-catalog-diy-not-lakekeeper-gravitino-unity-or-ducklake.md) |  | Keep the catalog DIY, not Lakekeeper, Gravitino, Unity or DuckLake (2026-09-02) |
| [0108](0108-the-lance-ns-merge-is-total-and-the-medallion-replaces-rask-s.md) | R1, R2 | The lance-ns merge is total, and the medallion replaces rask's orchestration (R1, R2, 2026-07-24) |
| [0109](0109-one-ray-cluster-on-the-latest-release-r3-2026-07-24.md) | R3 | One Ray cluster on the latest release (R3, 2026-07-24) |
| [0110](0110-serialization-is-a-projection-from-gold-served-by-its-own.md) | R4, R25 | Serialization is a projection from gold, served by its own service (R4, R25, 2026-07-24) |
| [0111](0111-gold-carries-its-lineage-as-a-jsonb-column-r26-2026-07-28.md) | R26 | Gold carries its lineage as a JSONB column (R26, 2026-07-28) |
| [0112](0112-the-media-plane-absorbs-rask-s-discovery-and-viewing-r5-r6.md) | R5, R6 | The media plane absorbs rask's discovery and viewing (R5, R6, 2026-07-24) |
| [0113](0113-the-zone-set-r8-r9-r15-r16-r17-r18-2026-07-27.md) | R8, R9, R15–R18 | The zone set (R8, R9, R15, R16, R17, R18, 2026-07-27) |
| [0114](0114-lance-ns-s-frontend-toolchain-and-zone-directory-win-r10-r11.md) | R10, R11 | lance-ns's frontend toolchain and zone directory win (R10, R11, 2026-07-27) |
| [0115](0115-dagger-tracks-the-newest-release-r12-2026-07-27.md) | R12 | Dagger tracks the newest release (R12, 2026-07-27) |
| [0116](0116-the-otel-collector-is-the-only-log-shipper-r13-2026-07-27.md) | R13 | The OTel Collector is the only log shipper (R13, 2026-07-27) |
| [0117](0117-nginx-is-retired-and-the-fastapi-gateway-is-the-in-cluster.md) | merge decision 4, R14 | nginx is retired, and the FastAPI gateway is the in-cluster edge (merge decision 4, R14, 2026-07-24) |
| [0118](0118-common-merges-into-service-kit-r19-2026-07-27.md) | R19 | common merges into service-kit (R19, 2026-07-27) |
| [0119](0119-the-ray-plane-service-is-compute-and-no-deployable-carries.md) | R20, R22 | The Ray-plane service is `compute`, and no deployable carries `-api` (R20, R22, 2026-07-28) |
| [0120](0120-one-compute-lineage-layer-lineage-kit-r21-2026-07-27.md) | R21 | One compute-lineage layer, `lineage-kit` (R21, 2026-07-27) |
| [0121](0121-raw-is-not-a-catalog-tier-and-ingest-is-its-own-service-r23.md) | R23, R24 | Raw is not a catalog tier, and ingest is its own service (R23, R24, 2026-07-28) |
| [0122](0122-the-ray-plane-gets-a-standing-audit-r27-2026-07-28.md) | R27 | The Ray plane gets a standing audit (R27, 2026-07-28) |
| [0123](0123-storage-is-registered-with-a-role-r28-2026-07-28.md) | R28 | Storage is registered with a role (R28, 2026-07-28) |
| [0124](0124-chart-unification-one-control-plane-each-one-object-store.md) | P4 | Chart unification: one control plane each, one object store, every hook pod labelled (P4, 2026-07-24) |
| [0125](0125-age-on-cnpg-via-imagevolume-behind-its-own-gate-merge.md) | merge decision 1 | AGE on CNPG via ImageVolume, behind its own gate (merge decision 1, 2026-07-24) |
| [0126](0126-the-merge-s-other-four-decisions-dex-stays-zone-names-stay.md) | merge decisions 2–5 | The merge's other four decisions: Dex stays, zone names stay, e2e is extended, NATS HA is parked (2026-07-24) |
| [0127](0127-an-existing-lance-table-enters-by-fragment-append-or-the.md) | ingest 1b | An existing Lance table enters by fragment append or the register door, never by overwrite (ingest 1b, 2026-08-07) |
| [0128](0128-incremental-ingest-is-an-anti-join-against-bronze-on-a-cron.md) | ingest 1c | Incremental ingest is an anti-join against bronze, on a cron (ingest 1c, 2026-08-07) |
| [0129](0129-ingest-needs-the-warehouse-and-namespace-never-the-table.md) | ingest 1d | Ingest needs the warehouse and namespace, never the table (ingest 1d, 2026-08-07) |
| [0130](0130-a-manual-push-to-bronze-is-an-authorization-policy-not-a.md) | ingest 2 | A manual push to bronze is an authorization policy, not a tier guard (ingest 2, 2026-08-07) |
| [0131](0131-annotations-are-derived-and-readiness-is-the-published-tag.md) | ingest 3/4 | Annotations are derived, and readiness is the `published` tag (ingest 3/4, 2026-08-07) |
| [0132](0132-the-two-cascade-heads-are-distinct-events-and-both-fire.md) | §10 | The two cascade heads are distinct events, and both fire (2026-08-15) |
| [0133](0133-a-synchronous-head-s-trigger-rides-the-caller-retry-contract.md) | §11 | A synchronous head's trigger rides the caller-retry contract (2026-08-15) |
| [0134](0134-the-stage-workflow-review-and-its-operator-surface-superseded.md) | §12 | The stage workflow review and its operator surface (2026-08-16, superseded by 0088) |
| [0135](0135-a-dock-lives-inside-its-zone-2026-08-03.md) |  | A dock lives inside its zone (2026-08-03) |
| [0136](0136-bulk-labeling-is-a-mode-of-the-task-and-columns-carry-recipes.md) |  | Bulk labeling is a mode of the task, and columns carry recipes (2026-08-09) |
| [0137](0137-model-endpoints-are-ray-serve-discovered-by-the-labeling.md) |  | Model endpoints are Ray Serve, discovered by the `labeling` user_config (2026-08-09) |
| [0138](0138-an-erasure-rebases-a-pinning-branch-after-a-seven-day-notice.md) | D4 | An erasure releases a pinning branch or tag after a seven-day notice (2026-10-10) |
| [0139](0139-training-stays-in-the-producer-and-names-no-engine.md) | CP-044 | Training stays in the lakehouse producer and names no engine (2026-10-10) |
| [0140](0140-the-engine-adapter-runs-in-the-compute-service.md) | CP-054 | The engine adapter runs in the compute service (2026-10-10) |
| [0141](0141-the-orphan-scan-reports-and-does-not-gate-the-purge.md) | D13 | The orphan scan reports and does not gate the purge (2026-10-10) |
| [0142](0142-no-observability-backend-receives-the-object-store-root-key.md) | LH-161 | No observability backend receives the object store's root key (2026-10-10) |
| [0143](0143-a-project-is-created-by-an-estate-admin-or-an-operator.md) | D2 | A project is created by an estate admin or an operator (2026-10-10) |
| [0144](0144-storage-credentials-are-vended-to-any-authenticated-client.md) | D9 | Storage credentials are vended to any authenticated client, wherever it runs (2026-10-10) |
| [0145](0145-the-unused-project-admin-relations-are-deleted.md) | CTL-019 | The unused project admin relations are deleted (2026-10-10) |
| [0146](0146-an-expired-drop-releases-its-grants-and-its-name.md) | LH-228 | An expired drop releases its grants and its name (2026-10-10) |
| [0147](0147-produce-asks-the-catalog-where-bronze-lives.md) | D6 | /produce asks the catalog where bronze lives (2026-10-10) |
| [0148](0148-classification-governs-delivery-not-column-reads.md) | LH-288 | Classification governs delivery, not column reads (2026-10-10) |
| [0149](0149-one-token-grammar-for-every-door.md) | LH-300 | One token grammar for every door (2026-10-10) |
| [0150](0150-protection-guards-deletion-not-history-reclaim.md) | LH-300 | Protection guards deletion, not history reclaim (2026-10-10) |
| [0151](0151-a-boot-bound-secret-rotates-only-for-consumers-rask-owns.md) | D8 | A boot-bound secret rotates only for consumers rask owns in production (2026-10-10) |
| [0152](0152-service-kit-ships-py-typed-and-skips-report-zeros.md) | PS | service-kit ships py.typed, and skip attribution reports zeros (2026-10-10) |
