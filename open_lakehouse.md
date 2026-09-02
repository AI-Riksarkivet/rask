# open_lakehouse — the governed Lance lakehouse: verdict, decisions, backlog, open questions

Working backlog, **2026-09-02**, against the working tree at `feec956`. Unsettled work; **delete this
file when the backlog is drained**. `docs/` is for settled architecture only.

**No code was changed by this analysis.** It is a read-only pass whose deliverable is this backlog.

## Why this exists

The owner asked, in sequence: Flyte 2 vs Dapr Workflow (keep Dapr Workflow for now, retreat later);
what a Dapr-free lakehouse looks like; how hard-bound the estate is to Dapr; whether the core uses
state and needs actors; what Lakekeeper has that rask lacks; what the robotics reference teaches;
whether to buy Lakekeeper / Gravitino / Unity / DuckLake instead of the DIY catalog; whether the catalog
is Lance Namespace spec-verbatim; what the Lance docs and the five 2026 posts (multi-base, branching,
blob v2, Spark late materialization, blob streaming) change; and finally a governance sweep of every
backend service that touches the lakehouse plus a zero-trust diff against Lakekeeper. The answers are
spread over five documents and six sweep reports. This file is the one register of what is left.

Source documents, committed under `docs/audits/lakehouse-2026-09/`:
`dapr-coupling-analysis.md`, `lakehouse-analysis.md`, `catalog-build-vs-buy.md`,
`lance-conformance-and-build-rules.md`, `verdict.md`, and the sweep reports `sweeps/{notifications,
lineage, maintenance, gateway-compute-controlplane, packages}.md`. Zero-trust diff: pending at the time
of writing (§F carries its placeholder).

Scope the owner set: catalog, compute, ingest, medallion, maintenance, lineage, notifications, gateway,
controlplane, and the shared packages. **Not swept, on the owner's instruction:** annotator, viewer,
search, flows, models, and every frontend zone.

## How this was produced

Three multi-agent workflows (state + Lakekeeper diff, robotics lessons, Lance docs + 54-op conformance)
with adversarial verifiers: 24 claims, 23 confirmed, 1 refuted, several tightened. Then seven
single-service sweeps against one nine-point rubric (how it touches the lakehouse, authorization,
lineage/events, state, Dapr coupling, format awareness, governance gaps, tests, top findings), each
citing file:line. Live probes where a claim rested on runtime behaviour (the catalog app under the dir
backend, pylance 10.0.0 `RestNamespace` against a logging stub, `DirectoryNamespace` version ops).
Every number below is from those reports; nothing is from memory.

---

## 0. The verdict, and the condition attached to it

**Continue, DIY catalog, Lance-only.** Every candidate (Lakekeeper 0.13.1, Gravitino 1.3.0, Unity OSS
0.6.0, DuckLake 1.0, and all eleven `lance-namespace-impls` backends) gives Lance a registry floor and
`managed_versioning=false`. None knows about the external-manifest-store commit path, multi-base,
branches tracked by root, blob v2 lifecycle, or shallow clones. Those are the things that make a Lance
lakehouse *governed*, and they only exist if the catalog understands the format.

**The condition:** a DIY catalog that only implements the registry floor is worse than Lakekeeper.
rask earns its existence by the format-aware governance in §C. Today it does roughly a third of it,
and the sweeps in §D–§H show the surrounding services are not yet holding the line either. If the team
cannot commit to §A–§D, the coherent alternative is Lakekeeper for the registry plus a thin rask
"governed commit + blob" service, and two catalogs of record. Not recommended; recorded so the choice is
explicit.

---

## 1. Design decisions

### Decided by the owner during this analysis (recorded, not re-litigated)

| # | Decision |
| --- | --- |
| D1 | Audience: **platform teams bringing their own engine** (Temporal, Flyte 2, …). rask exposes events and idempotent doors; it does not own workflow execution long-term. |
| D2 | The annotator is a **client application** of the lakehouse, not platform core. |
| D3 | **Spec-verbatim Lance Namespace REST is a hard goal.** A stock Lance client must work with no rask SDK. |
| D4 | **Lance is the only format, ever.** OpenFGA and OpenLineage are locked as the governance vocabulary. |
| D5 | Keep Dapr Workflow for now; the retreat is OpenBao for secrets, JetStream for events, no Dapr Workflow, BYO engines. |

### Recommended by the analysis; need an owner acknowledgement

| # | Decision | Why |
| --- | --- | --- |
| R1 | The governed commit path IS the spec's managed-versioning path (`CreateTableVersion` / `BatchCommitTables`, `managed_versioning=true`). The non-spec `/commit` door is retired or aliased. | It is the only mechanism by which a stock client's commit passes FGA, gate, lineage and the replay marker. |
| R2 | Everything rask-only leaves `/v1/{namespace,table,materialized_view,transaction}` for a versioned **management API** (Lakekeeper's `/management` vs `/catalog` split). | Governance side effects inside spec handlers change what a spec client observes (7 of the 8 conformance blockers). |
| R3 | **Bases are the storage-profile primitive.** A warehouse in another bucket/account/region or a hot tier is a `DatasetBasePath` plus `base_<id>.<key>` options; writes are steered with `target_bases`; failover is an edit of `base_paths`. | Replaces the hand-rolled "storage profile per warehouse" with the format's own vocabulary. |
| R4 | **Credential vending is per base**, never per table prefix. | The multi-base post calls per-prefix vending the model that does not scale; rask currently refuses to vend a multi-base table. |
| R5 | **Branches are governed at the branch prefix** (`tree/<branch>/`): FGA `branch` type, vending scoped to the prefix, protection/trash/lineage per branch. Write-audit-publish is branch → gate → tag or copy; **there is no merge primitive in pylance 10.0.0**. | The format's branch-by-root design only pays off if the catalog scopes to it. |
| R6 | **Cross-dataset pins are a catalog feature.** A clone or branch records its (source, version) edge; the source version is tag-pinned while referenced; sweep, purge and the on-demand doors consult it. | Lance GC has no knowledge of clones; nothing in any candidate catalog has this either. |
| R7 | **External blob bases carry a lifecycle policy** (`managed` vs `reference-only`) and cleanup runs without delete rights on reference-only bases. | The blob post claims reachability GC reaches in-base external blobs; the archival case must never be reclaimable. |
| R8 | **The descriptor is the read contract.** Query responses return the blob v2 descriptor struct by default; bytes on `blob_handling="all_binary"` opt-in. | Same contract as the Spark connector; what a BYO engine expects. |
| R9 | **The medallion tiers stop copying managed blob bytes.** Silver/gold as shallow clones of bronze at a pinned version plus derived columns (`add_columns`, already used in place). Requires R6 first. Measure before committing. | The cascade re-materialises managed blobs per tier today; external descriptors are already forwarded. |
| R10 | **One lineage emit kernel, outbox-backed.** lineage-kit stays; `service_kit.lancekit.openlineage` and `lancekit.lineage_emit` go; every producer stages before transport. | Two kernels, three producer strings, and both swallow failures; a swallowed bronze-write emit cancels a cascade silently. |
| R11 | **Zero-trust posture is an explicit target**, measured against the control list in §F, not a claim. | Lakekeeper markets it; the sweeps show rask's edge, service doors and storage identity are far from it today. |

### Decisions the owner still has to make

| # | Question | Options | Default if unanswered |
| --- | --- | --- | --- |
| Q1 | Where the five analysis documents live. | Committed under `docs/audits/lakehouse-2026-09/` on `claude/flyte-2-dapr-audit-19cyc2` (the default was taken; move them if you prefer elsewhere). | done |
| Q2 | Delete remote branch `claude/flyte-2-dapr-audit-19cyc2`? The proxy refuses the delete from the sandbox. | Owner deletes; or it becomes the branch for Q1. | Reuse it for Q1. |
| Q3 | UNSUPPORTED error status: keep 501 or follow the spec's 406? | Either parses (the client dispatches on `code`). | Keep 501, document. |
| Q4 | `ratch` console script: dev-only extra, or keep in the head image? | Dev-only vs status quo. | Dev-only (CLAUDE.md says no production-state-changing CLIs). |
| Q5 | Feature flag 32 (`DISABLE_TRANSACTION_FILE`): refuse (today), or support and move the replay marker off `.txn`? | Refuse / support. | Refuse until pylance writes it by default. |
| Q6 | Which service door authenticates producers on the lineage bus: per-publisher tokens, producer signature, or mTLS identity? | See §F. | Producer signature over the CloudEvent. |
| Q7 | The `x-api-key` principal: back it with a management-API key store, or bearer-only? | Both spec-legal. | Support both; keys minted by the management API. |

---

## A. Spec-verbatim (D3) — the ten blockers

Measured: 12 of 54 ops verbatim, 34 partial, 3 model-differs, 5 stub. With pylance 10.0.0's bundled
`RestNamespace`, 5 ops are unusable and 4 answer silently wrong. Vendored spec is v0.9.0; current is
v0.12.0. Details and evidence: `lance-conformance-and-build-rules.md` §2–§3, §9.

### A1 · Bodyless handlers ignore the required JSON body
**What.** `DescribeTable`, `ListTableIndices`, `GetTableStats`, `DescribeTableIndexStats`, the exists/
deregister/transaction ops read version/tag/branch/vend_credentials/pagination from the query string or
not at all. The reference client sends `vend_credentials` only in the body, so **credential vending is
unreachable by any spec client**. **Where.** `services/catalog/src/catalog/api/v1/endpoints/tables.py:270-283,903-907`, `indices.py:97-114`.
**Closes it.** Declare the request model as the body on every op, `reconcile_body_id` uniformly, body
wins over rask's query aliases; a wire-level test posting each spec body.

### A2 · Three response shapes the client cannot parse
**What.** `schema_metadata/update` answers the wrapped envelope (spec: direct `{str:str}`); explain/
analyze answer `text/plain` (spec: JSON string); `count_rows` answers `text/plain` (parses by accident).
**Where.** `columns.py:229-234`, `data.py:698-720`. **Closes it.** Direct map, `JSONResponse` strings,
JSON integer; the envelope dialect moves to the management API.

### A3 · GET vs POST on `count_rows` and `tags/list`
**What.** The spec and lance-namespace's generated client say POST at every tag since 0.9.0. **pylance's
own bundled client and reference server use GET** (`lance` repo `rust/lance-namespace-impls/src/rest.rs`,
`rest_adapter.rs`, at v10.0.0 and main). **Closes it.** Dual-mount both routes; file the upstream issue.

### A4 · `delimiter` ignored on every route
**Where.** `core/identifiers.py:59-63`; 0 of 153 served ops declare it; the FGA gate splits with the
server delimiter too. **Closes it.** Request-scoped delimiter dependency feeding `parse_identifier` and
`canonical_object_id`.

### A5 · Error bodies without `code`
**What.** 422, generic 500, FastAPI 404/405, maintenance 503, 413, 429 and draining 503 all collapse to
`InternalError 18` in the client; tag/branch failures are unmapped 500s (codes 8/9/11/22/23 unreachable);
column/data ops never mint 14/20. **Where.** `service_kit/lakehouse/ns_errors.py:135-161`,
`api/maintenance_mode.py:31-43`, `dataplane.py:1345-1418`. **Closes it.** One coded problem+json builder
for every status (the four hand-built ones in `body_limit.py`, `load_shed.py`, `draining.py` fold in).

### A6 · Identity: `x-api-key` never read; bearer verified only with OIDC on
**Where.** `api/security.py:36-49,168-181`, `core/config.py:181` (`LANCE_OIDC_ENABLED=False`).
**Closes it.** See §F; anonymous-by-default ends.

### A7 · Governance inside spec handlers
**What.** Warehouse-scoped namespace refusal, no root tables, trash soft-delete, protection 409/code 3,
lineage keys injected into schema metadata, implicit BTREE on merge_insert, insert pre-coercion,
maintenance 503 on POST reads, update/delete ignoring `branch`. **Closes it.** R2; each refusal
re-expressed with the spec's own code; `branch` honoured (plumbing at `dataplane.py:1085`).

### A8 · Stub status codes
**Where.** `views.py:24,58`, `columns.py:146` lack 201/202. One-line fix each.

### A9 · 0.12.0: merge_insert `on` is an array
**Where.** `data.py` merge handler declares `on: str | None`; the wire form is a repeated query
parameter. **Closes it.** `on: list[str]`; pass through to pylance.

### A10 · 0.12.0: bump `lance-namespace` and re-vendor `lance_docs`
**What.** 0.11.1's vector index build params are dropped by the 0.11.0 pydantic model before the backend
sees them; 0.12.0's `computed` columns + backfill are a spec-level data-evolution contract rask answers
501 to; `header.*` context mapping is now specified both directions. **Closes it.** Pin 0.12.0, re-vendor
the spec and the current blob/object-store/versioning guides, re-run conformance.

### A11 · The conformance test that defines "verbatim"
**What.** `tests/integration/test_spec_conformance.py` pins (method, path) only; no test constructs
`lance.namespace.RestNamespace`, lancedb `namespace_client_impl="rest"` or lance-ray namespace mode
against a running catalog (the urllib3 client is exercised through rask's own transport wrapper only).
**Closes it.** One suite that drives every op with the three stock clients. A1–A10 land behind it.

---

## B. The governed commit path and the management API (R1, R2)

### B1 · Advertise and govern managed versioning
**What.** Version routes are mounted and FGA-gated (`_BATCH_PATHS`, `_action_relation` → `can_write_data`)
but carry no lineage, gate, protection or replay marker, and `managed_versioning` is never advertised;
the real governed door is the non-spec `/commit` doing a direct pylance commit under root creds.
`DirectoryNamespace` implements `create_table_version` and enforces the staged-manifest protocol (probed);
`batch_commit_tables` is `UnsupportedOperationError`. **Where.** `endpoints/versions.py`, `data.py:326-364`,
`dataplane.py:556-637`. **Closes it.** Lineage/gate/marker/protection on `CreateTableVersion`;
`managed_versioning=true` in `DescribeTable`; `/commit` aliased then removed; `batch_commit_tables`
backed by rask's own staged-manifest KV (the dir backend will not provide it).

### B2 · Carve the management API
**What.** 25 route groups to move: `/commit`, `/credentials`, `/publish`, `/protection`, `/undrop`,
`/maintenance/*`, `/policy/*`, `/access/*`, `/history`, `/blobs`, `/v1/warehouses`, `/v1/projects`,
`/v1/model`, `/v1/access`, `/v1/events`, `/v1/me`, `/v1/user-state`, `/v1/stores`, plus non-spec query
params, headers (`X-Lance-Run-Facets`, `x-lance-originator`) and dialects. Full list:
`lance-conformance-and-build-rules.md` §4.

### B3 · Conflict classification on every mutating door
**What.** `_classify_commit_error` (400 incompatible / 409 retryable / 503) wraps only
`commit_appended_fragments`; update/delete/column ops let a Lance conflict escape as 5xx.
**Where.** `dataplane.py:578-596, 945-972, 992-1029`. **Closes it.** One classifier exported from
`service_kit.lancekit.writer` (the catalog keeps a duplicate today) applied on every door.

---

## C. Format-aware governance — what makes DIY worth it (R3–R9)

### C1 · Per-base credential vending
**What.** The vendor refuses any table whose fragments carry a `base_id` (feature-flagged) instead of
vending per base; no package seam can carry a `session_token`, so vended STS creds cannot even travel
through `lance_storage_options`, `s3_filesystem`, `records._s3_client` or `storage.s3_client`.
**Where.** `endpoints/credentials.py:76-130`, `core/vending.py:213,278`, `service_kit/lakehouse/objectfs.py:21-57`,
`storage/client.py:76-90`. **Closes it.** `session_token` in every storage-options builder; vend the union
of `base_paths` with per-base rights (read on inherited bases, write on `target_bases`, never on
reference-only bases); expiry in the vended options.

### C2 · Branch-scoped operations, FGA, vending, protection, lineage
**What.** The model has `can_create_branch: owner` on `table` and nothing else; branch writes fall through
to the table's `can_write_data`; update/delete write main; vending, protection, trash and lineage are
branch-blind; the catalog's branch and tag doors **emit no lineage at all**, so no notification ever
fires for a branch or tag. **Where.** `model.fga:357,349`, `fga_deps.py:108`, `endpoints/branches.py`,
`tags.py`. **Closes it.** `type branch { parent:[table]; reader/writer; can_write_data }` + `.fga.yaml`
cases; branch prefix in vending; lineage facets `parent_branch`/`parent_version`; per-branch protection
and trash records.

### C3 · Cross-dataset pins for clones and branches
**What.** Maintenance's base-refs pre-pass protects a clone's source only when the clone sits in a
maintained bucket (deactivated warehouses and unlisted buckets are invisible); **the catalog's on-demand
`/maintenance/run` and `/compact` have no such guard and destroy a live shallow clone**; there is no
lineage edge for a clone. **Where.** `catalog/services/maintenance.py:91-124`,
`maintenance/.../base_refs.py:38-42,90`, `sweep.py:145-155`. **Closes it.** Record the clone/branch →
(source, version) edge at creation; tag-pin the source version; every GC door (sweep, purge, on-demand)
consults the registry; enumerate referrers over all registered buckets including deactivated.

### C4 · External-base lifecycle policy and cleanup identity
**What.** Ingest registers the source **bucket** as a base, so flag 16 makes the orphan scan refuse the
dataset, `report_is_clean` blocks every purge, and `protected_roots` protects the whole bucket; whether
`cleanup_old_versions` reclaims external in-base blobs is unverified against pylance 10.0.0 (the post says
yes, the docstring says nothing). **Where.** `ingest/adapters.py:296-306`, `lander.py:311-325`,
`maintenance/.../orphans.py:294-295`, `purge.py:223-224`, `features.py:113-114`.
**Closes it.** Parse `BasePath.is_dataset_root`; per-base `managed`/`reference-only` policy on the
warehouse record; cleanup credentials without delete on reference-only bases; a RED test pinning
pylance's behaviour on external blobs under a registered base.

### C5 · Storage profiles as bases
**What.** The estate runs "one endpoint, one key" (`config.py:60-62,101-102`); a warehouse-rooted
connection swaps only `root`. **Closes it.** `initial_bases` + `base_<id>.<key>` options on the warehouse
record; `target_bases` on the write doors; `aws_provider_scheme` once pylance ships it.

### C6 · Tiers as shallow clones plus columns (R9)
**What.** `compute.py` re-materialises managed blob bytes per tier; external descriptors are forwarded.
**Closes it.** After C3: silver = shallow clone of bronze@N + `add_columns`; measure bytes and latency
against the copying path on one corpus before adopting.

### C7 · Descriptor-first reads (R8) and `read_blob_ranges`
**What.** rask's `/blobs` door streams `take_blobs` chunks with Range/ETag; query responses do not expose
the descriptor struct by default. **Closes it.** Descriptor struct on Arrow responses, `all_binary` on
opt-in; document `read_blob_ranges` as the batched client path once creds are vended.

### C8 · Repack and branch maintenance in the sweep
**What.** `compact_files` never passes a `compaction_mode`; nothing repacks packed sidecars; datasets
under `tree/<branch>/` are never compacted, optimized or cleaned (`optimize.py:123-127`).
**Closes it.** Discover branch datasets; add repack; pin that compaction does not rewrite dedicated blobs.

### C9 · Feature flags 32 / 64 / 128
**What.** `features.py` whitelists 1|2|4|8 (+16 for GC), names 64, does not know 32 or 128; comments say
the spec stops at 16. Refusal is fail-closed and correct; the decision is Q5. **Closes it.** Name the
bits, decide 32, add 128 once an index declares covering columns.

---

## D. Edge and service doors (from the gateway/compute/controlplane sweep)

### D1 · Two services fully open through the gateway — **HIGH**
**What.** The gateway enforces no authn/authz on any row; `controlplane` (`GET /api/projects`: tenant
names, teams, namespaces, hosts) and `compute` (`/api/ray/*`, `/api/serve`: topology, job entrypoints,
node log files) have no door of their own; the Ingress routes `/api` to the gateway and the front-door
policy admits from anywhere. **Where.** `gateway/__init__.py:317-360`, `controlplane/.../routes.py:36-44`,
`compute/.../routes.py:24-67`, `proxy.py:58-59`, `chart/templates/ingress.yaml:66-72`,
`network-policy.yaml:251-275`. **Closes it.** `make_auth_deps` (OIDC + FGA reader on the root object) on
both routers and the Serve proxy.

### D2 · Sidecar-only blocklist is a partial hand-list
**What.** Only `lineage-events` and `lineage-reconcile-cron` are blocked; the root rewrites expose
`/api/lineage/lineage-dlq`, `/api/catalog/control-events`, both `/dapr/subscribe`, `/ui/*`, `/demo/*`;
with `APP_API_TOKEN` unset the route guards no-op. **Closes it.** Invert to an allowlist per
root-rewritten row (catalog `/v1/*`; lineage `/runs`, `/events`, `/v1/*`).

### D3 · No body cap, rate limit, request-id, forwarded-for, access log, or coded errors at the edge
**Where.** `gateway/__init__.py:280,332,341,347`. **Closes it.** Streaming body-size middleware,
token bucket per subject/IP, `RequestIDMiddleware`, strip inbound `X-Forwarded-*` and inject at the edge,
one structured access line per request, problem+json with `code` for 404/413/429/502.

### D4 · Compute's prune route does not fail closed; Serve proxy path unbounded
**Where.** `compute/.../pruner.py:43-67`, `proxy.py:53`, `ray_kit/dashboard.py:674`. **Closes it.**
`assert_app_token_configured` in compute's lifespan; reject dot-segments in `_canonical`.

### D5 · Compute is an introspection shell: none of the three BYO seams exists on it
**What.** No submit door with vended creds, no idempotent outcome door, no plan document on a control
lane; `submit_or_reattach` exists only as library code used in-process by the medallion.
**Closes it.** The two BYO artefacts from `lakehouse-analysis.md` §11 D, exposed on the management API.

---

## E. Lineage (from the lineage sweep)

### E1 · Lost origination events are unrecoverable and invisible — **HIGH**
**What.** The reconcile sweep enumerates the graph, not storage, and skips nodes without `source_uri`;
every HTTP producer swallows failures. A lost `create/declare/register` means the table never exists in
lineage; a lost write on a known table is back-filled version-only. **Where.** `lineage/core/reconcile.py:169-172`,
`catalog/core/lineage_emit.py:598-604`, `lineage_kit/emitter.py:193-197`. **Closes it.** Enumerate the
catalog registry / warehouse roots; create the Dataset vertex from on-disk `lineage.dataset_id`; R10.

### E2 · Bus door trusts a producer-stamped author behind one shared token — **HIGH**
**Where.** `lineage/api/dapr.py:302-310`, `services/consumer.py:33-35`, `core/config.py:70-79`.
**Closes it.** Q6; `enforce_output_authz` as the stamped subject on the bus door too.

### E3 · Run state regresses on out-of-order ingest
**Where.** `repository.py:98-110` (last-delivery-wins), fed by outbox drain and DLQ replay.
**Closes it.** Sticky terminal state; a START-after-COMPLETE test.

### E4 · `GET /events` is a lossy subset of the graph, and notifications replays from it
**Where.** `ingest.py:261-264`, `consumer.py:445-454`, `repository.py:1295-1296` (20 000-row prune per
insert). **Closes it.** Feed row inside the ingest transaction; time-based retention.

### E5 · Unbounded growth, O(history) hot paths, no default pruning
**Where.** `values.yaml:404` (`runRetentionDays: 0`), `repository.py:352-354,660,711`, no index on
`Run.event_time`, unbounded `*1..` traversals. **Closes it.** `latest_version` on the Dataset node;
index on `event_time`; bounded paths; paginated `/runs`, `/producers`.

### E6 · Model gaps
**What.** Versions are `WROTE` edge properties (one per run+dataset); no branch/tag/clone/base
representation; `parent` facets parsed and discarded; column lineage latest-only; rename strands history
on the old vertex. **Closes it.** Version and branch nodes; persist `parent`; clone edge (C3); rename
carries history.

---

## F. Zero trust — the diff against Lakekeeper (R11)

*Pending: the control-list diff is running at the time of writing and will replace this section.* What
the service sweeps already establish, for the record:

- **Anonymous by default.** `LANCE_OIDC_ENABLED=False`; `x-api-key` never read; the gateway forwards
  `Authorization` verbatim and enforces nothing; two services have no door at all (D1).
- **One shared app token** authenticates every sidecar delivery and doubles as the service credential
  presented to lineage; the lineage bus door applies no FGA (E2); notifications' feed lane reads as the
  service principal and needs estate-wide reader to see anything.
- **One static root S3 key** (`rustfsadmin`) for the catalog, maintenance and medallion; vending refuses
  multi-base tables and its STS output cannot pass through the package seams (C1).
- **Raw `AWS_*` keys forwarded into Ray job specs** by `ratch` (`core/jobs.py:47`), visible in the
  dashboard, which is itself reachable unauthenticated (D1).
- **Trust headers** (`x-lance-service-identity`, `dapr-caller-app-id`, `dapr-api-token`) are stripped by
  the gateway (pinned by `test_spoofable_headers.py`) but honoured by every in-namespace door, and the
  namespace network policy admits any pod to any non-store pod.
- **No read audit log** anywhere; `audit()` fires on authn/authz outcomes and tuple writes only.

---

## G. Notifications (from the notifications sweep)

### G1 · Feed-lane coverage depends on the service principal's own grants — **HIGH**
**Where.** `lineage/.../runs.py:116-148`, `notifications/.../reconciler.py:19-21`. **Closes it.** A
service-only ungoverned projection of the feed gated by `can_observe_events`; `can_be_notified` stays the
sole disclosure gate.

### G2 · Unbounded producer strings become permanent retry loops — **HIGH**
**Where.** `models.py:122-135`, `fanout.py:164-176`. **Closes it.** `max_length` on delivery fields; a
permanent outcome for validation faults.

### G3 · Producer `eventTime` is the sort and retention key — **HIGH**
**Where.** `feed.py:61-80`, `inbox_actor.py:299-343`. **Closes it.** Service-side `received_at`; cap
inside `deliver`.

### G4 · Control lane trusted end-to-end with no catch-up path
**Where.** `control_events.py:125-139`, `dlq.py:9-16`. **Closes it.** Reconcile from the catalog's
durable audit trail; verify `object_id` against `object_type` and the actor app-id.

### G5 · Bare 500s and a blocking sidecar wait per call
**Where.** `proxies.py:98-119`. **Closes it.** Map transport errors to 503 problem bodies; one proxy
factory in the lifespan.

### G6 · No erasure, no retention for watches/prefs/cursor, no reverse index for a subject
**Where.** `watch_actor.py:1-7`, `models.py:322-335`, `inbox_actor.py:444-446`. **Closes it.** A
delete-subject door that sweeps inbox, prefs, watches and the sent ledger; TTLs.

Also: `.claude/skills/rask-notifications/SKILL.md` contradicts the code in eight places (reason count,
line refs, `lease_expired`, the feed grant, render on control rows, delivery membership check,
`named_subjects`, the missing `WatchIndexActor`). Fix the skill in the same commit as G1.

---

## H. Maintenance (from the maintenance sweep)

### H1 · On-demand doors destroy live shallow clones — **HIGH** (see C3)
### H2 · Bucket-granular external bases freeze purge and protect whole buckets — **HIGH** (see C4)
### H3 · Clone protection bounded to maintained buckets — **MEDIUM-HIGH** (see C3)

### H4 · No lease, no deployment strategy, unpersisted retry state
**Where.** `routes.py:61`, `maintenance.yaml:41-66`, `trash.py:80-91`, `purge.py:442-445`, `sweep.py:204-206`.
**Closes it.** Conditional-put lease per tick and per dataset (`records.create_json` exists); `attempts`
and `last_refusal` on the trash record; `strategy: Recreate`.

### H5 · No per-object GC audit
**Where.** `routes.py:80`, `sweep.py:486-524`, `endpoints/maintenance.py:39-56`. **Closes it.**
Per-dataset structured log plus a `table_maintained` control event from sweep and doors.

### H6 · Purge deletes any sub-prefix a trash record names; root key everywhere
**Where.** `purge.py:326-340,380-385`, `maintenance.yaml:123`, `values.yaml:1517`. **Closes it.** Verify
the location is a Lance root before `delete_dir`; a maintenance identity scoped per warehouse (C4/§F).

---

## I. Shared packages (from the packages sweep)

### I1 · `ratch` is an ungoverned direct write path in the production head image — **HIGH**
**Where.** `ratch/core/driver.py:99-103,280,317,355`, `core/dataset.py:73-81`, `lineage.py:1-5`,
`.docker/ray-cluster.dockerfile:62-69`, `pyproject.toml:70`; also `runtime_env.pip` (the rejected
pattern), `allow_external_blob_outside_bases=True`, modality literals (`FTS_LANGUAGE="Swedish"`,
institution column names). **Closes it.** Q4; commits through `CatalogTableWriter`; strip modality
literals into a runner.

### I2 · Vended credentials cannot pass through any seam — **HIGH** (see C1)

### I3 · Both emit kernels swallow; only the medallion has an outbox — **HIGH** (R10)

### I4 · The FGA model cannot express the verdict's rungs — **MEDIUM-HIGH**
**What.** No `branch`, `column`, `base`, `estate` type; bootstrap is a configured root warehouse plus
out-of-band tuples (`provision()` writes none). **Closes it.** C2's `branch`; a column-policy relation
(§J3); an `estate` root with `can_create_project`; `.fga.yaml` cases; `_CHILD_EDGE_PARENT_TYPES`.

### I5 · Duplicated seams
**What.** Two conflict classifiers; two emit kernels with three producer strings; ingest hand-maps 409;
`storage/client.py:102` is a verified no-op; three boto3 constructors; two S3FileSystem constructors with
different scheme logic. **Closes it.** B3, R10, one `s3_client`, delete the dead line.

### I6 · Untested seams
`objectfs.py`, `lakehouse/blobs.py`, `lancekit/store.py`, `lancekit/reader.py` REST path, `audit.py`,
`middleware.py`; ratch's ingest/materialize/indexing/search/jobs; `submit_or_reattach`'s delete branch.

---

## J. Governance features a lakehouse buyer expects (none exist)

| # | Feature | Today | What closes it |
| --- | --- | --- | --- |
| J1 | Read audit log (subject, table, version, columns, when) | none; `lineage_reads` covers lineage's own endpoints only | `audit()` on every data-read door via a service-kit middleware; retention; an index on dataset |
| J2 | Right to erasure end to end | Lance row delete only | propagate to blob sidecars (reachability GC), clones/branches (C3), and tags pinning old versions; a delete-subject door in notifications (G6) |
| J3 | Column-level policy (mask/deny) | column *lineage* only; `columns.py` has no FGA check | `column` relation in `model.fga`; masking on query and descriptor-first reads |
| J4 | Change feed for BYO consumers | Lance has row versions | `changes since version N` door and event on the control lane |
| J5 | Encryption at rest options per warehouse | none in code or chart | `aws_server_side_encryption` / `aws_sse_kms_key_id` on the warehouse record and in vended options |
| J6 | Schema evolution governance | raw pylance errors | compatibility check; breaking change requires owner; schema history door |
| J7 | Distributed index build as a governed job | synchronous in request handlers | `create_index_uncommitted` / commit-segments protocol behind an index-job record |
| J8 | Quotas and storage accounting | none | per-project/warehouse accounting; branch-by-root gives per-directory cost for free |
| J9 | PII / sensitivity classification | a `pii` key in seed data | classification on the dataset node; policy keyed on it |
| J10 | Backup and tested restore of the control root | chart snapshot for RustFS and Postgres | a documented, exercised restore of projects, warehouses, bindings, trash |
| J11 | In-flight blob-byte admission budget | none (catalog counts requests) | 503 + `Retry-After` on every blob door (from the lance-context spec) |

---

## K. The Dapr retreat (D5) — sequenced after A–D

From `dapr-coupling-analysis.md`: 7 of 12 blocks used; 40/480 source files import the SDK, 171 name it;
actors 2 481 LOC (annotator, notifications only); workflows 3 407 LOC; 240/769 tests; 36/53 chart
templates. Replacement map per block is in that document and in each sweep's §5. Order: secrets (OpenBao
direct) → pub/sub (JetStream durable consumers, `Publisher` protocol behind `dapr_publish`) → state
(JetStream KV with CAS; notifications' actors become KV rows with revision CAS) → bindings (in-process
scheduler + KV lease) → invocation (plain HTTP + mTLS) → workflow last (BYO engine on the event plane;
`promotion_review` becomes a record + door + scheduled message).

---

## L. Runtime hygiene from the Lance guide (unchanged from the digest)

Shared `lance.Session` in every Lance-plane process (catalog opens ~24 bare datasets per request path);
`LANCE_CPU_THREADS` / `LANCE_IO_THREADS` / `LANCE_LOG` in the Ray `runtime_env`; `instrument_lance_metrics`
once per process (ingest, viewer, search, annotator never call it); unenforced primary key on the ingest
`id`; branch/tag name validation at the door; blob thresholds pinned on every create path; `allow_http`
derived from the endpoint scheme; HTTP client timeouts.

---

## M. What needs more investigation before a decision

| # | Question | How to answer it |
| --- | --- | --- |
| M1 | Does `cleanup_old_versions` on pylance 10.0.0 delete external blobs under a registered base? | RED test: external blob under `initial_bases`, cleanup, assert the object survives. Decides C4's default. |
| M2 | Does Lance honour a tag that pins a *branch* version during main cleanup? | Test on `tree/<branch>/` with a root tag; matters once C8 maintains branches. |
| M3 | Bytes and latency of tiers-as-clones vs copying on one corpus (R9). | `scripts/` measurement against the medallion's blob path. |
| M4 | Blob v2 default thresholds: post says 64 KB / 4 MB, guide says 16 KiB / 2 MiB, rask measured 64 KiB / 4 MiB. | Keep pinning; re-measure on each pylance bump. |
| M5 | Per-base `base_<id>.<key>` storage options and `aws_provider_scheme` in pylance 10.0.0. | `base_store_params` exists; the keyed form and provider scheme are unconfirmed in the installed build. |
| M6 | Put-if-not-exists on every store rask might run on (RustFS verified; COS/GooseFS need commit locks per the guide). | A per-store CAS probe in the warehouse validation endpoint. |
| M7 | Shared-base cleanup safety: no test proves cleanup on a dataset sharing a non-root base spares its sibling. | Add the test before C5 ships. |
| M8 | Whether MemWAL server-id sharding is a fit for append-only bronze landing (coordinator-free ingest). | Prototype after K; blob v2 columns read `None` through the MemWAL scanner today. |
| M9 | Ray Serve / dashboard exposure once D1 lands: which dashboard reads are still needed by the compute zone. | Enumerate the zone's calls; keep only those behind FGA. |
| M10 | The `x-api-key` key store (Q7) and its rotation model. | Design note in the management API RFC. |
| M11 | Upstream: pylance's GET routes (A3) and the 0.12.0 `header.` vs `headers.` prefix in the bundled client. | File the two issues; track the fix version. |
| M12 | Whether branch tags need CAS (`_set_tag` is unconditional at every layer, incl. pylance's `Tags::update`). | Object-store conditional put on `_refs/tags/<name>.json`; verify RustFS honours `If-Match` there. |

---

## N. What was asked of the owner and is still open

Q1–Q7 above. Nothing else blocks §A–§C. §F is filled in when the zero-trust diff completes.
