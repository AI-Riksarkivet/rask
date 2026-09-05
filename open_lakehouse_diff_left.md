# open_lakehouse_diff_left — the governed Lance lakehouse: verdict, decisions, backlog, zero-trust diff, open questions

> **Naming note (2026-09-02).** This document was written against `feec956` (2026-08-25), before the
> X10 hard rename. Every `LANCE_*`/`LINEAGE_*`/`MEDALLION_*`/`MAINTENANCE_*` auth variable it quoted
> is now `RASK_*`; the names have been rewritten in place so the repo-wide retired-name gate stays
> meaningful, and because a retired name in prose sends an operator to a variable that binds nothing.
> The line references are unchanged.


**Counted 2026-09-05, from the rows below rather than asserted: 124 tracked, 113 open, 11 struck.**
That splits into 58 lettered rows (52 open) and 54 rows in the Q sections — § Q2 carried from
`open_estate-verification.md`, § Q3 from `open_python-audit.md`, § Q4 recorded from the first e2e run
against the deployed estate. Re-derive the counts when
you change them; the previous header claimed a freshness date two days older than rows struck beneath
it, and a header nobody re-counts is how a register stops being evidence.

THIS IS NOW THE ESTATE'S ONLY LAKEHOUSE BACKLOG. Its two siblings were drained and deleted
(`73b171d7`, `058da189`); what they were and what they found is in `docs/DECISIONS.md` under
"The Python estate audit" and "A repeating condition is a LEVEL, not an event".

SCOPE, so rows stop accumulating that nobody here will do: this file tracks the LAKEHOUSE — the Lance
catalog, the medallion cascade, maintenance, lineage, storage governance. Edge rows moved to
`open_gateway.md` (2026-09-05). A row about the compute plane, the frontend or the annotator belongs
in its own register, and D5 below is the one knowingly left in place — it is compute-plane work the
current goal defers rather than a row this file should own.

The body was written against `feec956` (2026-08-25); line references are from there and rows landed
since carry their own dates. **Delete this file when the backlog is drained.** `docs/` is for settled architecture only.

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
lineage, maintenance, gateway-compute-controlplane, packages}.md`. Zero-trust diff: `sweeps/zero-trust.md`, folded into §F.

The control plane and the future `rask-operator` are a separate plan, `open_controller.md` (2026-09-02), and
moving maintenance execution out of the lakehouse process LANDED — see `docs/DECISIONS.md`, "The
lakehouse cloud-native cutover"; this register stays on
the lakehouse itself.

Scope the owner set: catalog, compute, ingest, medallion, maintenance, lineage, notifications, gateway,
controlplane, and the shared packages. **Not swept, on the owner's instruction:** annotator, viewer,
search, flows, models, and every frontend zone.

## How this was produced

Three multi-agent workflows (state + Lakekeeper diff, robotics lessons, Lance docs + 54-op conformance)
with adversarial verifiers: 24 claims, 23 confirmed, 1 refuted, several tightened. Then five
single-service sweeps against one nine-point rubric (two more, annotator and viewer/search, were started
and stopped when the owner narrowed the scope) plus the zero-trust control diff (how it touches the lakehouse, authorization,
lineage/events, state, Dapr coupling, format awareness, governance gaps, tests, top findings), each
citing file:line. Live probes where a claim rested on runtime behaviour (the catalog app under the dir
backend, pylance 10.0.0 `RestNamespace` against a logging stub, `DirectoryNamespace` version ops).
Every number below is from those reports; nothing is from memory.

## Freshness — read this before acting on any row

This register was written against `feec956`, dated **2026-08-25**. On 2026-09-02, 359 commits
separate it from `main`, and driving the estate showed at least one HIGH item already closed before
the register existed. **Every row below is a claim about a tree that no longer exists; check it
against HEAD, and where it is runtime behaviour drive it, before starting work.** Rows already
re-checked carry a dated **Status** line. Known stale as of 2026-09-02: D1 (done 2026-08-26), the
data-door half of A7/C2 (done 2026-08-31/09-01), the request-id half of D3.

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
| R11 | **Zero-trust posture is an explicit target**, measured against the control list in §F, not a claim. | Lakekeeper does not actually use the term; it implements a control set that rask matches or beats on 9 of 19 rows and misses on 1, with 8 partial. |

### Decisions the owner still has to make

| # | Question | Options | Default if unanswered |
| --- | --- | --- | --- |
| Q1 | Where the five analysis documents live. | Committed under `docs/audits/lakehouse-2026-09/` on `claude/flyte-2-dapr-audit-19cyc2` (the default was taken; move them if you prefer elsewhere). | done |
| Q2 | Delete remote branch `claude/flyte-2-dapr-audit-19cyc2`? | **Decided: delete.** Everything is on `main`. The sandbox proxy refuses `git push --delete`, so the owner runs it from a machine with push rights. | — |
| Q3 | UNSUPPORTED error status: 501 or 406? | **Decided: 406, per the spec.** lance-namespace v0.12.0 `spec.yaml` defines `UnsupportedOperationErrorResponse` as status 406 ("Not Acceptable / Unsupported Operation") on every op that lists it, and Lance's own reference server maps `ErrorCode::Unsupported` to `NOT_ACCEPTABLE` (`rust/lance-namespace-impls/src/rest_adapter.rs:347`). rask's 501 (`service_kit/lakehouse/ns_errors.py:25`) parses in the client because it dispatches on `code`, but a spec-verbatim server answers 406. Folded into A5. | — |
| Q4 | ~~`ratch` console script~~ | withdrawn: `packages/ratch` was dissolved 2026-08-28 and is not on `main`; the packages sweep read untracked residue on the sandbox. | — |
| Q5 | Feature flags 32 / 64 / 128? | **Decided, per the current format doc and `lance-table/src/feature_flags.rs`.** The rule is asymmetric: readers check `reader_feature_flags`, writers check `writer_feature_flags`, and an unknown bit on the side you are on is an "unsupported" error. So: (1) name all three bits in `features.py` (32 `FLAG_DISABLE_TRANSACTION_FILE`, writer-required only; 64 `FLAG_UNSTABLE_DATA_OVERLAY_FILES`, both, and release builds reject it unless `LANCE_ENABLE_UNSTABLE_DATA_OVERLAY_FILES` is set, so rask never sets that in a deployed image; 128 `FLAG_COVERED_INDEX_METADATA`, both, sticky). (2) Split the whitelist into a reader mask and a writer mask: report-only passes (orphan scan, reconcile, base-refs) proceed when only writer-required bits are unknown; compaction, cleanup and purge stay fail-closed on any unknown writer bit. (3) Support for 32 lands together with the pylance bump that can read manifest-recorded transactions, because rask's replay marker and `/history` read `.txn` files through `read_transaction`; pylance 10.0.0 predates 32 and 128 entirely (no symbol in the installed package), so today refusal is the only correct answer. Folded into C9. | — |
| Q6 | Which service door authenticates producers on the lineage bus? | **Decided (owner delegated, 2026-09-02):** Dapr mTLS SPIFFE `dapr-caller-app-id` enforced by an `accessControl` policy while Dapr is the transport; a producer signature over the CloudEvent as the transport-independent form that survives the Dapr retreat (§K). The bus door applies `enforce_output_authz` as the stamped subject either way. | — |
| Q7 | The `x-api-key` principal? | **Decided (owner delegated, 2026-09-02):** support both spec identity headers; keys are minted, scoped and revoked by the management API (a key = a `user`/service principal in FGA with an expiry), never by the spec surface. Both Q6 and Q7 stay in this backlog: the bus door is the integrity of the lakehouse's write record, and `x-api-key` is the spec's own identity contract. | — |

---

## A. Spec-verbatim (D3) — the eleven blockers

Measured: 12 of 54 ops verbatim, 34 partial, 3 model-differs, 5 stub. With pylance 10.0.0's bundled
`RestNamespace`, 5 ops are unusable and 4 answer silently wrong. Vendored spec is v0.9.0; current is
v0.12.0. Details and evidence: `lance-conformance-and-build-rules.md` §2–§3, §9.

### ~~A1 · Bodyless handlers ignore the required JSON body — **DONE 2026-09-02** (`a6d2032e`)~~
**DONE 2026-09-02** (`a6d2032e`). All nine now declare the spec body; a present body field wins and the
query aliases stay as a fallback. It also closed a hole it had opened elsewhere: `stats`, `index/list`
and `index/{n}/stats` had been given a `branch` QUERY parameter to REFUSE a branch-scoped read, and a
spec client sends `branch` in the body, so all three answered 200 from main until this landed.
**What.** `DescribeTable`, `ListTableIndices`, `GetTableStats`, `DescribeTableIndexStats`, the exists/
deregister/transaction ops read version/tag/branch/vend_credentials/pagination from the query string or
not at all. The reference client sends `vend_credentials` only in the body, so **credential vending is
unreachable by any spec client**. **Where.** `services/catalog/src/catalog/api/v1/endpoints/tables.py:270-283,903-907`, `indices.py:97-114`.
**Closes it.** Declare the request model as the body on every op, `reconcile_body_id` uniformly, body
wins over rask's query aliases; a wire-level test posting each spec body.

### ~~A2 · Three response shapes the client cannot parse — **DONE 2026-09-02** (`aa57350c`)~~
`count_rows` answers a JSON integer, both plan doors a JSON string (encoded AS a string, so a plan that
looks like JSON is not mistaken for structure), `schema_metadata/update` the direct map. Three tests
that pinned the deviation were rewritten.
**What.** `schema_metadata/update` answers the wrapped envelope (spec: direct `{str:str}`); explain/
analyze answer `text/plain` (spec: JSON string); `count_rows` answers `text/plain` (parses by accident).
**Where.** `columns.py:229-234`, `data.py:698-720`. **Closes it.** Direct map, `JSONResponse` strings,
JSON integer; the envelope dialect moves to the management API.

### ~~A3 · GET vs POST on `count_rows` and `tags/list` — **DONE 2026-09-02** (`9e3844b5`; the dual-mount made the OpenAPI non-deterministic and was fixed in `fa4ee8f8`, now gated by `test_the_openapi_contract_is_deterministic.py`)~~
**DONE 2026-09-02** (`e2f0…`). Both dual-mounted `GET` + `POST`. The upstream one-liner in lance is
still worth filing: its bundled client and reference server disagree with its own spec.
**What.** The spec and lance-namespace's generated client say POST at every tag since 0.9.0. **pylance's
own bundled client and reference server use GET** (`lance` repo `rust/lance-namespace-impls/src/rest.rs`,
`rest_adapter.rs`, at v10.0.0 and main). **Closes it.** Dual-mount both routes; file the upstream issue.

### A4 · `delimiter` ignored on every route — **HALF DONE 2026-09-02**
**Status.** The silent half is closed: a `delimiter` this server does not use is now refused 400
(coded 13, naming the server's own) by a ROUTER-level guard, so a client configured with `.` gets a
message it can act on instead of a real table reported 404. Refusing rather than honouring is
deliberate: honouring means threading the client's delimiter through `parse_identifier` AND
`fga.canonical_object_id`, and deciding authorization against a differently-spelled object is a worse
failure than the one being fixed. **Remaining:** the full form — honour it, with the FGA
canonicalisation designed.
**Where.** `core/identifiers.py:59-63`; 0 of 153 served ops declare it; the FGA gate splits with the
server delimiter too. **Closes it.** Request-scoped delimiter dependency feeding `parse_identifier` and
`canonical_object_id`.

### A5 · Error bodies without `code` — **HALF DONE 2026-09-02** (`f1ee42d3` framework 404/405 carry the spec code; `0699bac3` `Unsupported` answers 406). REMAINING: tag/branch dataplane failures still surface as unmapped 500s, so codes 8/9/11/22/23 stay unreachable, and the column/data ops never mint 14/20.
**MOSTLY DONE 2026-09-02.** Two halves landed. `f1ee42d3`: FastAPI's own 404/405 went out as
`{"detail": ...}` with no `code`, so the reference client reported `InternalError 18` — a
`StarletteHTTPException` handler now stamps `Unsupported` (the honest code for "this backend does
not serve that operation"), with a status→code fallback for the statuses the spec does have a code
for, registered BELOW the domain handler so a `TableNotFound` still answers code 4. And Q3:
`UNSUPPORTED` is **406**, the spec's own status and the one Lance's reference server uses; ten
assertions and four prose sites that pinned 501 were rewritten. The 422 and the generic 500 were
already coded — the register was stale on those. **Remaining:** the tag/branch dataplane failures
that surface as unmapped 500s (codes 8/9/11/22/23 unreachable), and column/data ops never minting
14/20.
**What.** 422, generic 500, FastAPI 404/405, maintenance 503, 413, 429 and draining 503 all collapse to
`InternalError 18` in the client; tag/branch failures are unmapped 500s (codes 8/9/11/22/23 unreachable);
column/data ops never mint 14/20; UNSUPPORTED answers 501 where the spec and Lance's reference server
answer 406. **Where.** `service_kit/lakehouse/ns_errors.py:25,135-161`,
`api/maintenance_mode.py:31-43`, `dataplane.py:1345-1418`. **Closes it.** One coded problem+json builder
for every status (the four hand-built ones in `body_limit.py`, `load_shed.py`, `draining.py` fold in), and
`UNSUPPORTED → 406` (Q3, decided).

### A6 · Identity: `x-api-key` never read; bearer verified only with OIDC on
**Where.** `api/security.py:36-49,168-181`, `core/config.py:181` (`RASK_OIDC_ENABLED=False`).
**Closes it.** See §F; anonymous-by-default ends.

### A7 · Governance inside spec handlers
**Status 2026-09-02.** The *"update/delete ignoring `branch`"* clause is closed and wider than
written: `update`, `delete`, `insert`, `merge_insert`, the merge's index build, `schema_metadata/update`
and `count_rows` honour `branch`; `query`, `explain_plan`, `analyze_plan`, `create_index`,
`create_scalar_index`, `stats`, `index/list`, `index/{n}/stats` refuse it (see C2). The rest of A7
stands.
**What.** Warehouse-scoped namespace refusal, no root tables, trash soft-delete, protection 409/code 3,
lineage keys injected into schema metadata, implicit BTREE on merge_insert, insert pre-coercion,
maintenance 503 on POST reads, update/delete ignoring `branch`. **Closes it.** R2; each refusal
re-expressed with the spec's own code; `branch` honoured (plumbing at `dataplane.py:1085`).

### ~~A8 · Stub status codes — **DONE 2026-09-02** (`9e3844b5`)~~
**DONE 2026-09-02.** 201 / 202 / 202 declared on the three decorators, asserted through the OpenAPI
(all three answer 501 today, so no live call can exercise the success status).
**Where.** `views.py:24,58`, `columns.py:146` lack 201/202. One-line fix each.

### A9 · 0.12.0: merge_insert `on` is an array
**BLOCKED ON A10, measured 2026-09-02.** Widening only the door was tried and reversed: the installed
0.11.0 model types `on` as `str` with `MinLen(1)`, so a list fails validation INSIDE the request model
and a 0.12.0 client gets a pydantic error one layer deeper — which reads as a rask bug rather than a
version skew. The current single-key contract is now pinned so the change lands WITH the bump.
**Where.** `data.py` merge handler declares `on: str | None`; the wire form is a repeated query
parameter. **Closes it.** `on: list[str]`; pass through to pylance.

### A10 · 0.12.0: bump `lance-namespace` and re-vendor `lance_docs`
**Status 2026-09-02.** Stands — `service-kit` pins `lance-namespace>=0.11.0` and 0.11.0 is installed.
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
**Status 2026-09-02 — the data doors are done, the governance half is not.** Nine data doors were
driven against the live catalog with the object store as ground truth and fixed (commits `7dddbd94`
… `34aad854`, `e61abc0a`); `tests/e2e-py/test_track_a_acceptance.py` pins them and runs in CI under
`scripts/e2e_stack.sh`'s no-silent-skip guard; `services/catalog/tests/test_a_declared_branch_is_never_silently_dropped.py`
refuses a new door that hands a branch to `native.call` undecided. Still standing: the FGA `branch`
type, vending scoped to `tree/<b>/`, per-branch protection and trash, `parent_branch`/`parent_version`
facets, and the branch/tag doors emitting no lineage.
**A correction that A1 makes visible.** On `stats`, `index/list` and `index/{n}/stats` the refusal was
added as a QUERY parameter. Those routes declare no body, so the spec's `{"branch": …}` body is still
dropped by FastAPI and answered from main. The e2e tests send `?branch=` and are green over that open
channel. The fix is A1 — declare the request model as the body — not a second patch here.
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

### C9 · Feature flags 32 / 64 / 128 (Q5 decided)
**What.** `features.py` whitelists 1|2|4|8 (+16 for GC), names 64, does not know 32 or 128; comments say
the spec stops at 16. Refusal is fail-closed and correct today. **Closes it.** Name the three bits with
their reader/writer requirement; split `SUPPORTED` into a reader mask and a writer mask so report-only
passes proceed on writer-only unknowns while compaction, cleanup and purge stay fail-closed; never set
`LANCE_ENABLE_UNSTABLE_DATA_OVERLAY_FILES` in a deployed image; support 32 only with the pylance bump
that reads manifest-recorded transactions (the replay marker and `/history` depend on `read_transaction`).

---

## D. Edge and service doors (from the gateway/compute/controlplane sweep)

### ~~D1 · Two services fully open through the gateway — **DONE 2026-08-26, verified live 2026-09-02**~~
**Status.** Stale when written: `1e9acf06` (2026-08-26 19:17, one day after `feec956`) gave both
services `security.py`, `routes.py`/`proxy.py`/the `projects` router carry `Depends(require_read)`
(estate `reader` on the root object), both lifespans `attach_auth`, and the chart renders
`governedAuth: true` for both. Driven 2026-09-02 through the deployed gateway with no token:
`/api/ray/health`, `/api/ray/jobs`, `/api/serve/applications/`, `/api/projects/`, `/api/ray/cluster`
all **401** with a coded problem body. Kept for the record; nothing to do.
**What.** The gateway enforces no authn/authz on any row; `controlplane` (`GET /api/projects`: tenant
names, teams, namespaces, hosts) and `compute` (`/api/ray/*`, `/api/serve`: topology, job entrypoints,
node log files) have no door of their own; the Ingress routes `/api` to the gateway and the front-door
policy admits from anywhere. **Where.** `gateway/__init__.py:317-360`, `controlplane/.../routes.py:36-44`,
`compute/.../routes.py:24-67`, `proxy.py:58-59`, `chart/templates/ingress.yaml:66-72`,
`network-policy.yaml:251-275`. **Closes it.** `make_auth_deps` (OIDC + FGA reader on the root object) on
both routers and the Serve proxy.

### ~~D4 · Compute's prune route does not fail closed; Serve proxy path unbounded — **DONE 2026-09-02, verified live**~~
**Status.** Both halves were worse than written and both are closed (`a95ca7e5`). Measured on the
deployed pod before the fix: the Dapr SIDECAR held `APP_API_TOKEN` and stamped every delivery while
the APP container held none — the chart rendered it only for `daprIngest`/`lanceWriter` services —
so `require_dapr_token` compared each delivery against an empty string and the prune route was open
to any pod in the namespace. Now: compute's lifespan calls `assert_app_token_configured`, compute
carries `daprIngest: true`, and `test_every_pod_whose_app_fails_closed_on_the_app_token_is_given_one`
turned RED on the code change alone and green on the values change — the two halves cannot drift
apart again. `ray_kit.dashboard.proxy` refuses any empty, `.`, `..` or dot-decoding segment with
400 before a URL is built (reproduced offline first: `%2e%2e` was decoded and forwarded as
`api/serve/../v0/logs/file/`). Driven on the deployed estate after the roll (image `d4-205851`,
release rev 90): app container `APP_API_TOKEN` set, boot `startup_complete`, unsigned
`POST /compute-prune-jobs-cron` → **403**, wrong token → 403, correctly signed → 200, and the
encoded traversal now dies at authentication (401) before the proxy is reached.
**Release note.** `helm upgrade --wait` timed out at the 9 min I gave it (`make k3s-up` allows 20)
and left rev 90 marked `failed`; every release resource is ready and a `failed` (not `pending-*`)
release does not block the next upgrade, which will clear it.
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

> MERGED into **Q3-10** — the same defect (unbounded list reads with no server-side LIMIT) was tracked here and in the Python-audit ledger under two ids. Q3-10 is canonical: it carries the finding id and severity the audit assigned. Kept as a pointer rather than deleted, because this section's framing is how the defect was first seen.
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

**Premise check first.** Lakekeeper never uses the words "zero trust" anywhere in its repository (docs, README, crates). What it claims is "secure", "every request is checked against your policy before any data is read, and recorded", vended credentials or remote signing, and "does not issue API-Keys". So this section diffs what Lakekeeper *implements* against what rask implements, control by control, at Lakekeeper v0.13.1 (b328e58) and rask `feec956`. Full report with every citation: `docs/audits/lakehouse-2026-09/sweeps/zero-trust.md`.

**Score.** 19 controls: rask HAVE 6, STRONGER 3, PARTIAL 8, MISSING 1 (per-workload storage identities: catalog, maintenance and every mover run as the RustFS root user). Both sides lack image signing and SBOM. Lakekeeper is stronger on service identity (one Kubernetes SA per service, no shared secret), location containment and audit request-ids; rask is stronger on token validation, vending posture, mTLS on the Dapr plane, pod hardening, network policy and fail-closed behaviour.

**The twelve items in F2 are the zero-trust backlog.** Items 1–4 (per-workload storage identity, fail-closed in code, kill the shared service bearer, stop laundering anonymous browser reads into a service identity) are the ones that decide whether the claim is honest.

**Framing fact first:** Lakekeeper never uses the words "zero trust" anywhere — not in docs, README, or crates (`grep -rniE "zero[- ]?trust"` over the whole tree returns nothing). Its actual claims are "secure" (README.md:18), "Every request is checked against your policy before any data is read, and recorded" (README.md:18), vended-credentials/remote-signing (README.md:48), and "does not issue API-Keys" (docs/docs/authentication.md:11). Everything below is therefore *what Lakekeeper implements*, not what it calls zero trust.

### F1 · The control list — Control list — Lakekeeper vs rask

| # | Control | Lakekeeper (claim/impl, cite) | rask (cite) | rask status |
|---|---|---|---|---|
| 1 | Every request authenticated; no anonymous default | **Impl, but anonymous by default in OSS.** Authn is on only if `OPENID_PROVIDER_URI` / `OPENID_PROVIDERS` / `ENABLE_KUBERNETES_AUTHENTICATION` set (docs/configuration.md:201-207); otherwise `Actor::Anonymous` and only a `tracing::warn!("Authentication is disabled…")` (crates/lakekeeper/src/service/authn.rs:403; serve.rs:67-72 passes `None`). Only *Lakekeeper Plus* refuses to start without an authenticator (configuration.md:209). | Code default is anonymous: `oidc_enabled` default False (services/catalog/src/catalog/core/config.py:181); `authenticate()` returns `None` and every route opens (api/security.py:66-67, docstring :3). Chart default flips it ON: `auth.enabled: true` (chart/values.yaml:695-696) → `RASK_OIDC_ENABLED=true` via `lance.governedOidcEnv` (_helpers.tpl:1059-1061), pinned by tests/unit/test_invariants.py:1475-1490. FGA requires OIDC (config.py:350). | **PARTIAL** — same shape as LK OSS (code default open, deployment default closed). Gap: no in-code refuse-to-boot like LK Plus. |
| 2 | Token validation: issuer, audience, alg, transport | **Impl (external crate).** Audience *optional* — "If unset, audience validation is skipped" (configuration.md:243, :271); issuer from discovery + `ADDITIONAL_ISSUERS`; required-claim rules, scope (configuration.md:245-246, 297-346). JWT crypto lives in the external `limes` crate (Cargo.toml:132), not in-repo. K8s `TokenReview` audience also optional ("all tokens proceed to validation!" configuration.md:221-223). | Audience and issuer **required** when enabled (config.py:348); asymmetric-only alg allowlist (packages/service-kit/src/service_kit/governed/oidc.py:51); HTTPS required for issuer + JWKS unless `allow_insecure` (oidc.py:19-20, :108); leeway configurable (config.py:189). Chart derives `RASK_OIDC_ALLOW_INSECURE` from a plain-http `dex.issuer` default (`http://rask-dex:5556/dex`, values.yaml:2125; _helpers.tpl:1069). | **STRONGER** on validation policy; **PARTIAL** on transport (default IdP is plaintext in-cluster). |
| 3 | Service-to-service auth | **Impl.** Kubernetes SA tokens via `TokenReview`, one identity per SA `kubernetes~<uid>` (authentication.md:526-565). No shared service secret exists; "Lakekeeper does not issue API-Keys" (authentication.md:11). | Service door = shared Dapr `APP_API_TOKEN` + caller-supplied `x-lance-service-identity` checked against an allowlist (governed/dapr_auth.py:237-293; catalog api/security.py:78-160). ONE Secret per release for all sidecars (chart/templates/dapr-app-token.yaml; _helpers.tpl:210). Docstring admits: "with one shared token across an allowlist, any holder can pick the highest-privileged name on it" (dapr_auth.py:255-258); only `privileged_subjects` get a dedicated credential (dapr_auth.py:282-289). Web pods hold the shared token (chart/templates/frontends.yaml:251-256) and the BFF spends it for **anonymous GETs** as `frontend.serviceIdentity` (frontend/packages/api/src/bff.ts:191-195; runs-feed.ts:213-217), which is on `LINEAGE_SERVICE_SUBJECTS` (chart/templates/services.yaml:425-445). `x-api-key` is read nowhere (grep over services/packages/chart: 0 hits). | **PARTIAL** — LK is stronger (per-SA identity, no shared secret). |
| 4 | No implicit trust of identity headers; edge strips | LK has no identity headers to forge. It does trust `x-forwarded-*` by default for URL building (configuration.md:13, :29 `USE_X_FORWARDED_HEADERS` default true) and records `user_agent` unverified (logging.md:83). | Gateway strips `dapr-caller-app-id`, `dapr-api-token`, `dapr-app-id`, `x-lance-service-identity` on every route (services/gateway/src/gateway/__init__.py:66-76, applied :340); catalog refuses the door for a public caller (security.py:102-106); absent header ≠ public (dapr_auth.py:51-67). **Can an external caller forge them through the gateway? No** — verified by services/gateway/tests/test_spoofable_headers.py:52-105 (incl. casing, duplicate-header binding). Residual: anything reaching a pod *not* via the gateway (web BFF, any in-namespace pod) is unstripped, and NetworkPolicy is off by default (values.yaml:559). | **HAVE at the edge / PARTIAL in-cluster.** |
| 5 | AuthZ on every object, policy engine, deny-by-default, managed access | **Impl.** OpenFGA; additive grants; owners get all incl. grant; Managed Access strips `grant` from owners and inherits down (authorization-openfga.md:20-36, 129-139); server `admin` cannot read project data (:40); sign endpoint authorizes per request (crates/lakekeeper/src/server/s3_signer/sign.rs:127-144). | Router-level `authorize` maps every guarded route to a `can_*` relation (services/catalog/src/catalog/api/fga_deps.py:554-600); fails closed 503 on unwired client or OpenFGA outage (fga_deps.py:561-562; :63-64, :86-87); model has `managed_access`/`managed_access_inheritance`/`pass_grants` (governed/auth/model.fga:116-163, 218-226). Gaps: id-less routes need only authn (fga_deps.py:577-578); top-level create is open unless `fga_lock_root_create` (values.yaml `lockRootCreate: false`, :698); FGA default False in code (config.py:196). | **HAVE** (parity, arguably stronger on fail-closed). |
| 6 | No static storage creds to clients; scoped short-lived vending | **Impl.** STS AssumeRole with per-table inline session policy (`{key}*` + `s3:ListBucket` prefix condition, crates/lakekeeper/src/service/storage/s3.rs:1258-1312); TTL `sts-token-validity-seconds` default 3600 (s3.rs:104,842; storage.md:303); remote signing authorized per request (sign.rs:139-144); per-warehouse credential; live validation check `vended-credentials-scope-enforced` (storage/validation.rs:84-88; storage.md:86-88); `client-managed` opt-out (storage.md:107). | Modes (core/vending.py): `mode_b` vends nothing — **chart default** (values.yaml:744; config.py:278); `sts`/`web_identity` build a per-table, per-tier session policy (vending.py:94-124) with TTL 900 (config.py:279) and refuse to boot without an STS endpoint so the caller's token never goes to public AWS STS (config.py:357-369); `web_identity` exchanges the **caller's own** JWT (vending.py:263-283). **Nothing hands out the estate's root key** — `static` would hand out long-lived keys (vending.py:134-155) but `make_vendor` never passes `static_keys` (main.py:128-134) so it always returns `None`: a dead mode. Reachability: `POST /v1/table/{id}/credentials` (api/v1/endpoints/credentials.py:44) is a rask extension; a pure Lance-Namespace-spec client never sees a credential and uses server-mediated Arrow IPC. Write tier re-checks `can_write_data` (credentials.py:62-72); every issuance audited (:102-110). | **STRONGER by default posture; PARTIAL as a feature** (RustFS has no real AssumeRole per vending.py:170-172; `static` is dead; scope follows a caller-supplied `location`, see row 12). |
| 7 | Catalog's own storage identity least-privilege; per-warehouse identities | **Impl.** One credential per warehouse (storage.md:7; production.md:21 "distinct credentials that only grant access to the prefix"); system identity must use `assume-role-arn` + `external-id` by default (configuration.md:50-51); location-exclusivity check (validation.rs:60-62). | **One root key for the whole fleet**: catalog gets `rustfs.accessKey` = `rustfsadmin` (chart/templates/services.yaml:92-94; values.yaml:1517-1518 — the RustFS root user); maintenance identical (maintenance.yaml:123-125; services/maintenance/src/maintenance/core/config.py:88 default `"rustfsadmin"`); medallion producer and every mover identical (medallion.yaml:234-236, 389-391). OpenBao only changes *where the secret value comes from* (services.yaml:93-99), not *which identity* it is. | **MISSING** — largest single gap. |
| 8 | Secrets backend | **Impl.** Postgres `pgp_sym_encrypt(... 'cipher-algo=aes256')` (crates/lakekeeper-storage-postgres/src/secrets.rs:68,130); default key literally "This is unsafe" (configuration.md:65); Vault KV2 option; secrets cached 10 min (configuration.md:500-506). | OpenBao behind a Dapr `secretstores` component scoped per app-id (chart/templates/dapr-component.yaml:277-340); catalog fails closed at boot on a store miss (config.py:170-174; main.py:99). Defaults: `openbao.devMode: true` = in-memory, root token `root` (values.yaml:2148-2152), `tls_disable = 1` (templates/openbao.yaml:34); prod overlay requires `devMode: false` (values-prod.yaml:123-124) and then refuses dev creds (infra-credentials guard, values-prod.yaml:120-122). | **HAVE** (architecture) / **PARTIAL** (defaults). |
| 9 | TLS everywhere / mTLS between components | **Claim, not impl.** "Lakekeeper does not terminate connections natively. Please use a reverse proxy" (production.md:22); PG `sslmode` configurable (configuration.md:75-76); OpenFGA endpoint example is `http://` (configuration.md:382); outbound TLS validated via webpki + native certs (configuration.md:594-600). No mTLS anywhere. | Dapr Sentry mTLS pinned ON (values.yaml:1947-1952) → sidecar↔sidecar hops (service invocation, pub/sub, actors) encrypted with SPIFFE ids. Everything **not** via a sidecar is plaintext: catalog→OpenFGA `http://` (_helpers.tpl:1076); catalog→RustFS `http://` + `LANCE_S3_ALLOW_HTTP=true` (services.yaml:101-102; `lance.s3Endpoint` _helpers.tpl:653-655); NATS `nats://` (_helpers.tpl:703-705); OpenFGA + Dapr-state Postgres `sslmode=disable` (templates/infra-credentials.yaml:43; external-secrets.yaml:53; openbao.yaml:171); lineage DSN carries no sslmode (services.yaml:302, with the password in env when OpenBao is off); OpenBao `http://` with `skipVerify: true` derived from the scheme (dapr-component.yaml:283,303; openbao.yaml:34); Dex issuer `http://` (values.yaml:2125); ingress `tls: []` (values.yaml:1701). | **PARTIAL** — mTLS exists (stronger than LK) but only on the Dapr plane; every store hop is plaintext by default. |
| 10 | Audit logging (who/what/resource/decision, request id) | **Impl.** Structured audit events `event_source="audit"` with actor, action, entity, decision, `request_id` (uuid7 via `set_x_request_id`, crates/lakekeeper/src/api/router.rs:255), `idempotency_key`, `user_agent` (logging.md:49-89). Docs contradict on default: "enabled by default" (logging.md:35) vs `LAKEKEEPER__AUDIT__TRACING__ENABLED` default `false` (configuration.md:676). Grant writes audited separately (logging.md:139). No tamper evidence. | Dedicated `lance.audit` logger (governed/audit.py:22-50); authn success/failure (security.py:103-175), every authz decision incl. batch (fga_deps.py:63-93), and credential issuance (credentials.py:70,104-110) are audited; default ON (config.py:222). No request/trace id on audit records (audit.py:42-50); no tamper evidence. | **HAVE** (parity; rask lacks request-id correlation). |
| 11 | Secrets never logged / not exported | **Impl.** `SetSensitiveHeadersLayer([AUTHORIZATION])` (router.rs:256-258); header/body logging opt-in via `debug.log_authorization_header` / `log_request_bodies` (config.rs:1074; router.rs:320-395 logs `request_body`/`response_body` at `debug!`); `#[redact]` on secret fields (crates/authz-openfga/src/config.rs:90-95; storage/az/credentials.rs:16-22). Audit logs carry PII and live idempotency keys — documented (logging.md:345, :404). | S3 secret is a `SecretStr` (config.py:165); 5xx messages redacted (service_kit/lakehouse/ns_errors.py:87-98); no OTel header capture configured (no `CAPTURE_HEADERS` env in chart); the medallion `"token"` in logs is an idempotency key, not a secret (medallion/services/produce.py:62-75); viewer logs the secret *name* only (viewer/api/v1/endpoints/objects.py:91). Vended creds appear only in the response body. Gaps: `LINEAGE_DATABASE_URL` with password in pod env when OpenBao is off (services.yaml:302); `age.password: lance` default (values.yaml:2091). | **HAVE** (no evidence of token/URL egress; presigned URLs are not used anywhere — vending returns keys, not URLs). |
| 12 | Anti-SSRF / user-supplied storage URLs | **Partial impl.** Endpoint scheme must be http/https (s3.rs:1368-1371, 1411-1416); no private-IP/link-local block (grep `is_private|loopback|169.254` → 0 hits); location-exclusive across warehouses (validation.rs:60-62); tables must sit under warehouse location (storage.md:24). | Storage endpoint is operator-only; client `storage_options` explicitly refused (api/v1/endpoints/data.py:119-121); `data_base` allowlist (data.py:123-125); reserved buckets refused on warehouse create (api/v1/endpoints/warehouses.py:164, 641). But `register_table` forwards `body.location` with **no** location/bucket check in the Python door (api/v1/endpoints/tables.py:514-536; Rust dir-impl behaviour not verified) — the catalog then opens it with root creds (credentials.py:84) and vends session creds scoped to *that* prefix. | **PARTIAL** — LK is stronger on location containment. |
| 13 | Bootstrap protection | **Impl.** Bootstrap runs once; concurrent/second bootstrap refused (crates/lakekeeper/src/api/management/v1/server.rs:260-275); first token becomes admin; with auth off "no admin is set" (bootstrap.md:18-22). | `chart/templates/bootstrap-admin.yaml` + `auth.bootstrapAdmin` (values.yaml:702); root object has no auto-seed (config.py:225-231); top-level create open unless `lockRootCreate` (fga_deps.py:591-594 "None => open top-level create"). Bootstrap Job internals not read. | **PARTIAL** (not fully verified). |
| 14 | Destructive-op protection, soft delete, idempotency, maintenance mode | **Impl.** Soft-delete per warehouse + protection + `force` override (concepts.md:144-241); `Idempotency-Key` per spec (configuration.md:656-668); read-only maintenance mode (:643-653). | Trash + undrop (service_kit/lakehouse/trash.py:1-12), protection records (lakehouse/protection.py; tests/unit/test_drop_protection.py:134-268), all owner-gated (fga_deps.py:104-162); `LANCE_MAINTENANCE_READ_ONLY` (config.py:283); commit idempotency by `run_id` (services/catalog/tests/test_commit_idempotency.py:49-97) but no `Idempotency-Key` header (grep → 0). | **HAVE** (parity). |
| 15 | Request limits / rate limiting | Body 32 MB, 30 s timeout (configuration.md:610-617; router.rs:270-273). No rate limiter. | Body limit 256 MiB, pure-ASGI 413 (api/body_limit.py:1-28; config.py:285); concurrent-write load-shed 429 + Retry-After (api/load_shed.py:7-96; config.py:287; values-prod.yaml:84-92); Dapr `Resiliency` CRs (dapr-resiliency.yaml:26,116). | **HAVE** (slightly more than LK). |
| 16 | Network segmentation | Nothing in this repo (Helm chart is a separate repo `lakekeeper-charts`, not cloned — **not verified**). Docs only recommend OpenFGA co-location (production.md:17). | Default-deny Ingress+Egress + DNS + targeted store allows (chart/templates/network-policy.yaml:1-50); `networkPolicy.enabled: false` by default (values.yaml:559), ON in values-prod.yaml:82-83; needs an enforcing CNI. | **HAVE (prod) / PARTIAL (default)** — stronger than anything in LK's repo. |
| 17 | Internal doors all require the app token? Dapr access control | N/A | `require_dapr_token` on sidecar-delivered routes and actor callbacks (dapr_auth.py:70-102, 301-341); catalog/lineage service door checks the token (dapr_auth.py:274-293); components scoped per app-id (dapr-component.yaml:19,44,113,152,233,265,336). **Not** every internal door: ordinary `/v1/*` routes are OIDC-guarded, so with OIDC off they are open (security.py:66-67); no Dapr `accessControl` policy exists (grep `accessControl` over chart → 0; the only `Configuration` is tracing/retention, observability.yaml:50-56) so any sidecar may invoke any app-id. Token no-op when `APP_API_TOKEN` unset in dev (dapr_auth.py:98-102) but boot refuses that when Dapr ingest is on (:105-114). | **PARTIAL**. |
| 18 | Supply chain: non-root, read-only FS, signing, SBOM | Distroless `nonroot` images (docker/bin.Dockerfile:3,32-35; full.Dockerfile:47,78). Release builds with `--provenance=false` and no cosign/SBOM/attestation (.github/workflows/release.yml:200,209; grep → nothing else). No pod securityContext in repo (chart elsewhere). | Every image `useradd … --uid 10001` + `USER` (.docker/gateway.dockerfile:62,79; rest-catalog.dockerfile:68,93; ray-cluster.dockerfile:114,167; frontend.dockerfile:87,116; only cnpg-age-ext.dockerfile:16 is `USER root`, a CNPG base image). Restricted securityContext + `readOnlyRootFilesystem: true` default (_helpers.tpl:952-970; values.yaml:520-521); sidecar seccomp (_helpers.tpl:748). Scanners: osv-scanner, trivy config+image, trufflehog (Makefile:255-297; ci.yml:124,156-189). No cosign signing, no SBOM. | **STRONGER on hardening; both MISSING signing/SBOM.** |
| 19 | Compute-plane auth (rask-specific) | N/A | Ray dashboard token auth required in prod, fails render otherwise (values-prod.yaml:128-135; tests/unit/test_ray_auth.py:176-191). | HAVE |

### F2 · What rask must add to honestly claim zero trust (ordered)

1. **Per-workload storage identities.** chart/templates/services.yaml:92-94, maintenance.yaml:123-125, medallion.yaml:234-236 & 389-391, services/maintenance/src/maintenance/core/config.py:88 — stop running catalog, maintenance and every mover as the RustFS root user; provision one least-privilege RustFS user/policy (or STS role) per service, scoped to its buckets/prefixes.
2. **Fail closed in code, not only in the chart.** services/catalog/src/catalog/core/config.py:181 (`oidc_enabled=False`) and api/security.py:66-67 — default to enabled and add an explicit `LANCE_INSECURE_ALLOW_UNAUTHENTICATED` escape (LK Plus's shape, configuration.md:209), so a service run outside the chart is not anonymous.
3. **Kill the one shared service bearer.** governed/dapr_auth.py:291-293 + chart/templates/dapr-app-token.yaml (one Secret for the release) — make every allowlisted subject "privileged" (dedicated credential, dapr_auth.py:283-289) or, better, derive identity from Dapr's mTLS SPIFFE `dapr-caller-app-id` enforced by an `accessControl` policy rather than a copyable token.
4. **Stop laundering anonymous browser reads into a service identity.** frontend/packages/api/src/bff.ts:193-195 and runs-feed.ts:214-217 send the shared token + `frontend.serviceIdentity` when there is no session; that subject is allowlisted at lineage (services.yaml:425-445) — anonymous must be 401 or an explicit `anonymous` FGA principal with visible grants.
5. **Add a Dapr access-control policy and turn NetworkPolicy on by default.** chart/templates/observability.yaml:50-56 (add `accessControl: {defaultAction: deny, trustDomain, policies[]}`), chart/values.yaml:559 (`networkPolicy.enabled: true`).
6. **TLS to every store.** _helpers.tpl:1076 (OpenFGA https + preshared key/OIDC — none configured today, fga.py:295-350 builds `ClientConfiguration(api_url=…)` with no credentials), infra-credentials.yaml:43 / external-secrets.yaml:53 / openbao.yaml:171 (`sslmode=disable` → `verify-full`), services.yaml:302 (lineage DSN sslmode), services.yaml:102 + _helpers.tpl:654 (RustFS https, `ALLOW_HTTP=false`), _helpers.tpl:704 (`tls://` NATS), openbao.yaml:34 + dapr-component.yaml:303 (real certs, `skipVerify: false`), values.yaml:2125 (https Dex), values.yaml:1701 (ingress TLS).
7. **Validate `register_table` locations.** services/catalog/src/catalog/api/v1/endpoints/tables.py:514-536 — require the location to sit under the namespace's warehouse root and outside `reserved_bucket_set`, as warehouses.py:164/641 already does; otherwise a writer attaches any prefix the root key reaches and credentials.py:73-89 vends scoped creds to it.
8. **Delete or wire the dead `static` vending mode.** services/catalog/src/catalog/main.py:128-134 never passes `static_keys`, so core/vending.py:134-155 silently returns `None` — a configured mode that does nothing is a false control.
9. **Refuse well-known defaults in the base chart, not only on `devMode=false`.** values.yaml:1517-1518 (`rustfsadmin`), :2091 (`age.password: lance`), :1915 (dev app token), :2148-2152 (`openbao.devMode: true`) — generate at install or fail render.
10. **Correlate audit records.** governed/audit.py:42-50 — add request id + trace id (LK has uuid7 request ids on every audit event, router.rs:255; logging.md:70-76) and ship the `lance.audit` stream through the Collector to an append-only sink.
11. **Lock root create by default.** values.yaml:698 (`lockRootCreate: false`) + fga_deps.py:591-594 — top-level namespace creation is open self-serve for any authenticated subject.
12. **Sign and attest images.** .github/workflows/ci.yml:124-189 has scanners only — add cosign keyless signing + SBOM attestation (LK lacks this too, and even disables provenance, release.yml:200).

### F3 · Where rask is already stronger than Lakekeeper

1. **Token validation policy.** Audience *and* issuer are mandatory when auth is on (config.py:348), signing algorithms are an asymmetric-only allowlist (oidc.py:51), and issuer/JWKS must be HTTPS unless explicitly overridden (oidc.py:108). Lakekeeper skips audience validation when unset (configuration.md:243, :271) and does its JWT work in an out-of-tree crate (Cargo.toml:132).
2. **Vending posture.** Default `mode_b` vends nothing (values.yaml:744) so no storage credential ever leaves the catalog unless an operator opts in; STS TTL 900 s vs LK's 3600 (config.py:279 vs s3.rs:104); boot refuses an STS mode without an explicit endpoint so the caller's JWT can never be POSTed to public AWS STS (config.py:357-369); every issuance is audited with subject/resource/tier (credentials.py:102-110); `web_identity` exchanges the caller's own token instead of the catalog's (vending.py:263-283).
3. **In-repo runtime hardening and edge discipline.** Dapr Sentry mTLS pinned on (values.yaml:1947-1952), restricted PodSecurity + `readOnlyRootFilesystem` for every app container (_helpers.tpl:952-970), default-deny NetworkPolicy with exclusive store ingress (network-policy.yaml:1-50, prod-enabled values-prod.yaml:82-83), a gateway that strips every trust header with tests proving duplicate/casing variants cannot smuggle (gateway/__init__.py:66-76; test_spoofable_headers.py:52-105), and fail-closed OpenFGA/secret-store outages (fga_deps.py:561-562; config.py:170-174). Lakekeeper's repo carries none of this (its chart is elsewhere) and its release pipeline explicitly disables provenance.

### F4 · Tests that exist for these controls

**Lakekeeper (in-repo):**
- Audience config parsing: crates/lakekeeper/src/config.rs:1777-1787; debug header/body logging defaults off: config.rs:1972-2014. JWT signature/issuer/audience tests are in the external `limes` crate — none in this tree.
- Vended-credential scope: live validation check (storage/validation.rs:79-88, exposed as `vended-credentials-scope-enforced`, storage.md:86-88) — an operator-run probe, not a unit test; STS/multipart vending tests s3.rs:2626, 2925, 3162; storage/mod.rs:2065-2259.
- AuthZ: `test_managed_access_warehouse_inheritance_{user,role}`, `test_load_table_hidden_table_denied`, `test_load_generic_table_credentials_hidden_*_denied`, `test_batch_authorization_all_denied`, `test_move_namespace_denied_*`, `test_openfga_client_credentials_with_scope` (crates/lakekeeper-integration-tests, crates/authz-openfga).
- Remote signing: sign.rs:1303-1681 are parser/URL tests only — no in-crate test that an unauthorized sign is refused.
- Cache/identity headers: router.rs:730-757 (`responses_are_private_and_vary_on_the_request_identity`).
- Bootstrap once: server.rs:260-275 (logic; no dedicated test found).

**rask:**
- Header spoofing at the edge: services/gateway/tests/test_spoofable_headers.py:52,69,83,105.
- Public-caller laundering and proxied humans: tests/unit/test_catalog_gateway_proxied_human.py:89-212 (incl. `test_public_caller_cannot_launder_even_while_holding_a_valid_bearer`, `test_anonymous_through_the_gateway_is_unauthenticated_not_permitted`).
- Service door: services/catalog/tests/test_service_door.py:96-175 (unconfigured door 401, empty allowlist, shared token cannot claim privileged subject, unreadable store 503); tests/unit/test_dapr_auth.py:28-107; packages/service-kit/tests/test_actor_route_guard.py.
- OIDC verifier: tests/unit/test_oidc_verify.py:192-401 (expired, wrong aud, wrong iss, bad sig, HS256, alg=none, split-horizon).
- Vending scope: tests/unit/test_vending.py:33-224 (`test_a_tenants_policy_denies_another_tenants_bucket`, `…SIBLING_table…`, `test_a_read_tier_policy_cannot_write_its_own_table`, real AssumeRole at :159).
- AuthZ fail-closed + audit: tests/unit/test_invariants.py:620, 642, 652, 873; auth-on-by-default and session-secret refusal :1475, :1493; every app-token-consuming pod gets one :1407; Dapr secrets through the store :982.
- Protection/trash: tests/unit/test_drop_protection.py:111-291; namespace/trash guards `test_namespace_trash_guard.py`, `test_trash_purge.py`, `test_maintenance_trash_exclusion.py`.
- Secrets fail-closed: tests/unit/test_medallion_secrets.py:25-57; `test_secrets.py`, `test_media_s3_secret.py`.
- Body limit / idempotency: `test_body_limit.py`; services/catalog/tests/test_commit_idempotency.py:49-97.
- Ray auth in prod: tests/unit/test_ray_auth.py:78-191.
- **No rask test** covers: per-service storage identity (there is none), TLS on store hops, Dapr access-control policy, `register_table` location containment, or the BFF anonymous-read service-door path.

**Scope notes (things I did not verify):** Lakekeeper's Helm chart (separate repo) for pod security/NetworkPolicy; the Rust `DirectoryNamespace.register_table` location check behind pylance; rask's `bootstrap-admin.yaml` Job internals; where `LINEAGE_API` in the zones resolves (direct service vs gateway) for the BFF service-door reads.

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

### I1 · ~~`ratch` ungoverned write path~~ — withdrawn
**What.** `packages/ratch` was dissolved 2026-08-28 (`open_ray-kernel.md`) and is absent from `main`; `.docker/ray-cluster.dockerfile` builds from the root lock. The packages sweep audited untracked residue. The one transferable point survives as I5/L: no service may open a governed table with bare pylance outside the catalog's doors.
### I2 · Vended credentials cannot pass through any seam — **HIGH** (see C1)

### I3 · Both emit kernels swallow; only the medallion has an outbox — **HIGH** (R10)

> MERGED into **Q3-13** — the same defect (two OpenLineage kernels and four RunEvent builders) was tracked here and in the Python-audit ledger under two ids. Q3-13 is canonical: it carries the finding id and severity the audit assigned. Kept as a pointer rather than deleted, because this section's framing is how the defect was first seen.

### I4 · The FGA model cannot express the verdict's rungs — **MEDIUM-HIGH**
**What.** No `branch`, `column`, `base`, `estate` type; bootstrap is a configured root warehouse plus
out-of-band tuples (`provision()` writes none). **Closes it.** C2's `branch`; a column-policy relation
(§J3); an `estate` root with `can_create_project`; `.fga.yaml` cases; `_CHILD_EDGE_PARENT_TYPES`.

### I5 · Duplicated seams

> MERGED into **Q3-14** — the same defect (three hand-rolled storage_options builders) was tracked here and in the Python-audit ledger under two ids. Q3-14 is canonical: it carries the finding id and severity the audit assigned. Kept as a pointer rather than deleted, because this section's framing is how the defect was first seen.
**What.** Two conflict classifiers; two emit kernels with three producer strings; ingest hand-maps 409;
`storage/client.py:102` is a verified no-op; three boto3 constructors; two S3FileSystem constructors with
different scheme logic. **Closes it.** B3, R10, one `s3_client`, delete the dead line.

### I6 · Untested seams

> MERGED into **Q3-38** — the same defect (`ray_kit.submit` has no test) was tracked here and in the Python-audit ledger under two ids. Q3-38 is canonical: it carries the finding id and severity the audit assigned. Kept as a pointer rather than deleted, because this section's framing is how the defect was first seen.
`objectfs.py`, `lakehouse/blobs.py`, `lancekit/store.py`, `lancekit/reader.py` REST path, `audit.py`,
`middleware.py`; `submit_or_reattach`'s delete branch.

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
| ~~J7~~ **CLOSED 2026-09-04** | Index build off the request handler | ~~synchronous in request handlers~~ — `create_index` / `create_scalar_index` publish one `IndexWorkItem` and answer with its id, which is the spec's OWN model (`CreateTableIndex`: "index creation is handled asynchronously") | NOT the commit-segments protocol: measured on pylance 10.0.0, an index segment carries no `json`/`to_json`/`serialize`, so unlike `CompactionTask` it cannot cross a process boundary. The whole build moves to the worker instead. **Its own pubsub COMPONENT, not merely its own topic** — `ackWait`/`durableName`/`queueGroupName` are per-component, so a second topic on the work queue inherits its 720s window |
| Low | **`maintenance.indexAckWait` is a 3600s placeholder, not a measurement** — the value should come from a real index build on a real dataset, and the lane has not been driven in-cluster (its unit tests drive it end to end). Carried out of docs/DECISIONS.md "A rename moves a POINTER, not bytes" when that file closed | Both its defect rows CLOSED 2026-09-04; the rename ruling is in `docs/DECISIONS.md` |
| ~~High~~ **CLOSED 2026-09-04** | `rename_table` copied the dataset root inside a request handler | ~~a rename's cost is the DATASET's size~~ — a rename is a `__manifest` POINTER move: register the destination at the source's location, deregister the source. O(1), no byte read or written, and the byte-copy's three failure modes go with it. The answer is lance-ns's own V2 naming rule, measured on the `dir` backend the chart runs | `docs/DECISIONS.md`, "A rename moves a POINTER, not bytes" |
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

Nothing. Q1 taken (documents under `docs/audits/lakehouse-2026-09/`), Q2 decided (owner deletes the branch), Q3 decided (406), Q4 withdrawn, Q5 decided (reader/writer masks, 32 with the pylance bump), Q6 and Q7 decided by delegation. R1–R11 stand unless the owner objects. Nothing blocks §A–§C.

**Decided 2026-09-02:** this file is the single register — `open_backlog.md` is folded into §O below
and deleted. Order of work: D1 (found already done) → §A spec-verbatim, A1–A5 with A11 as the RED gate
→ B1 → C.

---

## O. Folded from `open_backlog.md` (sessions of 2026-08-31 / 09-01) — items not already above

The session ledger that found and fixed the branch family. Rows already expressed by a lettered
section above point at it rather than repeat it.

### O1 · Lakehouse

> MERGED into **Q3-22** — the same defect (the catalog's repeated describes and dataset opens per mutating op) was tracked here and in the Python-audit ledger under two ids. Q3-22 is canonical: it carries the finding id and severity the audit assigned. Kept as a pointer rather than deleted, because this section's framing is how the defect was first seen.

| Priority | Item | Note |
| --- | --- | --- |
| Medium | **No index is ever built on a governed table**; search tunes `nprobes` for one that is not there, so semantic search is a brute-force scan | J7 is the governed version of the fix |
| Medium | **Compression never configured** anywhere, and no decision record; thresholds are schema-resident so it gets dearer with corpus size | |
| Medium | **`register_table` accepts a dataset created without stable row ids**, so `source_rowid` provenance can never be honest and cannot be repaired short of a rewrite. The catalog's own create sets the flag and the ingest gate A14 refuses without it, but A14 guards the ingest path only — `ingest/lander.py:68` says the catalog refuses and it does not. Needs a decision: opt-in by claim (refuse when registering INTO a governed tier) is the shape consistent with D1 | Same door as F2·7 (location containment); fix both together |
| Medium | `_row_last_updated_at_version` unused → publication deltas miss in-place updates, and the annotator's whole write path is `merge_insert` | |
| Medium | 53 `lance.dataset()` call sites, 5 pass a `session` | = L (BR9) |
| Low | Body-id reconciliation on four routes | = A1 |
| Low | `delimiter` silently ignored | = A4; a 400 would be strictly better than silence |
| Low | A subchart names `{{ .Release.Name }}-x` while this chart's Secrets use `lance.fullname`; they agree only when the release is named `rask` | |
| Low | `can_promote` buys nothing on `table` (`validator ⊇ owner`) | |
| Doc | The DIY provenance recipe (`stamp_stage`, `source_rowid`, the tier contract) is written down nowhere under `docs/` or `.claude/` | |
| Open | **Refused, not served** (all 501 today): branch-scoped `query`/`explain_plan`/`analyze_plan`, `create_index`/`create_scalar_index`, `stats`, `index/list`, `index/{n}/stats`. Each needs a faithful mapping and its own tests; the line drawn was the OPTION SURFACE (a fixed-shape op is served, an open option surface is refused) | Named so a 501 never reads as finished |

### O2 · Compute and workflow

| Priority | Item | Note |
| --- | --- | --- |
| In progress | **The executor contract** — `BAKED_JOBS_DIR` + `BAKED_CLUSTER_JOBS` live in the shared library and the catalog enforces them, so a non-Ray lane cannot be declared and the word "Ray" reaches every API client through the published OpenAPI | The agnosticism claim rests on this; D5 is the BYO half |
| Medium | **The `Transform` CRD is DEFERRED to `rask-operator`, not abandoned** — reasoning in `docs/DECISIONS.md`, "The compute plane is decoupled". — docs/DECISIONS.md "The compute plane is decoupled" (§7.4) step 5. A CRD without its controller renders unreconciled CRs as objects stuck mid-provision (`docs/DECISIONS.md` 2026-08-16, re-verified live at `open_estate-verification.md` row 21), so it must not ship in this chart. What it would buy is the declaration living in git with the catalog record as a projection | Carried out of docs/DECISIONS.md "The compute plane is decoupled" when that file closed |
| Medium | **A mover row still carries `stageJob`/`ray_entrypoint`/`ray_job_params` beside the declaration that supersedes them** — two sources of truth for what a lane runs, with `engine_choice` arbitrating. Not removable before there is a seeding path: without one, the default deploy could run no cascade at all. It dies with the row above | Same |
| ~~High~~ **CLOSED 2026-09-04** | ~~No cascade reconciler and no re-run verb.~~ Both exist: the medallion carries a `bindings.cron` reconciler and `POST /api/movers/stages/rerun` re-drives ONE edge on that edge's own rung. Driving C3 in-cluster found it had NEVER worked — a seven-layer chain from a 404 route to a missing credential — of which six layers are fixed and the seventh is `dedicatedServiceCredentials: false`, i.e. row 35 (B) below rather than this row. **Reasoning moved to `docs/DECISIONS.md`, "Cascade repair"; docs/DECISIONS.md "Cascade repair" deleted** | `open_estate-verification.md` row 35 (D), closed with it; row 35 (B) carries the remainder |
| High | **No Dapr Workflow versioning seam** — two replay divergences already shipped; "drain before deploying" is the only safe answer | K sequences the retreat; this is the cost of staying meanwhile |
| Medium | Submission bypasses the `RayJob` CRD, so Kueue admits nothing | |
| ~~High~~ **CLOSED 2026-09-04, PROVEN LIVE** | ~~Maintenance compaction runs in a 512Mi pod while the distributed seam has no executor.~~ M1 split the planner from the workers; M2 consumes the protocol. Verified in-cluster on a Dagger-built image: the planner published 21 units and two dedicated workers consumed 7 and 8 as competing consumers, each vending its own per-table credential — so the bytes were moved by something other than `rask-maintenance`. The protocol itself: `{"read_version":6,"tasks_planned":1,"tasks_executed":1,"tasks_failed":0,"version":8,"fragments_added":1,"fragments_removed":6}`, 300 rows intact, signed by key `536H5FARWTW3GAZV5KOK` where maintenance's own is `rask-maintenance`. It DEGRADES rather than fails and `DatasetResult.compaction_mode` counts it. **M3 (BYO compute via `RayJob` + Kueue) is DEFERRED, behind docs/DECISIONS.md "The compute plane is decoupled" (§7.4) steps 3-4** — the same body of work, not a second one | docs/DECISIONS.md "Cascade repair" deleted 2026-09-04; its live measurements live in the commit that closed it |
| Medium | 1,367 orphan rows in `daprstate`, no TTL, no alert | |
| Medium | The workflow status metric reports success on a dying path | |
| Owner | Ray GCS is not fault-tolerant: a head restart kills in-flight jobs. The platform now degrades in one poll interval instead of 24 h (row 34), but fault tolerance itself needs an external Redis, which this estate refuses by standing rule | |

### O3 · Blocked on the owner

`dedicatedServiceCredentials` — the CHART DEFAULT is `false` (`values.yaml:807`), under which every
mover holds `owner` on every warehouse and the bounding control (`LANCE_PRIVILEGED_SUBJECTS`) is
unrendered. **This estate's release sets it `true`** (verified 2026-09-02: the live catalog renders
`LANCE_PRIVILEGED_SUBJECTS` with the five service subjects), so the question is the DEFAULT posture a
fresh install ships with, not this estate's. F2·3 is the same question from the zero-trust side.

### O4 · Bootstrap on a fresh machine is NOT chart-complete

| Piece | Chart-owned? |
| --- | --- |
| Fleet, lakehouse services, zones, infra toggles, ExternalSecrets | Yes |
| Kueue queues | Yes, and structurally bypassed (O2) |
| **The Ray head the cascade runs on** | **No** — hand-applied `deploy/ray-lance-demo.yaml`, diverged from the chart's own RayService. Re-applying an older copy silently reverted the scoped S3 credential to the root key once; the file now matches the live pod, but a manifest outside the chart is where the security posture drifts |
| OpenBao's Kubernetes auth backend, policy, role | **No** — a runbook, not a manifest |
| The KV secret values | **No** — seeded by hand |

Until the head is reconciled with the chart's RayService and the OpenBao bootstrap is a Job, "it is
all in the chart" is false, and the gap sits exactly where the security posture lives.

---

## P. The dropped-parameter sweep (2026-09-01) — partial, and why

A six-lens sweep drove the live catalog for the class *"a door declares a parameter, accepts it,
forwards it, and something downstream disregards it."* 22 distinct candidates, 53 verdicts returned
(40 real). **Sixteen verify calls and the completeness critic failed on the weekly subagent limit**
(resets 2026-09-04 06:00), so coverage is unassessed and the rows below are candidates, not a
finished list. Re-run the critic when the limit lifts.

Fixed the same day (8): `create_index`, `create_scalar_index`, `explain_plan` (branch nested in
`query`), `describe` (`?branch=` and `?version=9999`), `stats`, `index/list`, `index/{n}/stats`,
`insert?branch=`.

Not addressed — 12 DOORS carrying 14 parameters (a door may drop more than one), by severity as reported:

| Severity | Door | Parameter |
| --- | --- | --- |
| read-from-wrong-target | `POST /v1/table/{id}/publish` → control event `table_published` | `to_version` — the event carries the wrong version |
| read-from-wrong-target | `POST /train` (medallion producer) | `features[].dataset` — the `$n` form is not resolved |
| read-from-wrong-target | `POST /v1/table/{id}/version/list` | `page_token` |
| read-from-wrong-target | `GET /api/search` (search :8102) | `dataset` |
| silently-weaker | `POST /v1/namespace/{id}/create`, `POST /v1/table/{id}/register` | `mode` — 409 whatever the mode |
| silently-weaker | `GET /movers/{mover}/stages/{instance_id}` and its POST | `mover` + `instance_id` — the wrong mover answers |
| silently-weaker | `POST /produce` | the governed-tier claim in `settings` |
| silently-weaker | `GET /api/search` | `mode` |
| silently-weaker | `GET /projects/{project_id}/tasks` (annotator, out of scope) | `limit`, `cursor` |
| cosmetic | `POST /v1/table/{id}/tags/create` | `branch` |
| cosmetic | `POST /v1/table/{id}/branches/create` | `from_branch`, `from_version` |
| cosmetic | `POST /v1/table/{id}/branches/delete` | `name` |

The same session also re-learned two things worth keeping: `version/list` takes `branch` as a
**query** parameter and was never broken — a probe that sent it in the body produced a false
"defect" and a fix that was reverted; and upstream honours `branch` **per operation**
(`describe_table_version` and `batch_delete_table_versions` do, `count_table_rows` did not), so no
static rule can stand in for driving each door.

## Q2. Carried from `open_estate-verification.md` when it was drained (2026-09-05)

That register was 35 rows: 29 CLOSED, 1 OPEN, 5 partial. The closed rows and their evidence live in
the commits they name; what survives is below, one row each, so the file could be deleted without
anything being dropped silently. Its own header said *"Delete when every row is CLOSED. Status is
counted from this file, never asserted elsewhere"* — these counts were re-derived from its table.

| # | Row | Was | What actually remains |
| --- | --- | --- | --- |
| Q2-1 | Lineage e2e: 2 of 9 failing | 11, MOSTLY CLOSED | Two `test_lineage_e2e.py` cases fail against current code. Subsumed by the wider gap: 111 e2e functions across 30 files exist, `make test` excludes them (`-m "not e2e"`), and NOTHING points them at k3s — every "verified live" claim in this repo rests on a manual terminal run. Fixing the two without wiring the suites leaves the class open |
| Q2-2 | Six owner decisions ruled but NOT implemented | 16, PARTLY CLOSED | `CAT-CORE-04`, `ingest-flow-11` (the only one rated *should-decide-soon*), `PS-07`, `catalog-api-17`, `MED-011`, `X1`. Each has a stated default the owner did not object to; none is built. They are `open_python-audit.md` rows and belong with that drain |
| Q2-3 | Three deletion paths never driven live | 19, CLOSED (mostly) | Warehouse and project delete; cascade DETACH + the plural undrop (#96); bucket-purge sole-ownership (`projects_claiming_bucket`). Table-level drop/protect/force/undrop ARE proven — the row's own evidence — so this is the container tier only, which is where `force` and cascade interact |
| Q2-4 | Two non-rask log sources | 25, OPEN | `rask-kueue-controller-manager` TLS handshake errors ~450/min (a third-party operator's webhook cert) and 2× otel-collector scrape failures. Neither is rask code and neither touches the cascade, but the first is loud enough to hide something that does |
| Q2-5 | The movers still write as the RustFS tenant root | 30, CLOSED (partial by design) | The cascade's writes are AUTHORIZED (the mover asks `POST /v1/table/{id}/credentials?tier=write`, `can_write_data` audited) and not SCOPED: the credential vended is the tenant root's. `rask-maintenance` and `rask-ray-compute` are provisioned and scoped (rows 31/32 and `5c11002c`); the movers are the remaining holder. **Needs an owner ruling** — a scoped mover credential must still reach the outbox and `HeadBucket`, which is what row 32 measured as the blocker for the Ray key |
| Q2-6 | `LANCE_FGA_CASCADE_WRITERS` grants every mover `owner` on every warehouse | 35 (B) | The bounding control (`LANCE_PRIVILEGED_SUBJECTS`) now renders on catalog AND lineage (`79512bb0` closed the door asymmetry), but the grant itself is still estate-wide: a mover holds `can_drop`, `can_deregister`, `can_restore` and `manage_grants` on every tenant's warehouse. **Needs an owner ruling** on whether the cascade writer's grant narrows to the warehouses it actually writes |

Row 35 (C) is CLOSED and was stale when written: it said `/bronze-arrival` carries no `from_uri`
"(verified — zero grep hits)", and `ingest_trigger.py:303-305` sets it from `_vended_upstream`
(`d58ffaff`). The operator door does the same as of `bd905e61`, so all three cascade heads now name
the catalog-vended location.

## Q3. Carried from `open_python-audit.md` when it was drained (2026-09-05)

That ledger held 249 distinct findings (498 row entries across a detail table and an index): 384
FIXED, 32 DISSOLVED, 4 WRONG, 74 PARTIAL, 4 OPEN by row entry. Thirty-nine were still live and are
below. What the audit WAS, its final counts and its four structural lessons are in
`docs/DECISIONS.md` "The Python estate audit"; a FIXED finding's reasoning is in the commit that
fixed it, which is where this estate keeps history.

`DUP-08` is NOT carried: its remainder was "the OIDC/FGA settings block is re-declared in 4
services", and eight services now import `GovernedAuthSettings` while none re-declares
`RASK_OIDC_ISSUER`. Closed by X10's rename, which the ledger never re-checked against.

| # | Finding | Sev | What remains |
| --- | --- | --- | --- |
| Q3-1 | `CAT-CORE-13` **OPEN** | med | One 340-line `Settings` carries every domain's configuration |
| Q3-2 | `DUP-15` **OPEN** | med | The Dapr-workflow scheduler is written twice and the copies' timeouts disagree |
| Q3-3 | `VS-07` | med | Five silent swallows in the search path render real failures as empty results |
| Q3-4 | `PS-02` | med | `storage`'s error taxonomy is half-applied — `s3_errors` wraps nothing inside the package |
| Q3-5 | `MAINT-08` | med | `reconcile()`'s `control_root` falls back to the POLICY root, not the control root |
| Q3-6 | `ingest-flow-06` | med | `park_poison` publishes unguarded — one bad unit fails the whole run when the DLQ is down |
| Q3-7 | `catalog-api-07` | low | `_collect_descendants` recurses with no depth cap and no cycle guard |
| Q3-8 | `catalog-api-06` | low | Three tuple write/revoke sites bypass the `seed_ownership` seam |
| Q3-9 | `ING-14` | med | The A8 provenance check fetches the entire unbounded `/runs` board |
| Q3-10 | `F-LIN-04` | med | `list_runs`/`list_datasets`/`list_jobs` are fetch-all with no server-side LIMIT |
| Q3-11 | `ANN-14` | med | Publish transport builds a fresh httpx connection per call, retries one error class |
| Q3-12 | `DUP-14` | med | The same hand-rolled HTTP backoff loop in `packages/storage` and `services/ingest` |
| Q3-13 | `DUP-10` | med | Two OpenLineage kernels and four `RunEvent` builders |
| Q3-14 | `DUP-19` | low | Three hand-rolled `storage_options` builders bypass `lance_storage_options` |
| Q3-15 | `DUP-21` | low | Seven outbound HTTP sites build a fresh httpx client per call |
| Q3-16 | `MED-008` | med | Every outbound call builds its own httpx client — one pool per call |
| Q3-17 | `SK-03` | med | A fresh urllib3 `ApiClient` per catalog read/write, never disposed |
| Q3-18 | `SKG-07` | med | `make_client` returns an aiohttp-backed `OpenFgaClient` with no disposal contract |
| Q3-19 | `SKG-11` | med | Module-level mutable cache in `warehouse_registry` with no bound and no eviction |
| Q3-20 | `MAINT-07` | med | `reconcile()` builds a boto3 client per call inside an `async def` |
| Q3-21 | `MAINT-12` | med | The multi-base gate issues one sequential S3 HEAD per referenced path |
| Q3-22 | `CAT-CORE-09` | med | Each mutating table op performs three namespace describes plus three dataset opens |
| Q3-23 | `VS-16` | med | Voice similarity issues one Lance scan per hit (N+1) and a fresh executor |
| Q3-24 | `SKG-10` | med | Five direct `os.environ` reads outside any Settings class |
| Q3-25 | `SK-14` | low | `RASK_*` read directly via `os.environ` outside the settings modules |
| Q3-26 | `F-LIN-08` | med | Route topology decided at import time by settings-conditional module-level branches |
| Q3-27 | `X8` | med | The four `make_service_app` services expose liveness only; the chart points readiness at it |
| Q3-28 | `MED-014` | low | Both app entrypoints read settings and configure logging at import time |
| Q3-29 | `ING-18` | low | Query-parameter clamping done by hand instead of declared |
| Q3-30 | `ingest-flow-16` | low | Generator workflows annotated as returning their final value |
| Q3-31 | `ANN-07` | med | Half the annotator routes return bare `dict[str, Any]` — raw actor documents reach clients |
| Q3-32 | `VS-18` | med | Ten routes return bare `dict`/`list[dict]`, losing the response contract |
| Q3-33 | `SKG-09` | med | Every lakehouse control-plane record is an unvalidated `dict[str, Any]` |
| Q3-34 | `F-LIN-07` | med | Domain values cross models→repository as untyped dicts and positional tuples |
| Q3-35 | `CAT-CORE-08` | low | Service functions return `dict[str, Any]` that endpoints splat into models |
| Q3-36 | `MED-013` | low | Two handler seams typed `Any` with an ANN401 suppression |
| Q3-37 | `SKG-14` | med | The audited scope sits under a blanket 21-rule ruff exemption (5 lines still in `pyproject.toml`) |
| Q3-38 | `PS-15` | med | `ray_kit.submit` — deterministic ids and the reattach branch — has no test |
| Q3-39 | `MED-002` | low | `transform.py`'s process-wide `_write_lock` is still acquired BLOCKING |

## Q4. What the FIRST run against the live estate found (2026-09-05)

`make e2e-live` runs the e2e suites against the DEPLOYED k3s release, discovering every address and
credential from the cluster. Nothing had ever done this — `make test` excludes the marker and
`scripts/e2e_stack.sh` builds its own reduced kind cluster — so these rows are the cost of that
silence, found in 88 seconds on the first run.

**First result: 63 passed, 10 failed, 40 skipped, 4 errors.** Two failures were repaired in the same
commit and are struck; the rest are rows.

| # | Suite | What the live estate says | Verdict |
| --- | --- | --- | --- |
| ~~Q4-1~~ | `test_dummy_lane_e2e` declaration | 422 `body.entrypoint: Extra inputs are not permitted` | ~~FIXED — the suite still sent `entrypoint` after the `task` rename. Nothing caught it because nothing ran it: exactly what this target exists for~~ |
| ~~Q4-2~~ | `test_dummy_lane_e2e` command refusal | asserted the word "baked" | ~~FIXED — the door's refusal now NAMES THE REGISTRY (`no task is registered as '…'; … under the control root's _tasks/ prefix`), which is a better message than the one the test was written against~~ |
| Q4-3 | `test_dummy_lane_e2e` terminal event | `namespace:acme-silver -> table:acme-silver$dummy` link absent | The suite names it: `seed_estate.py` seeds `$features` (the HTR lane's output) and not `$dummy`. A seed gap, not a code defect |
| Q4-4 | `test_observability_e2e` (4 errors) | 400 — a top-level namespace must belong to a warehouse | The suite creates an unbound namespace, which `require_warehouse_scoped` refuses when `catalog.warehouses.enabled` is on. It is OFF in the kind stack and ON here, so the suite has only ever run against half the estate's shapes |
| Q4-5 | `test_multibase_e2e` (3) | 403 `can_create_table required` | alice holds no grant on the throwaway namespaces these mint. The kind stack seeds them; a live estate does not, and the suite cannot assume its own fixtures exist |
| Q4-6 | `test_warehouses_e2e::test_per_warehouse_physical_isolation` | `AssertionError: []` — no objects where isolation was expected | Unclassified. Needs driving by hand before it is called a defect or a fixture gap |
| Q4-7 | `test_maintenance_e2e::test_sweep_compacts_real_datasets_and_meters` | `KeyError: 'datasets'` | The sweep's response shape and the suite's expectation disagree. One of them is stale and it is not yet established which |
| Q4-8 | `test_outbox_e2e::test_reconcile_sweep_drains_a_staged_outbox_event` | expects a bare run id, the store holds `<id>@COMPLETE` | A key-format change the suite never saw |
| Q4-9 | `test_warehouses_e2e::test_create_warehouse_denied_for_non_admin` | bob CREATED the warehouse | NOT a governance hole, and this nearly went in as one. `team:eng` is bound to `project:acme` and `project.admin` is "… or member from team"; bob is a member, so he IS an admin here. The RUNNER was wrong to assume the identity — it now verifies `can_administer` is false before offering the token, and the leg SKIPS otherwise. An honest skip beats a red test alleging something untrue |

The 40 skips are suites whose target this estate does not run (the Ray-path pair, the two-tenant
isolation attack). They are skips rather than failures because the targets require their inputs and
say so, which is the behaviour `e2e-auth`'s comment argues for: *"a live drive with no live target is
a failed invocation, not a pass"*.

## Q5. What driving the FULL TENANT PATH live proved, and what it did not (2026-09-05)

Project `c6t115034` was minted for this: a fresh tenant on a runtime-minted warehouse bucket, so the
proof could not lean on anything seeded at bootstrap. Every step below is a pasted live result.

**PROVEN.** `POST /v1/projects` → `POST /v1/warehouses` (bucket `c6t115034-wh`, minted at runtime and
nameable by no chart value) → three warehouse-scoped namespaces → `POST /produce?project=c6t115034` → 202.
The cascade ran and the catalog governs ALL THREE TIERS on the tenant's own bucket:

    c6t115034-bronze$events    -> s3://c6t115034-wh/medallion/bronze
    c6t115034-silver$features  -> s3://c6t115034-wh/b19ee6fa_c6t115034-silver$features
    c6t115034-gold$catalog     -> s3://c6t115034-wh/8b477cb7_c6t115034-gold$catalog

**THE GATE HELD THE HOP, AND THE RUNG RELEASED IT** — better evidence than a straight-through run.
Silver's promotion was held (`reasons: ['first_promotion']`), so gold did not fire. Approving it
through `POST /api/promotions/{id}/decision` as a signed-in validator (`can_promote`, granted as
`validator` because a `can_*` relation is never directly assignable) released it, and gold landed.
Lineage is queryable in AGE for the tenant — 10 runs, including `aggregate_gold` carrying
`consumed=(None,3]`, the range field exposed earlier today.

**NOT PROVEN, and these are the rows.**

| # | What | Evidence |
| --- | --- | --- |
| ~~Q5-1~~ | The maintenance credential reached ONE bucket while the sweep discovered 91 | ~~FIXED. The live `rask-maintenance` policy granted `lance-catalog` alone — the same defect fixed for the Ray user in `5c11002c`, which the chart already corrected and which had never been applied here. Applying the chart's own rendered policy took the sweep from `planned:21, skipped:4` to `planned:250, skipped:0`. **229 datasets across 90 tenant warehouses had never been maintained**, silently~~ |
| Q5-2 | Maintenance cannot compact ANY tenant's tables, and the RUNG may be the defect rather than the grant | Refused live: `credentials?tier=write` -> **403**, `compaction_plan` -> **403**. Established 2026-09-05 that this is not about runtime-minted tenants at all — `service-maintenance` is absent from `LANCE_FGA_CASCADE_WRITERS`, so it holds nothing on ANY warehouse. **The question is which rung, and the model has no good answer.** Vending needs `can_write_data` (= `writer`); the compaction door is gated on `can_drop` (= `owner`), so admitting maintenance the ordinary way hands it `can_deregister` and `manage_grants` on every tenant — the same over-grant Q2-6 flags for the movers. A COMPACTION DROPS NOTHING, and the model has no `can_compact`/`can_maintain` relation to gate it on (grepped: none exists). So: (a) add a maintenance rung to the model and gate the compaction doors on it — most work, least privilege; (b) grant `writer` and re-gate `compaction_plan` off `can_drop`; (c) grant `owner` and accept the breadth. **OWNER RULING** — (a) is the honest design and it changes the authorization model |
| Q5-3 | The ambient-credential log line names the wrong key | `write credential AMBIENT … this rewrite is signed by the root key` — it is signed by `rask-maintenance`, which is not the root key on this estate. False prose in an operational log, which is where it is hardest to catch |
| Q5-4 | `bronze-media` is measured for every tenant that has no media lane | `GET /v1/table/c6t115034-bronze-media$objects/tags/list` → 403 on the lag tick. The lane map is estate-wide, so a tenant using only the tabular lanes still has its media edge probed. Harmless (it counts UNMEASURABLE) but it is 1/3 of the tick's work for nothing |

So C6 is **two-thirds proven**: the data path, the governance gate and lineage are demonstrated end to
end on a runtime-minted tenant; maintenance reaches the bytes and is refused at the catalog door.
Q5-2 is the remaining work, and it is the same question as Q2-5/Q2-6 — what a cascade or maintenance
subject is granted on a tenant that did not exist at bootstrap.

## Q6. Carried from the 2026-09-04 review after the fixes landed (2026-09-05)

| # | Finding | Sev | What remains |
| --- | --- | --- | --- |
| Q6-1 | The compute credential can enumerate every bucket | med | Measured: `mc ls ray/` returns 104 buckets with the deployed key. Withholding `s3:ListAllMyBuckets` does not withhold the list — RustFS falls back to per-bucket `ListBucket`, which `arn:aws:s3:::*` grants. The widening is the deliberate trade (the allow-list it replaced broke every tenant's cascade); the narrowing that closes it is PER-TABLE VENDED credentials on the Ray lane, a service change. Same root as Q5-2 |
| ~~Q6-2~~ | `RayJobExecutor` reports a CR whose cluster never came up as PENDING forever | med | **DISSOLVED HERE, CARRIED TO THE COMPUTE GOAL.** Real and unfixed: `status()` maps only `status.jobStatus`, while KubeRay records a cluster that never came up, an `activeDeadlineSeconds` expiry or a Kueue eviction in `jobDeploymentStatus` and leaves `jobStatus` empty — which `_JOB_STATUS` maps to PENDING, so such a run is never reported FAILED and `DURABLE_RECORD` forbids resubmitting it. Not fixed under this goal because the goal's own scope line says COMPUTE IS UNTOUCHED UNTIL IT CLOSES; fixing it here would be the scope creep the line exists to prevent. It belongs to the compute goal, stated so it is picked up rather than lost |
| ~~Q6-3~~ | `RayJobExecutor` treats any 409 as REATTACHED without reading the CR, and the CR name omits `code_version` | med | **DISSOLVED HERE, CARRIED TO THE COMPUTE GOAL.** Real and unfixed: a 409 may mean a DIFFERENT job holds that name, and the name omitting `code_version` makes that reachable — a same-token re-run after a deploy reattaches to the previous build's job. Same scope reason as Q6-2 |
| Q6-4 | A queued index build emits no lineage anywhere | med | The door skips the emit and the worker never makes one, so an index that took an hour is invisible to the run board |
| Q6-5 | `plan_compaction` answers 400 where every sibling door answers 404 | med | "registered but never written" is mapped to `InvalidInputError` off a bare `ValueError`; siblings raise `TableNotFoundError`. A client dispatching on the code sees a different class for the same condition |
| Q6-6 | The halt-counter alert gate is a substring search over the whole rules dump | med | It matches annotation prose, so a rule could be deleted and the gate stay green on its own description |
| Q6-7 | The promtool-expectation gate silently skips unknown alertnames and missing annotation keys | med | A typo in an alertname makes the expectation vacuous rather than failing |
| Q6-8 | The RayJob Role grants `list` and `watch` the executor never issues | low | Narrow to `create,get,delete` |
