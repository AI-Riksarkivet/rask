# open_lakehouse_diff_left — the governed Lance lakehouse: verdict, decisions, backlog, zero-trust diff, open questions

> **Naming note (2026-09-02).** This document was written against `feec956` (2026-08-25), before the
> X10 hard rename. Every `LANCE_*`/`LINEAGE_*`/`MEDALLION_*`/`MAINTENANCE_*` auth variable it quoted
> is now `RASK_*`; the names have been rewritten in place so the repo-wide retired-name gate stays
> meaningful, and because a retired name in prose sends an operator to a variable that binds nothing.
> The line references are unchanged.


**Counted 2026-09-08, from the rows below rather than asserted: 230 tracked, 148 open, 82 struck.**
That splits into 67 lettered rows (53 open) and 98 rows in the Q sections — § Q2 carried from
`open_estate-verification.md`, § Q3 from `open_python-audit.md`, § Q4 recorded from the first e2e run
against the deployed estate. Re-derive the counts when
you change them; the previous header claimed a freshness date two days older than rows struck beneath
it, and a header nobody re-counts is how a register stops being evidence.

THIS IS NOW THE ESTATE'S ONLY LAKEHOUSE BACKLOG. Its two siblings were drained and deleted
(`73b171d7`, `058da189`); what they were and what they found is in `docs/DECISIONS.md` under
"The Python estate audit" and "A repeating condition is a LEVEL, not an event".

SCOPE — NARROWED BY OWNER RULING 2026-09-07: **the lakehouse FIRST, then compute, and nothing else.** Verbatim: *"prio lakehouse and ignore other zones that are not the lakehouse or compute. I.e search, flows and model training and annotator should be ignored and focus only on lakehouse compute services, but priotize lakehouse."* So `services/search`, `services/flows`, `services/annotator` and the TRAIN lane are out, and a row about them is struck with that reason rather than worked. A row being PRESENT is not evidence it is in scope — this register absorbed two drained ledgers that swept the whole estate. Beyond that, this file tracks the LAKEHOUSE — the Lance
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

### A5 · Error bodies without `code` — **DONE AND VERIFIED LIVE 2026-09-07**, one upstream-blocked residual (`1be78b1c`, `e2a03129`, `82bdaa49`, `256c9cc1`; earlier `f1ee42d3`, `0699bac3`). Codes 8/9/11/13/20/22/23 reachable where they were not, and 14 answers a lost race. The residual — a malformed BRANCH name — is a Lance panic, not a mapping gap; both are below.
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
**TAG/BRANCH: DONE 2026-09-07, driven against the deployed catalog before and after.** Only
`get_tag_version` translated anything, which is why the gap read as absent rather than systematic —
the one tag op anyone had driven by hand was the one already correct. Every other door let a bare
pylance `ValueError`/`OSError` reach `install_problem_handlers`, which maps only the typed
`lance_namespace` hierarchy, so all of them landed on `Internal 18`. Measured on `rask-catalog:2333`:

    door                              before   after
    tags/delete   tag missing         500/18   404/8
    tags/update   tag missing         500/18   404/8
    tags/create   tag exists          500/18   409/9
    tags/create   version missing     500/18   404/11
    tags/update   version missing     500/18   404/11
    branches/delete branch missing    500/18   404/22
    branches/create branch exists     500/18   409/23
    branches/create bad from_version  500/18   404/11
    branches/create missing source    500/18   404/22
    tags/version  tag missing         404/8    404/8   (regression guard)

The status map was never the defect: all 24 codes were already in `_STATUS` and every typed class
already carries the right `.code`. What was missing is translation where the raw exception is raised.
`_classify_ref_error` follows the `_classify_commit_error` precedent beside it and captures the
failure's own NOUN, because a tag op scoped to a branch must report the BRANCH that is missing.

**A COLLISION AND A MISSING SOURCE ARE ESTABLISHED BY READING, NOT BY MATCHING A MESSAGE**, and the
first attempt got the second one wrong in a way only adversarial re-verification caught: pylance
renders a missing source BRANCH and a missing source VERSION with the same object-store text, so
matching it answered 11 for a branch that did not exist — the caller sent hunting a version when the
branch was absent. Pinned by `services/catalog/tests/test_tag_and_branch_failures_carry_their_spec_code.py`.

**COLUMN/DATA OPS: 14 AND 20 DONE 2026-09-07, both driven on the deployed estate.**

*Code 20 was an ASYMMETRY, not a missing feature.* `insert_into_table` and `merge_insert_into_table`
split on `branch`: without one they delegate to the native backend, which maps a schema mismatch to
`TableSchemaValidationError` (20 -> 400); with one they run pylance in-process, where the identical
mismatch escaped as a bare `OSError` and was reported `Internal 18`. Measured across three payload
shapes — wrong Arrow type, an extra column, a wholly unrelated schema — main answered 20 for all three
and the branch answered 500 for all three. A second sat beside it: `dataset.merge_insert(on)` is where
Lance rejects a key column that does not exist, and it was constructed OUTSIDE `_user_sql`'s guard, so
the one door whose entire job is matching on that column answered 18 for naming it wrongly while the
branchless path answered 13. Live after the fix, on `acme-bronze$agnostic`: main 20 / branch 20, and
main 13 / branch 13. The test asserts PARITY rather than a literal code — the branchless door already
decided, so pinning the two together states the contract and cannot drift from the native backend.
**This estate has shipped the branch-path-is-worse bug before** (`test_branch_scoped_mutations_hit_the_branch.py`:
update and delete silently rewriting MAIN). Same door family, same asymmetry, caught late both times
because nothing compared the two paths.

*Code 14 is the one error here that is nobody's mistake.* Lance calls it RETRYABLE in its own message.
Reported as 18, both of the things a caller should do become impossible: a client cannot know to
re-read and re-commit, and an operator is paged for contention that resolves itself. The vocabulary
already existed — `_COMMIT_CONFLICT_MARKERS`, which `_classify_commit_error` already mints 14 from for
the commit door — and the column ops simply never asked, so the fix is one branch in the one guard all
four share. It sits AFTER the missing-column test and that ordering is pinned by its own test: the
marker tuple carries the bare word `concurrent`, so testing it first would answer 14 to a caller who
merely named a column `concurrent`, turning a typo into "retry", advice that can never succeed.
**Proven live by racing the deployed catalog** — six concurrent `add_columns` on one table, three won
and three lost with `409 / code 14`; before the fix all three were 500/18. (The probe columns were
dropped afterwards; the table is back to its seven.)

**RESIDUAL, UPSTREAM-BLOCKED: a malformed BRANCH name answers 18 on the deployed estate.** pylance's
ref-name validator produces `Ref is invalid: ...` — mapped to `InvalidInput` 13 (400) and proven live
for TAGS on both backends. But branch creation on the S3-backed estate reaches Lance's clone path
BEFORE that validator and dies `OSError("Encountered internal error ... Clone operation should not
enter build_manifest.")`, the same text a collision produces, which the door already re-reads to
disambiguate. Measured both ways 2026-09-07: identical call, `dir` backend -> `Ref is invalid`,
deployed S3 -> the internal-error panic. Duplicating Lance's validator here is refused deliberately —
it accepts `über`, `HEAD`, `-x` and `x.LOCK`, disagreeing with its own message text, so a hand-rolled
copy would reject names Lance accepts. The fix belongs upstream (an invalid name must not panic).

**What.** 422, generic 500, FastAPI 404/405, maintenance 503, 413, 429 and draining 503 all collapse to
`InternalError 18` in the client; tag/branch failures are unmapped 500s (codes 8/9/11/22/23 unreachable);
column/data ops never mint 14/20; UNSUPPORTED answers 501 where the spec and Lance's reference server
answer 406. **Where.** `service_kit/lakehouse/ns_errors.py:25,135-161`,
`api/maintenance_mode.py:31-43`, `dataplane.py:1345-1418`. **Closes it.** One coded problem+json builder
for every status (the four hand-built ones in `body_limit.py`, `load_shed.py`, `draining.py` fold in), and
`UNSUPPORTED → 406` (Q3, decided).

### ~~A6 · Identity: `x-api-key` never read; bearer verified only with OIDC on~~ — **BOTH CLAUSES ANSWERED 2026-09-07**
**The second clause closed with §F2-2**: `assert_authentication_configured` refuses to boot a governed
service whose auth is off unacknowledged, so "anonymous by default" is not silently reachable.

**The first clause is REFUTED — not a conformance gap but a deliberate position, and the spec is what
says so.** It is literally true that `x-api-key` is read by nothing (zero files under
`services/catalog/src` or `packages/service-kit/src`, re-measured today). It is not a defect, and
reading the spec's own `security` block rather than only its `Identity` schema is what settles it:

    security:                     <- a DISJUNCTION: any ONE scheme satisfies the contract
      - OAuth2: []
      - BearerAuth: []
      - ApiKeyAuth: []

The `Identity` schema (`spec.yaml:2440`) defines how a CLIENT carries a credential it already has —
`api_key` -> `x-api-key`, `auth_token` -> `Authorization: Bearer`. It does not oblige a server to
accept all three. Measured against the deployed catalog: our scheme is
`{"type": "http", "scheme": "bearer"}`, **structurally identical to the spec's `BearerAuth`** (the
local name `HTTPBearer` differs, and OpenAPI scheme names are local identifiers a generated client
never dispatches on); **155 of 160 operations declare it**, and the five that do not are `/livez`,
`/readyz`, `/dapr/subscribe` and `/control-events` — correctly unauthenticated. The door enforces it:
`GET /v1/table` with no credential answers **401**, and with `x-api-key` alone also **401**.

**Implementing the third scheme would work AGAINST §F2-3.** An API key is a long-lived shared static
credential needing its own issuance, storage, rotation and revocation — a second credential plane
beside the OIDC one, against the standing rule that secrets come from the Dapr secret store only, and
the precise shape §F2-3 spent 2026-09-07 removing (one shared service bearer -> a credential per
subject). Adding it to satisfy a header name would trade a real control for a spelling.
**Struck: conformant as built, and the alternative is a regression.**

### A7 · Governance inside spec handlers
**Status 2026-09-02.** The *"update/delete ignoring `branch`"* clause is closed and wider than
written: `update`, `delete`, `insert`, `merge_insert`, the merge's index build, `schema_metadata/update`
and `count_rows` honour `branch`; `query`, `explain_plan`, `analyze_plan`, `create_index`,
`create_scalar_index`, `stats`, `index/list`, `index/{n}/stats` refuse it (see C2). The rest of A7
stands.
**MEASURED THROUGH THE STOCK CLIENT 2026-09-07 (§A11's suite), and most of this row is already
true.** The close condition is "each refusal re-expressed with the spec's own code", and four of the
governance refusals were driven against the deployed catalog with pylance's own `RestNamespace` — the
arbiter that matters, since the question is whether a spec client can UNDERSTAND a rask refusal.
Every one arrived as a typed spec error rather than an untyped 4xx or an `Internal 18`:

    create a table at the ROOT              InvalidInputError        13
    create a TOP-LEVEL namespace            InvalidInputError        13
    drop a PROTECTED table                  NamespaceNotEmptyError    3
    drop a NON-EMPTY namespace              NamespaceNotEmptyError    3

**The protection code is the one worth revisiting, and its recorded reason is FALSIFIED.**
`fga_deps.py::require_not_protected` chooses `NamespaceNotEmptyError` and explains it as "the spec's
own 'this container will not be deleted right now' error, reused rather than minting a status the
client SDKs do not map". Measured: `InvalidTableStateError` — "Table is in an invalid state for the
operation" — exists in the stock client with code 19 and maps to the SAME HTTP 409 in
`ns_errors._STATUS`. So the alternative is mapped by both ends, and the stated reason for not using it
does not hold.

The consequence is bounded but real, and it is the §A5 shape: the DETAIL is perfect ("is protected
against deletion. Pass force=true to override"), so a human is never misled — but a generated client
dispatches on the CODE, and code 3 tells it the container has contents. For a protected TABLE that can
never be made true, so a client that empties-and-retries loops forever.

**NOT CHANGED HERE, and the reason is a genuine tie rather than reluctance.** Protection covers four
object kinds (table, namespace, warehouse, project) through one shared guard. Code 19 is spelled
`InvalidTableState`, so moving all four onto it puts "table" on a project refusal; splitting it (19 for
tables, 3 for containers) mints two codes for one condition, which is the inconsistency this estate
refuses elsewhere. Both readings are defensible, the status quo is a recorded decision, and what the
row was missing is the measurement that its stated justification is wrong — which it now has.

**What.** Warehouse-scoped namespace refusal, no root tables, trash soft-delete, protection 409/code 3,
lineage keys injected into schema metadata, implicit BTREE on merge_insert, insert pre-coercion,
maintenance 503 on POST reads, update/delete ignoring `branch`. **Closes it.** R2; each refusal
re-expressed with the spec's own code; `branch` honoured (plumbing at `dataplane.py:1085`).

### ~~A8 · Stub status codes — **DONE 2026-09-02** (`9e3844b5`)~~
**DONE 2026-09-02.** 201 / 202 / 202 declared on the three decorators, asserted through the OpenAPI
(all three answer 501 today, so no live call can exercise the success status).
**Where.** `views.py:24,58`, `columns.py:146` lack 201/202. One-line fix each.

### A9 · 0.12.0: merge_insert `on` is an array — **STILL BLOCKED, one layer deeper than the row knew**
**REVERTED WITH A10 2026-09-07.** It was implemented and worked at the door — but the door is not
where it fails. pylance 10.0.0's NATIVE backend types `on` as a string, so a composite key raises a
`TypeError` beneath the catalog no matter what the door accepts, and only the BRANCH path
(`dataset.merge_insert`, which takes an `Iterable[str]`) would have worked. Half-working, split by
whether a branch was named, is the one outcome worse than a clean refusal. **A9 lands with the JOINT
lance-namespace + pylance bump, not with the lance-namespace bump alone.** What the implementation
established, and is worth keeping when it returns: the door takes `on: Annotated[list[str] | None, Query()]` — a composite merge key
as a repeated query parameter — and the value passes through untouched: 0.12.0 types the request
model's `on` as `Optional[List[StrictStr]]` and pylance's `merge_insert` already accepts
`Optional[Union[str, Iterable[str]]]`. **The index build was generalised with it**, which the row did
not ask for and needed: a scalar index covers ONE column, so the accelerator for a two-column key is
two BTREEs. Indexing only the first would have left the merge full-scanning on the others while the
log claimed the key was indexed — coverage is now decided per column, so a key half-covered by
earlier indexes still gets its missing half. 330 catalog tests pass.
**The blocked-on-A10 record, kept because it is why widening the door alone was reversed:** Widening only the door was tried and reversed: the installed
0.11.0 model types `on` as `str` with `MinLen(1)`, so a list fails validation INSIDE the request model
and a 0.12.0 client gets a pydantic error one layer deeper — which reads as a rask bug rather than a
version skew. The current single-key contract is now pinned so the change lands WITH the bump.
**Where.** `data.py` merge handler declares `on: str | None`; the wire form is a repeated query
parameter. **Closes it.** `on: list[str]`; pass through to pylance.

### A10 · 0.12.0: bump `lance-namespace` and re-vendor `lance_docs` — **BIGGER THAN A PIN EDIT**
**ATTEMPTED AND REVERTED 2026-09-07, and the reason changes what this row is.** The bump was made
(nine pins → `>=0.12.0`, verified against PyPI: 0.12.0 released 2026-09-01) and the catalog's 320
tests passed — then the integration suite failed, and the cause is not in rask:

    lance-namespace 0.12.0  MergeInsertIntoTableRequest.on : List[str]  — REJECTS a string
    pylance 10.0.0          native merge_insert_into_table : str        — TypeError on a list

Measured both directions, so this is a MUTUAL incompatibility rather than a defect in rask:
constructing the request with a string raises `ValidationError`, and passing the list the model
demands raises `TypeError: unexpected type: 'list' object is not an instance of 'str'` out of
`lance/namespace.py:580`. **So with that pair, `merge_insert` through the native path is broken for
ANY value of `on`** — not merely for the composite key A9 wanted. The estate was never exposed: the
deployed image carries the pre-bump lock.

**The pins now carry a CEILING (`>=0.11.0,<0.12`) with that measurement written beside them**, because
an unpinned floor let the resolver take 0.12.0 straight back. Reverting the floor alone did not hold —
which is itself the lesson: a "revert" that leaves the range open reverts nothing.

**What this row now needs is a JOINT bump — lance-namespace 0.12.0 WITH pylance 11.0.0** (released;
the estate is on 10.0.0). That is a major-version bump of the core columnar format library across
every service that reads a dataset, not a pin edit, and it deserves its own change with its own
verification. A9 rides on it.

**THE RE-VENDOR HALF, SIZED 2026-09-07 — AND IT IS A DOC LAG, NOT A CAPABILITY LAG.** Diffing
`lance_docs/ns_catalog/spec.yaml` against upstream `main`: the file is 79 lines short (101 changed),
but **the error contract is byte-identical** — 24 codes, all 54 operations — which is what makes §A5's
coded-error work safe to have landed against the vendored copy.

**The first reading of that diff was WRONG and is corrected here rather than left standing.** It listed
the index tuning params (`num_partitions` / `num_sub_vectors` / `num_bits` / `sample_rate`, and also
`ef_construction` / `m` / `max_iterations` / `target_partition_size`), the response's
`num_inserted_rows` + `version`, and `backfill_column`'s computed columns as things upstream had and
rask lacked. **Every one of them is PRESENT in the vendored 0.11.1 client** — checked against
`model_fields` rather than against the YAML. Two documents were compared where the question was what
the CODE carries: the same "verify where the value LANDS" failure this file records six other members
of. `dataplane.py`'s own comment already said `computed` arrived with 0.11.0.

So the ONE genuine gap is `on` as an array — A9's subject, and blocked exactly as this row's head
already measured. What the diff actually surfaced is smaller and was real: **`docs/catalog-openapi.json`
was stale**, missing the index params the deployed app already serves, and `docs/lineage-openapi.json`
was missing `/runs/{run_id}`. Both regenerated with `make openapi`.

Note Q3 above cited "lance-namespace v0.12.0 `spec.yaml`" for its 406 decision while the vendored copy
is pre-0.12.0. The decision stands — the `Unsupported` status is unchanged between them — but the
citation names a file this repo does not hold.

**THE RE-VENDOR HALF IS SEPARATE AND STILL OPEN, and measuring it found a second defect: `lance_docs/`
records no source version at all.** Six files, hand-vendored, no manifest, no pinned commit, no
automation — so nothing says which spec version they describe, and a reader citing them (as the
standing CONSTRAINTS require) cannot tell whether they are current. Re-vendoring should land a
provenance line with them, or the next reader is in exactly this position.

### A12 · `insert_into_table` answers null counts on MAIN and real ones on a BRANCH
**FOUND 2026-09-07 by driving the stock client** (§A11's write leg), which is the only reason it was
seen: our own transport never reads those fields. The third main/branch asymmetry in this file and the
only one where the BRANCH path is the better half —

    branch  dataplane.insert_into_table   version=<n>  num_inserted_rows=<n>   (computed, hot path pays for it)
    main    native.call(...)              version=null num_inserted_rows=null

**Not currently a spec violation, and that is why it is a row rather than a fix.** The VENDORED
`spec.yaml` declares only `context` and `transaction_id` on `InsertIntoTableResponse` — so null is
conformant today. Upstream declares `num_inserted_rows` and `version`, and the vendored 0.11.1 MODEL
already carries both fields (checked against `model_fields`), so the response model is ahead of the
schema this repo holds. It becomes a real gap the moment A10's bump lands.

**The trade-off is why it is not fixed here.** The native path returns no dataset handle, so filling
the fields costs an extra `open_dataset` plus two `count_rows` on EVERY main-path insert — the hot
write path, where nearly every insert has no branch. The branch path already pays exactly that and is
the reason the asymmetry exists at all. Fixing it blind on a hot path to populate a field the current
contract does not require is the shortcut this estate refuses; the measurement it needs is the cost of
that open against a large table. **Land it WITH A10**, where the field stops being optional.

### A11 · The conformance test that defines "verbatim" — **FIRST CLIENT PROVEN LIVE 2026-09-07**, two to go
**CONFIRMED AND SHARPENED 2026-09-07, and it is the estate's own recurring trap.** Grepping for the
stock clients finds them: `RestNamespace` and `namespace_client_impl` appear in
`tests/integration/test_spec_response_shapes.py` and `test_bodyless_handlers_read_the_spec_body.py`.
Both drive a **`TestClient` against a `MagicMock` namespace** (`fake_ns`), and `tests/e2e-py` — the
only suite that touches a running catalog — contains **zero** occurrences of either. So the NAME of
the conformance the row asks for is present and the conformance is not, which is exactly the pattern
recorded in `docs/DECISIONS.md`: verify where a control's value LANDS, not where its name appears.
**What.** `tests/integration/test_spec_conformance.py` pins (method, path) only; no test constructs
`lance.namespace.RestNamespace`, lancedb `namespace_client_impl="rest"` or lance-ray namespace mode
against a running catalog (the urllib3 client is exercised through rask's own transport wrapper only).
**THE STOCK PYLANCE CLIENT NOW DRIVES THE DEPLOYED CATALOG** —
`tests/e2e-py/test_the_stock_lance_client_drives_the_catalog.py`, `make e2e-spec-conformance`. It
constructs `lance_namespace.connect("rest", …)`, which resolves to pylance's Rust-backed
`lance.namespace.RestNamespace` — the client an outside Lance user gets — against the running catalog
with a real Dex token, and **imports no rask module anywhere**: if the estate is idiomatic, a stock
client needs none. Measured 2026-09-07: **10 of 10 read ops answer** (list/describe/exists on
namespaces and tables, `count_table_rows`, `list_table_tags`, `list_table_versions`,
`list_table_indices`).

**The half a curl cannot check is the one worth having.** A wire body can be perfect problem+json
while the client still fails to rebuild the typed exception a caller catches — and `code` is what a
generated client dispatches on. Driven: a missing tag comes back as `TableTagNotFoundError` with
`.code == 8`, so §A5's fix survives the round trip into the stock client's own hierarchy rather than
merely into a JSON field. The suite carries its own control — an uncredentialed client must answer
`UnauthenticatedError` code 16 — because a conformance suite that would pass against an open door is
measuring the door's absence.

**A CLIENT-SIDE QUIRK, PINNED HERE BECAUSE IT LOOKS EXACTLY LIKE A BROKEN SERVER.** The connect
property that carries a bearer is `headers.Authorization`. The spec's OWN spelling for this —
`auth_token`, `spec.yaml:2452`, "passed via the `Authorization` header with the Bearer scheme" — is
accepted by `connect()` and then silently ignored, and every call answers `UnauthenticatedError`. So
are `bearer_token`, `api_key` and `additional_headers`. Costs an hour to rediscover.

**THE WRITE ROUND TRIP IS PROVEN TOO** (2026-09-07): create -> insert -> tag -> read the tag -> untag
-> drop, every step through the stock client against the governed catalog, on a fresh uuid-suffixed
table dropped in a `finally`. The read surface proves the catalog can be READ idiomatically; this
proves it can be USED. **The row COUNT is the insert's assertion, not the response's
`num_inserted_rows`** — the native path leaves that field null, so trusting it would let the leg pass
while nothing was written (3 -> 6 measured instead).

**THE SECOND CLIENT IS MEASURED, AND IT CANNOT REACH A NESTED NAMESPACE — upstream, not ours.**
lancedb 0.34.0 connects fine (`namespace_client_impl="rest"` + `namespace_client_properties`, the
same `headers.Authorization` property) and `list_tables()` at the root correctly returns nothing,
because this estate's root holds NAMESPACES rather than tables — the stock `lance_namespace` client
agrees, listing `['bronze', 'transcripts_v2']` there and the tables one level down. But
`open_table("acme-bronze$agnostic")` is refused **400 InvalidInput**:

    request body id ['acme-bronze$agnostic'] does not match the path identifier ['acme-bronze', 'agnostic']

**Its request is internally inconsistent and our refusal is the spec's own rule.** It puts the whole
name in the PATH, where the spec says a delimited identifier splits into segments, and the SAME
unsplit string in the BODY as one segment — so the request asserts two different identifiers, which
`operations/index.md` says is a 400 (`core/identifiers.py::reconcile_body_id`). Measured: lancedb's
`open_table` accepts **only a `str`** (`['a','b']` and `('a','b')` both raise
`TypeError: argument 'name': 'list' object is not an instance of 'str'`), so it has no way to express
a multi-segment identifier, and no connection property scopes it to a parent — `parent=`, `namespace=`
and `root=` were all driven and none changes what it addresses.

So lancedb works against a FLAT catalog and cannot address a nested one. **Not papered over here**:
relaxing `reconcile_body_id` to accept the unsplit spelling would be exactly the outer-layer
workaround for a library bug that `CLAUDE.md` forbids, and it would weaken the one check that stops a
request naming two objects at once. The fix is upstream, and it is worth filing.

**THE THIRD CLIENT IS DEFERRED TO THE COMPUTE PASS, deliberately.** lance-ray's namespace mode is
real and reachable — `read_lance(table_id=[...], namespace_impl="rest", namespace_properties=...)`
takes a proper SEGMENT LIST, so it does not hit lancedb's single-string limit — but driving it needs a
Ray runtime, which is the compute plane rather than the lakehouse. Under the owner's scope ruling
(lakehouse FIRST, then compute) it waits for the compute pass. Two things measured on the way, so the
next attempt does not rediscover them:

* **`ray.init()` on this host attaches to a 41-DAY-OLD FOREIGN CLUSTER and then refuses.**
  `/tmp/ray/ray_current_cluster` (dated 2026-08-05) points at a Ray 2.56.1 / Python 3.14.3 instance
  running from `/opt/venv` under another user, with a Serve controller. This repo declares
  `ray[default]>=2.58` and has 2.58.0, so every connect dies `Version mismatch` — a host defect, not a
  code one, and the same shape as the stale dev gateway that squats `:8888`. `ray.init(address="local")`
  with a private `_temp_dir` bypasses it.
* **lance-ray reads the DATA PATH DIRECTLY, so the catalog's vended credentials are a precondition**,
  not an afterthought: with none it fails `CredentialsNotLoaded` before any spec op is exercised.
  `POST /v1/table/{id}/credentials` answers `{credentials, location, mode, read_version}` and is the
  governed way in — the same client-direct seam the estate already proves elsewhere.

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

### C1 · Per-base credential vending — **A FALSY-ZERO BUG IN ITS GUARD, FIXED 2026-09-07**
**`has_external_bases` could not detect the shape it exists for.** It decides whether a table may be
DIRECT-vended, and its own docstring gives the stake: *"the STS session policy is scoped to the primary
root bucket only, so a data-base fragment would be denied at the object store."* It asked
`any(getattr(df, "base_id", None) …)` — a TRUTHY test. Measured on pylance 10.0.0:

    plain dataset, its own files      base_id = None    falsy
    SHALLOW CLONE, files in the base  base_id = 0       falsy   <- the case the check exists for
    branch, files in the parent root  base_id = 0       falsy

`base_id` INDEXES `base_paths`, so the first registered base is **0**, while a file under the dataset's
own root carries `None`. The truthy test calls those identical and answered False for the canonical
multi-base shape, so such a table would be direct-vended with a session policy that cannot reach where
its bytes are. **Nothing goes red**: the vend SUCCEEDS and the denial lands later, at the object store,
on whoever used the credential — the estate's recurring "the control's name is present, its enforcement
is not", with an off-by-falsy twist.

Fixed to `is not None`, pinned by `services/catalog/tests/test_base_id_zero_is_a_real_base.py` — which
drives real datasets rather than a stub, because the subject is what pylance puts in `base_id`, and
which pins the other direction too: a plain dataset must stay direct-vendable, since reporting every
ordinary table as multi-base would be worse than the bug.

**LATENT, NOT LIVE, and stated as such.** Both call sites are gated on
`settings.multibase_data_base_list`, and the deployed catalog carries
`LANCE_MULTIBASE_DATA_BASES=""` — so the guard is not reached today. It would have failed on the first
estate that enabled the feature, which is precisely when it was needed.

**RE-VERIFIED 2026-09-08, and TWO OF THE THREE CLAUSES ARE ALREADY DONE** — the row was written
against `feec956` and the estate moved under it:

  * **the `session_token` seam: DONE.** All four sites it named now carry the token —
    `objectfs.lance_storage_options` (`aws_session_token`), `objectfs.s3_filesystem`,
    `records._s3_client`, and `storage.client` (which additionally REFUSES a lone token, because
    botocore drops it and silently signs with the env chain instead). "No package seam can carry a
    `session_token`" is no longer true and must not be read as current.
  * **expiry in the vended options: DONE.** `VendedCredentials.expires_at_millis`, populated from the
    STS `Expiration` by `_expiry_millis` on both vend paths.
  * **per-base vending: STILL OPEN**, and § H12 is the measured reason it matters — 69 datasets a tick
    refused compaction because the vended policy cannot reach a base the manifest declares.

**The spec settles what a per-base grant must look like** (`lance_docs/file_format.md` § Base Path
System): a base is `{id, name?, is_dataset_root, path}`, and resolution differs by that flag — for
`is_dataset_root=true` the files sit under the base's `data/`, `_deletions/` and `_indices/`; for false
"the base path points directly to the file directory, and the file path is appended directly without
subdirectory prefixes". A grant that ignores the flag would be wrong in one direction or the other.

**And the data is already in hand at vend time:** `credentials.py` already performs a root-cred manifest
read (`_current_version`) on the very manifest that carries `base_paths`, so reading them there costs
nothing new.

**One hazard to carry into the implementation:** `build_session_policy` refuses `*`/`?` in the prefix
because they are IAM metacharacters with no escape. A base path comes off a MANIFEST rather than the
create doors' validated identifier, so it must get the same guard or it is a wider hole than the one
that guard closes.

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
**THE SILENT-DOORS CLAUSE IS CONFIRMED AT ITS ROOT 2026-09-07, and the root is not a forgotten emit.**
Driven against the deployed catalog: `tags/create` and `branches/create` both answered **200** and the
control-event ring stayed at 10 with **zero** events naming the probe. Then the cause, which is one
level below the doors — **`ControlAction` is a `Literal` of 38 values and not one of them is a tag or
branch action.** There is nothing for those doors to emit. The contrast is what makes it a gap rather
than a policy: `table_protected` / `table_unprotected`, `table_published`, `policy_set`,
`namespace_dropped` are all there, so governance-relevant state changes on a table generally DO emit,
and these two are the exception.

**Why it is not fixed here.** Adding an action is a WIRE CONTRACT change in three files
(`control_events.py` -> `docs/catalog-openapi.json` -> the generated TS client, `make openapi` +
`gen:types`), which is mechanical — but the design question in front of it is not: a notification needs
a TARGET, and `rask-notifications` is explicit that coverage is decided at the producer and that an
event naming nobody is undeliverable rather than under-delivered. So "who is told when the `published`
tag moves?" has to be answered before the action exists, and that answer is an owner's.

**A SECOND MEASUREMENT, incidental but worth keeping:** the probe first asked for `version: 1` and was
refused **404 / code 11** — "no such version for tag 'c2probe': version main:1 does not exist" — because
the sweep had reclaimed it (`version/list` now answers `[13, 17, 18, 19, 20]`). That is §A5's mapping
working in a scenario nobody constructed: before it, a tag pinned to a reclaimed version answered a 500.

**What.** The model has `can_create_branch: owner` on `table` and nothing else; branch writes fall through
to the table's `can_write_data`; update/delete write main; vending, protection, trash and lineage are
branch-blind; the catalog's branch and tag doors **emit no lineage at all**, so no notification ever
fires for a branch or tag. **Where.** `model.fga:357,349`, `fga_deps.py:108`, `endpoints/branches.py`,
`tags.py`. **Closes it.** `type branch { parent:[table]; reader/writer; can_write_data }` + `.fga.yaml`
cases; branch prefix in vending; lineage facets `parent_branch`/`parent_version`; per-branch protection
and trash records.

### C3 · Cross-dataset pins for clones and branches — **THE DATA-LOSS CLAUSE IS REFUTED 2026-09-07**
**"The catalog's on-demand `/maintenance/run` and `/compact` have no such guard and destroy a live
shallow clone" IS NO LONGER TRUE, and it is the clause that made this row urgent.** Both doors compute
the pre-pass and pass it: `endpoints/maintenance.py::run_maintenance` does
`protected = await _base_refs(ds, so)` -> `maintenance.run_gc(..., protected=protected)`, and
`compact_maintenance` does the same before `require_compactable`. The service functions take
`protected: BaseRefs | None = None`, so the default WOULD be unguarded — which is exactly why the
CALL SITES are the thing to read, not the signatures.

**And the guard does real work, measured on the deployed estate rather than inferred:** in six hours
the sweep recorded **43,604 `maintenance_base_ref`** observations and **220
`maintenance_refused_protected_base`** refusals — 220 datasets it declined to touch because something
resolves through their bytes. This is a live control, not a dormant one.

**WHAT REMAINS IS THE ENUMERATION'S REACH, and the code says so in its own docstring.**
`sibling_base_refs` is "one non-recursive call against a flat layout" — it collects referrers among
SIBLINGS of the dataset in question. A clone whose referrer lives in another bucket, or in a
deactivated warehouse the sweep does not walk, is invisible to it, and the evidence for a source is
only ever on the referring side. So the row's real content is the second half of its own close
condition: record the clone/branch -> (source, version) edge AT CREATION, and consult that registry
from every GC door, rather than rediscovering referrers by listing. That also removes the reliance on
a listing being complete, which `protected_roots` already has to report as `unreadable`.

**Branches now ride the same protection**, as of C8: they are discovered, they set flag 16, and their
parent is a sibling — so the pre-pass sees the reference. There is still no lineage edge for a clone. **Where.** `catalog/services/maintenance.py:91-124`,
`maintenance/.../base_refs.py:38-42,90`, `sweep.py:145-155`. **Closes it.** Record the clone/branch →
(source, version) edge at creation; tag-pin the source version; every GC door (sweep, purge, on-demand)
consults the registry; enumerate referrers over all registered buckets including deactivated.

### C4 · External-base lifecycle policy and cleanup identity
**What.** Ingest registers the source **bucket** as a base, so flag 16 makes the orphan scan refuse the
dataset, `report_is_clean` blocks every purge, and `protected_roots` protects the whole bucket; whether
`cleanup_old_versions` reclaims external in-base blobs is unverified against pylance 10.0.0 (the post says
yes, the docstring says nothing). **Where.** `ingest/adapters.py:296-306`, `lander.py:311-325`,
`maintenance/.../orphans.py:294-295`, `purge.py:223-224`, `features.py:113-114`.
**MEASURED ON THE DEPLOYED ESTATE 2026-09-07 — the row was right and is now quantified.** Read out of
GreptimeDB (`opentelemetry_logs`), because the pod formatter drops every `extra=` field:

    reconcile_report   total 611   incomplete 490   orphan_files 598   orphan_buckets 12

**490 incomplete scans**, and every `orphan_scan_skipped` reason is the flag refusal
(`unsupported manifest reader feature flags: 16 (base_paths …)`). So the coverage gap this row
predicts is not theoretical: the orphan pass declines roughly as many datasets as it inspects.

**The purge is blocked, but at the FIRST gate rather than the one the row names.**
`report_is_clean` returns on `report.total` before it ever reaches `report.incomplete`, so with 611
findings the estate would not purge even if every scan completed. Two independent brakes, and a third:
`MAINTENANCE_TRASH_PURGE_ENABLED` is report-only by default, a recorded destruction posture. Nothing is
wrongly deleting; nothing is being reclaimed either. Zero maintenance purge records in six hours.

**THE ORPHAN SCAN'S WHOLESALE FLAG-16 REFUSAL IS DELIBERATE AND MUST NOT BE "FIXED" THE OBVIOUS WAY.**
Compaction got the refinement — `gather_compaction_bases` distinguishes a base that is a dataset root
from a bare external prefix — and the scan deliberately did NOT, with the measurement recorded:
`add_bases` registers a base no `DataFile` resolves through yet, every `base_id` stays `None`, and the
scan passed such a dataset as `checked=True` with orphans named. **A scan that names live data as
garbage is a worse failure than one that declines.** So this row is closed by the per-base POLICY it
already proposes, never by widening the scan's mask.

**HONEST ACCOUNTING OF MY OWN CHANGE:** C8's branch discovery ADDS to `incomplete`. A branch sets flag
16, so each of the estate's 114 branches is now discovered and then correctly refused by this scan. The
report is more incomplete than it was and more truthful for it — those branches were never scanned
before either; the difference is that the number now says so.

**Closes it.** Parse `BasePath.is_dataset_root`; per-base `managed`/`reference-only` policy on the
warehouse record; cleanup credentials without delete on reference-only bases; a RED test pinning
pylance's behaviour on external blobs under a registered base.

### C5 · Storage profiles as bases
**What.** The estate runs "one endpoint, one key" (`config.py:60-62,101-102`); a warehouse-rooted
connection swaps only `root`. **Closes it.** `initial_bases` + `base_<id>.<key>` options on the warehouse
record; `target_bases` on the write doors; `aws_provider_scheme` once pylance ships it.

### C6 · Tiers as shallow clones plus columns (R9) — **ITS PREREQUISITE IS NOW MEASURED WORKING**
**What.** `compute.py` re-materialises managed blob bytes per tier; external descriptors are forwarded.
Confirmed at HEAD, and the code states both halves plainly: the external branch forwards the pointer
("every tier that copied them was storing the corpus again to express a readiness state" — measured
bronze 0.16% + silver 0.19% carried that way against ~100% per tier materialised), while the managed
branch carries bytes because "the bytes exist nowhere else, so carrying them IS the only option".

**THAT LAST CLAIM IS WHAT THIS ROW DISPUTES, and the dispute is now cheaper to settle.** A shallow
clone IS the other option: silver would reference bronze's bytes rather than copy them. The row gates
that on C3 because a referencing silver means bronze's bytes must never be reclaimed while silver
exists — and **C3's base-refs protection is now measured live and active**: 43,604
`maintenance_base_ref` observations and **220 `maintenance_refused_protected_base`** refusals in six
hours, with both on-demand doors passing `protected` (§C3). The guard the redesign depends on is not
hypothetical any more.

**Still an owner decision, and the row already says why:** "measure bytes and latency against the
copying path on one corpus before adopting". A clone-per-tier trades storage for a hard coupling —
bronze can no longer be reclaimed independently of silver — and that is a lifecycle choice, not a
performance one. C3's remaining half (a recorded clone -> source edge rather than a sibling listing)
is what would make the coupling durable enough to rely on.

**Closes it.** After C3: silver = shallow clone of bronze@N + `add_columns`; measure bytes and latency
against the copying path on one corpus before adopting.

### ~~C7 · Descriptor-first reads (R8) and `read_blob_ranges`~~ — **THE MAIN CLAUSE IS REFUTED 2026-09-07**
**"Query responses do not expose the descriptor struct by default" IS NOT TRUE.** Driven against the
deployed catalog — `POST /v1/table/acme-bronze$agnostic/query` asking for a blob column — and the
Arrow response carries the descriptor, not the bytes:

    payload  struct<kind: uint8, position: uint64, size: uint64, blob_id: uint32, blob_uri: string>
             metadata {'lance-encoding:blob': 'true', 'lance-encoding:packed': 'true'}
    a row -> {'kind': 0, 'position': 0, 'size': 16, 'blob_id': 0, 'blob_uri': ''}

So R8's headline — descriptor-first reads, where a query hands back WHERE the bytes are rather than
the bytes — is already the default on the query door, and the response is 3,058 bytes for three rows
rather than the payloads themselves. The `lance-encoding:blob` field metadata rides along, so a client
can tell a descriptor column from an ordinary struct without out-of-band knowledge.

**A NOTE FOR THE NEXT READER, because it cost several 422s:** `columns` and `vector` are generated
`oneOf` WRAPPERS, not the bare list and array the row's shape suggests. The body is
`{"k": 3, "vector": {"single_vector": []}, "columns": {"column_names": [...]}}`; a bare
`"columns": ["id"]` answers 422 *"Input should be a valid dictionary or object"*. An empty
`single_vector` is the non-vector scan.

**WHAT ACTUALLY REMAINS is the smaller half of the close condition:** `all_binary` as an opt-in on the
query door (today the descriptor is the only shape it serves), and documenting `read_blob_ranges` as
the batched client path. The `/blobs` door already streams `take_blobs` with Range/ETag, so the
byte-fetch path exists — what is missing is the BATCHED one and its documentation.

### C8 · Repack and branch maintenance in the sweep — **BRANCH HALF DONE AND VERIFIED LIVE 2026-09-07** (`236379fb`)
**The branches were invisible, and it was a live leak.** `discover_datasets` treats a directory holding
`_versions/` as a dataset and stops there — it never recursed into what a dataset CONTAINS. A branch
lives at `<dataset>/tree/<branch>/` with its own `_versions/` and `_transactions/`, so every branch in
the estate was never version-cleaned and never index-optimized. Measured before the fix: **85 of 250
catalog tables carried at least one branch, 114 branches in total.**

**DISCOVERY ONLY, and the measurement is what says that is enough.** A branch is exactly the
shallow-clone shape — pylance 10.0.0: flags `(16, 16)`, data files identical to the parent's at
`base_id` 0, and `tree/<name>/` holding only `_versions` and `_transactions` with no `data/` of its
own. So the three existing gates already answer correctly for flag 16 and none was touched: compaction
REFUSES it (rewriting a clone materialises the parent's data into it), the orphan scan REFUSES it
(list-the-prefix-subtract-referenced would call the parent's live files garbage), and root-scoped
`cleanup_old_versions` / `optimize_indices` PERMIT it via `SUPPORTED_FOR_GC`. Finding branches buys
exactly the maintenance that is safe on one, enforced where it already was. Only `tree/` is descended
into — a dataset's `data/`, `_indices/`, `_deletions/` and `_transactions/` are not datasets, and
probing each would be a wasted round trip per directory per dataset on the hot discovery path.

**PROVEN ON THE DEPLOYED ESTATE, not inferred from the unit tests.** Built with Dagger, rolled onto
`rask-maintenance`, and a sweep driven through the same `POST /maintenance-cron` the hourly binding
calls: `planned: 440, skipped: 12` across 93 buckets. The decisive per-bucket number —
**`advbr1-wh` reports `datasets = 6`, and that bucket holds ONE table carrying FIVE branches** (1 + 5),
where the old walk found 1. Zero new errors and zero `tree`-related access denials.

**READ THE COUNTS IN GREPTIME, NOT IN `kubectl logs`.** The pod formatter is
`"… — %(message)s"` (`service_kit/app.py`), so every structured field the fleet logs through `extra=`
is absent from the pod log — `compaction_bucket_discovered` prints its name and nothing else. The
fields are not lost, they ride the OTel path: `SELECT timestamp, log_attributes FROM
opentelemetry_logs WHERE body = 'compaction_bucket_discovered'` returns `{bucket, datasets, truncated}`
per bucket. Anyone verifying a discovery change by tailing the pod will conclude nothing happened.

**STILL OPEN — the REPACK half:** `compact_files` never passes a `compaction_mode`, and nothing repacks
packed sidecars.

**What.** `compact_files` never passes a `compaction_mode`; nothing repacks packed sidecars; datasets
under `tree/<branch>/` are never compacted, optimized or cleaned (`optimize.py:123-127`).
**Closes it.** Discover branch datasets; add repack; pin that compaction does not rewrite dedicated blobs.

### ~~C9 · Feature flags 32 / 64 / 128 (Q5 decided)~~ — **DONE 2026-09-07** (`2c04ad66`), one clause moot
Its four clauses, each answered:

**1. Name the three bits with their reader/writer requirement — DONE.** `FLAG_DISABLE_TRANSACTION_FILE`
(32, writer-required only), `FLAG_DATA_OVERLAYS` (64), `FLAG_COVERED_INDEX_METADATA` (128, sticky), from
`lance-table/src/feature_flags.rs` as Q5 recorded. The vendored `file_format.md` stops at 16 and calls
32+ "unknown" — the same doc-lag the vendored `spec.yaml` shows against upstream — so the Rust source is
the authority. **Naming is not supporting**: none enters `SUPPORTED`, so all three still refuse. What
changed is that a refusal now says `32 (disable_transaction_file (writer-required only))` instead of
`32 (unknown)`, which an operator can act on.

**2. Split the whitelist so report-only passes proceed on writer-only unknowns — DONE, and the spec
settles it rather than a preference.** `file_format.md`: *"Readers should check the
`reader_feature_flags` … Writers should check `writer_feature_flags`"*, with a per-bit table in which
one row is already asymmetric — `FLAG_TABLE_CONFIG` (8) is Reader-No / Writer-Yes. **Measured on pylance
10.0.0: `update_config({"k": "v"})` produces `reader=0, writer=8`**, so the asymmetry is what Lance
writes, not a note in a table. Every gate ORed the fields, so the ORPHAN SCAN — the estate's one
read-only, report-only pass — refused datasets over bits only a writer must understand. `unsupported_features`
now takes `describe_read_unsupported_flags` (reader field alone); compaction and GC are untouched and
pinned, because they write. **32 is the concrete case**: writer-required only, so a dataset setting it
is one a read-only pass may safely scan, where before it was refused outright.

Not a weakening — an unknown bit in the READER field still refuses, since that field is by definition
what a reader must understand and proceeding means enumerating a layout we cannot resolve.

**3. Never set `LANCE_ENABLE_UNSTABLE_DATA_OVERLAY_FILES` in a deployed image — VERIFIED, not merely
intended:** zero occurrences anywhere in the repo (outside this register's own prose) and zero in the
live Deployments.

**4. Support 32 with the pylance bump — MOOT for now, and correctly so.** pylance 10.0.0 carries no
symbol for 32 or 128, so refusal is the only correct answer today. It lands with A10's joint bump, not
before; the replay marker and `/history` read `.txn` files through `read_transaction`, which is exactly
what flag 32 disables.

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

### E1 · Lost origination events are unrecoverable and invisible — **MEASURED 2026-09-07: ingest is the ONE lossy producer**
**The clause about producers swallowing failures is confirmed, and narrowed to one service.** Four of
the five lakehouse producers already stage durably through the shared object-store outbox
(`service_kit.lakehouse.outbox` — `stage_event` / `drop_event` / `resolve_event`, "stage → publish →
drop"): **catalog, lineage, maintenance, medallion**. **`ingest` has zero outbox usage** and emits
bare, so a refused or unreachable lineage door loses the event outright.

**IT IS SWALLOWED BY DESIGN, and the design's own reasoning is right** —
`ingest/lineage.py::_emit`: *"A run whose data landed must not be reported as failed because the graph
was unreachable — that would turn an observability outage into a data incident."* True, and the outbox
is precisely the mechanism that honours that constraint WITHOUT losing the event: staging cannot fail
the run either, and what is staged is drained later.

**IT HAS ALREADY HAPPENED TWICE, and the estate wrote it down** —
`ingest/service_identity.py`: *"a 401 there does not surface as an error, it surfaces as a permanent
gap in the graph that looks exactly like a healthy estate. That has already happened twice on this
lane (the trainer in 2026-07, `service-ingest` on 2026-08-06, a day of 403s while the data landed)."*

**THE FIX IS ONE CHOKEPOINT, AND THE OBSTACLE IS AN API ONE — traced 2026-09-07.** Both emit sites
funnel through `ingest/lineage.py::_emit` (268 and 341 → 358), so the wrapping has a single home, and
the event is NOT hidden the way it first looks: `lineage_kit.runs.RunRecorder.start()` /
`.complete()` / `.fail()` each build the `RunEvent`, call `self.emitter.emit(event)` and **return it**
— ingest simply discards the return.

**The real obstacle is that `Emitter.emit()` cannot report what happened.** It swallows BOTH failure
modes by design — authoring and transport, each counted into `lineage_kit.metrics` via `record_drop`
and logged — and returns `None`. So the emitter KNOWS the event was dropped and the caller cannot ask.
Nothing downstream can decide to stage.

Two shapes close it, and they are not equivalent:

* **Make `Emitter.emit()` report success** (a bool, or a raise the caller catches). Additive, and the
  emitter already has the fact. Then ingest stages ONLY on a reported failure — which covers the two
  recorded incidents exactly, since both were a refused door rather than a crash. Its residual is a
  crash between the failed emit and the stage: a smaller window, not a closed one.
* **Build the event first, stage, emit, drop** — the medallion's shape (`transform.py::_build_stage_event`),
  which is why it is durable. Closes the crash window too, and costs a stage write on the happy path.

**THEY ARE NOT ALTERNATIVES — measured 2026-09-08, and the framing above was the thing blocking the
row.** Staging cannot know when to DROP unless the emit reports: a wrapper that stages, emits, and then
drops unconditionally has staged nothing useful, and one that never drops grows an outbox no relay can
distinguish from real backlog. **Reporting is the ENABLER for staging, not a lesser substitute for it.**
So the sequence is forced rather than chosen, and the trade the row recorded (one object-store write per
event against a narrow crash window) is a decision about the SECOND half only.

**FIRST HALF DONE.** `Emitter.emit()` now answers `bool`. Additive — it never raises, so the I8
constraint that emission must not turn an observability outage into a data incident is untouched — and
the emitter already computed the fact in order to log and count it (`record_drop(AUTHOR|TRANSPORT)`).
**The bool means "this event needs no recovery", not "it reached the graph"**, which is why
`NoopEmitter` answers True: a deployment with lineage switched off has lost nothing, and staging its
events would grow an outbox nothing drains — turning an opt-out into a leak. Gated by
`packages/lineage-kit/tests/test_the_emitter_reports_what_it_already_knows.py`, which drives both
failure modes through a `cast` structural client double and pins that a refused door STILL does not
raise.

**SECOND HALF IS NOT BLOCKED — my own "blocker" was a mis-measurement, corrected 2026-09-08.**
I read the ingest Deployment's `env` list, saw an S3 endpoint and no key, and concluded ingest held no
credential and therefore could not write a shared outbox prefix. The `env` list was the wrong surface:
the credential arrives through **`envFrom: rask-app`**, which that filter cannot see. Verified inside
the running pod, which is the only authority:

    AWS_ACCESS_KEY_ID     = rustfsadmin      <- the RustFS ROOT credential
    AWS_SECRET_ACCESS_KEY = <set>
    AWS_ENDPOINT_URL      = http://rask-rustfs-io:9000

So ingest can write the outbox today and the second half is ordinary work, not a decision.

**AND THE TRUE MEASUREMENT IS A WORSE FINDING THAN THE ONE I INVENTED — see § H8.** The service that
takes EXTERNAL, UNTRUSTED bytes runs as storage ROOT, while `rask-maintenance` beside it runs on a
scoped identity. My note that ingest was "the estate's least-privileged service" was exactly backwards.

**OWNER RULING 2026-09-08: ZERO TRUST — the first option.** `ingest` gets its OWN service identity,
scoped to the outbox prefix and nothing else. That rules out staging through the catalog (which would
borrow another service's authority for a path that exists precisely for when a service is unreachable)
and rules out accepting the residual. The policy is the narrowest in the estate: `PutObject` +
`DeleteObject` under `<root>/_lineage_outbox/`, no read of governed data, no other prefix. It is F2-1's
shape and makes `ingest` the second named identity after the medallion plane.

**Not attempted here** because it changes a SHARED package every producer emits through, and wants the
live verification its shape deserves: drive a real ingest run with the lineage door refusing, confirm
the event is staged, then confirm the lineage reconciler drains it.

**Not attempted here** because it wants live verification the shape deserves: drive a real ingest run
with the lineage door refusing, confirm the event is staged, then confirm the lineage reconciler drains
it. That is a deliberate deferral, not an oversight — the measurement above is what the row was
missing.

**What.** The reconcile sweep enumerates the graph, not storage, and skips nodes without `source_uri`;
every HTTP producer swallows failures. A lost `create/declare/register` means the table never exists in
lineage; a lost write on a known table is back-filled version-only. **Where.** `lineage/core/reconcile.py:169-172`,
`catalog/core/lineage_emit.py:598-604`, `lineage_kit/emitter.py:193-197`. **Closes it.** Enumerate the
catalog registry / warehouse roots; create the Dataset vertex from on-disk `lineage.dataset_id`; R10.

### E2 · Bus door trusts a producer-stamped author behind one shared token — **HIGH** — PRODUCER HALF DONE 2026-09-08
**Where.** `lineage/api/dapr.py::on_lineage_event` — authenticated by `require_dapr_token` (the
SIDECAR's shared credential) and then straight into `handle_cloud_event`, applying neither
`enforce_author` nor `enforce_output_authz`, both of which the HTTP door at `endpoints/ingest.py`
applies. `enforce_output_authz`'s own source already names the asymmetry.
**Closes it.** Q6 (owner-delegated 2026-09-02): *"The bus door applies `enforce_output_authz` as the
stamped subject either way."*

**Q6 CANNOT BE APPLIED AS WRITTEN, and the measurement is the reason.** `enforce_output_authz`
authorizes as `token.sub` and raises `UnauthenticatedError` when the token is `None`; the bus door has
no principal at all. Measured on the deployed graph 2026-09-08:

    664 of 5 644 (:Run) nodes carry NO author
    518   producer …/services/maintenance/src/maintenance/core/lineage_emit.py
     79   no producer either
     34   …/services/compaction/core/lineage_emit.py

So gating the bus on the stamped subject would refuse 11.8% of live traffic — the maintenance sweep's
entire lineage — which is silent provenance loss, the shape this file's own ORDER ranks first. **A
consumer-side gate is only safe once the producers sign.** That is the same ordering § E6's `parent`
facet needs, and both rows read as consumer-side work, which is exactly why it is worth stating twice.

**THE PRODUCER HALF IS DONE** (`791a5f5b`). `services/maintenance` already knew its own name —
`MAINTENANCE_CATALOG_SERVICE_IDENTITY`, default `service-maintenance`, which it presents at the
catalog's service door — so the graph and the catalog disagreed about who compacted a dataset, one
recording the service and the other recording nobody. Both builders now stamp it, COMPLETE and FAIL
(an unattributed failure is the one a person has to chase). Empty means ABSENT, never a placeholder:
`author_sub_from_payload`'s rule is that anonymous beats misattributed, and a fabricated subject would
be worse than none because the bus gate this unblocks would authorize it. Built with Dagger
(`e2-791a5f5b`), deployed to `rask-maintenance`.

**WHAT IS LEFT IS AN OWNER DECISION, not a refactor.** `can_write_data` resolves to `writer` on
`table:` (`model.fga:349`), so gating the bus means `service-maintenance` needs `writer` on **every
table it maintains** — which is every governed table in the estate. That is a real grant with real
blast radius, and the alternatives are not equivalent:

  * grant the sweep `writer` estate-wide — simple, and hands one service the widest write grant there is;
  * add a narrower relation (`can_maintain`) that `can_write_data` does not imply — more model, but the
    sweep's authority then matches what the sweep actually does;
  * authorize on `dapr-caller-app-id` instead of the stamped author — which § F2-5 measured as not
    armable yet: the estate invokes over THREE planes and the actor plane is uncharacterised.

**OWNER RULING 2026-09-08: ZERO TRUST — the second option.** A `can_maintain` relation that
`can_write_data` does not imply, so the sweep's authority matches what a sweep actually does (rewrite
files in place, reclaim versions) and stops short of what a writer may do (change what the data SAYS).
Granting `writer` estate-wide was rejected: it is the widest write grant in the estate and would make
one compromised sweep equivalent to compromising every table. The bus gate then authorizes
`can_maintain` for a maintenance-authored run and `can_write_data` for a data-authored one — the
relation follows the operation, not the caller.

**THE REMAINING 146 WERE TRACED, and they are not a second producer gap.** The 79 carrying no producer
either break down as: 66 `lance-catalog/create_table|drop_table` runs stamped
`2026-07-11T09:00:00Z`–`09:10:00Z`, which is the fixture window of
`test_terminal_lifecycle_and_column_gc_against_age` — e2e residue in a suite that points at whatever
AGE its DSN names; 12 from this session's own §E4 probes; 5 `medallion/derive_media` ABORTs. **The
catalog itself is not emitting anonymously**: 3 064 of its 3 130 runs carry an author (97.9%), and the
66 that do not are all inside that one test window. The other 34 are the retired
`services/compaction` emitter and are historical.

So the live author-less population is essentially the sweep alone, and the producer half above closes
it. That also sharpens what the owner decision is FOR: the bus gate would be authorizing one service
principal, not a long tail of unsigned producers.

### ~~E3 · Run state regresses on out-of-order ingest~~ — FIXED 2026-09-08 (`cypher.py`)
**Where.** `cypher.py::MERGE_RUN` — `r.event_type=$et, r.event_time=$tm, r.author=$au, r.producer=$pr,
r.error_message=$err`, five properties assigned last-DELIVERY-wins beside six that had already been made
conditional. Fed by Dapr redelivery, the `POST /admin/dlq/{run_id}/replay` door and external
OpenLineage producers over HTTP — none of which delivers in order.

**NOT A HYPOTHETICAL — it had already fired.** Measured on the deployed graph 2026-09-07: run
`f280fd32-617d-5292-9323-993d021bb79e`, the cascade's `derive_media` writing `silver-media$features`,
holds two terminal events in the durable feed —

    seq 118723   FAIL       2026-09-07T13:30:31.410352+00:00
    seq 118724   COMPLETE   2026-09-07T13:29:50.154780+00:00

FAIL delivered first, COMPLETE second, but the COMPLETE is stamped **41 s earlier**. The graph reported
that run **COMPLETE**, and `GET /runs/<id>` served `started_at` 13:30:31 with `updated_at` 13:29:50 — a
run that finished before it began. The erased `error_message` was
*"catalog could not say where 'bronze-media$objects' lives: HTTP 503"*, so a real cascade failure was
badged a success in the one place the estate records what happened. Two consumers read that badge:
`_fold_writes` marks a dataset failed only from a producing run's FAIL/ABORT, and `LATEST_WRITE` filters
`WHERE r.event_type = 'COMPLETE'`.

**Closed with TWO rules, because neither covers the other.** `MERGE_RUN` now binds one `supersedes`
predicate and gates the whole state family on it: an event OLDER than what is stored never writes (the
measured case — both terminal, so only time separates them), and a NON-terminal event never overwrites a
terminal one whatever its stamp (the row's own START-after-COMPLETE case, which a time test alone lets
through). Terminal-to-terminal is still decided by time, so a retry that genuinely succeeds later
supersedes. `started_at` became the EARLIEST time seen rather than the first DELIVERED one; `events_count`
stays unguarded because it counts deliveries.

**Verified where it lands, not where it is configured.** The `WITH`-bound predicate was probed against
the deployed AGE 1.5.0 before being written — that seam has a known quirk (a `SET` fused to a MERGE on an
EDGE drops `$params`, which is why four statements in `cypher.py` are split out), so a guard AGE silently
ignored would have looked identical in the source. Then built with Dagger (`e3-runstate`), deployed to
`rask-lineage`, and driven through the real HTTP door: FAIL@13:30:31 → COMPLETE@13:29:50 **blocked** →
START@14:00:00 **blocked** → ABORT@14:05:00 **applied**, ending `started_at` 13:29:50 / `events_count` 4.
The corrupted live run was then healed by replaying its own FAIL event from the feed through the fixed
door — it reads `FAIL` with its error message restored.

**Gates.** `tests/e2e-py/test_lineage_e2e.py::test_a_run_state_is_decided_by_event_time_not_by_delivery_order`
replays the measured shape against real AGE; `tests/unit/test_a_run_state_does_not_regress.py` is the
drift half — DERIVED from `MERGE_RUN`'s own source, so a new `r.foo=$foo` fails there (it names all five
at `HEAD~1`), and it pins `cypher._TERMINAL` equal to `postgres.TERMINAL_TYPES`, the two spellings of
"terminal" that nothing else keeps together.

### ~~E4 · `GET /events` is a lossy subset of the graph, and notifications replays from it~~ — FIXED 2026-09-08
**Where.** `consumer.py::record_event_best_effort` (own connection, opened AFTER `ingest_event`'s
transaction had already committed, swallowing every exception into a WARNING), `postgres.PRUNE_EVENTS`,
`config.py` `events_retention` (default 20 000), run per insert by `repository.record_event`.

**BOTH HALVES WERE WORSE THAN THE ROW SAID.**

*The lossiness was structural, not probabilistic.* The feed write happened on a second connection after
the graph transaction committed, on the stated ground that *"a feed-write failure must never break
ingest (the authoritative AGE graph write already succeeded)"*. A connection error, an eviction or a
statement timeout between the two therefore left an event permanently in the graph and permanently
absent from the feed — undetectable afterwards, because the two stores share no key to reconcile on,
and traced only by a WARNING whose `extra=` fields `kubectl logs` drops. FOUR call sites (HTTP ingest,
the JetStream consumer, the DLQ replay door, the reconcile relay) each carried their own paragraph
explaining that the second call must not be forgotten. `backfill_write` had the same split: four Cypher
statements deliberately made atomic, and its feed row left outside them.

*The retention was a window in **bigserial**, not in rows and not in time.* `DELETE … WHERE seq <=
MAX(seq) - 20000`, with the knob read as "keep 20 000 events". `INSERT_EVENT` is `ON CONFLICT DO
NOTHING` and Postgres consumes the sequence value BEFORE the conflict check, so **every rejected
redelivery burned a seq without leaving a row.** Measured on the deployed feed 2026-09-08:

    seq window 101229…121228 = 20 000     rows retained = 3 133     (84.3% burned on duplicates)
    oldest retained 2026-08-31T20:13Z     newest 2026-09-07T21:56Z     7 360 kB

The horizon shortened exactly when redelivery rose — when a consumer is most likely to be behind. It
had already cost: `notifications_feed_gaps_total` in GreptimeDB, one increment per reconciler pass that
found the feed pruned below its cursor, reads **0 → 559 on 2026-08-29 and 0 → 1 537 on 2026-08-30**,
flat 0 for the eight days since. 2 096 passes that could not tell whether anything had been lost.

**Closed.** `ingest_event` and `backfill_write` write their feed row inside their own graph
transaction — one write, nothing to keep in step, and a failure retries BOTH (the graph MERGEs, the
feed is `ON CONFLICT DO NOTHING`, so the stricter contract costs nothing). `record_event_best_effort`
and `feed_fields` are gone; `record_event` survives only as a declared seeding primitive with no
production caller. Retention became `LINEAGE_EVENTS_RETENTION_DAYS` (default 7, the horizon the feed
happened to hold) over a new `received_at timestamptz DEFAULT now()` — the ESTATE'S clock, never the
producer-supplied `event_time`, which this service stores as an unparsed string. It runs BATCHED on
the reconcile tick under that sweep's single-flight lock, not on every insert: the feed's hottest path
no longer pays for a DELETE per event, and two replicas can no longer race the same delete.

**Verified where it lands.** Built with Dagger (`e4-feed`), deployed to `rask-lineage`, and observed:
the boot DDL created `received_at` + `lineage_events_received_at` on the live database (existing rows
backfilled to the upgrade moment, so the first pass cannot delete history it cannot date); an event
POSTed to the real `/api/v1/lineage` door landed in the graph AND the feed with a database-stamped
arrival; a row backdated past the window was deleted by the reconcile tick at 22:37:16 (3 182 → 3 181),
and `pruned_events` now rides the sweep report on every tick. The window is a chart value
(`services.lineage.eventsRetentionDays`, beside `runRetentionDays`) rather than a code-only default, so
an operator can say how long an outage this estate's feed must survive.

**Gates.** `tests/e2e-py/test_lineage_e2e.py` — a feed write that fails takes the graph write with it
(RED against the pre-fix body: the failure was swallowed and ingest reported success), and the prune
drops exactly the rows received outside the window. `tests/unit/test_the_events_feed_has_one_door.py`
is the drift half: both graph writers must write their feed row in-transaction, no method may write it
on its own connection, and no module may reintroduce a best-effort projection (AST, not a text scan —
the name is discussed in prose). `tests/unit/test_lineage_auth.py` pins that recording an event issues
no DELETE; `tests/unit/test_reconcile.py` pins `pruned_events` on every tick, present even at zero,
because a key that appears only when it did something makes "nothing was pruned" and "the pass never
ran" the same observation.

### E7 · The lakehouse zone polls `/events` unauthenticated, forever
**Observed 2026-09-08** while verifying § E4, in `rask-lineage`'s own access log:

    10.42.0.147 - "GET /events?limit=1&summary=true HTTP/1.1" 401 Unauthorized

26 of the last 500 log lines, steady. `10.42.0.147` is `rask-web-lakehouse`, the zone's SSR server —
so a zone panel is polling a governed endpoint it has never once been able to read, and the failure is
invisible from the zone (a 401 renders as an empty feed, which is also what an idle estate looks like).

**NOT a lineage defect, and the contrast is the evidence.** The notifications reconciler reaches the
same feed correctly from `10.42.0.48` — `GET /events?limit=500&summary=false` → 200, `lineage_feed_reconciled`
every 30 s — because it sends lineage's service-door PAIR (`dapr-api-token` + `x-lance-service-identity`,
both or neither, see `reconciler._headers`). The zone sends neither, and SSR has no user token on that
path.

**Closes it.** Decide which identity that panel reads with — the zone's own service identity, or the
viewer's token forwarded from the BFF — and make the failure visible in the zone rather than an empty
list. Edge/zone work: this row is recorded here because it was measured here, and belongs to
`open_gateway.md` or the frontend register when it is worked.

### E5 · Unbounded growth, O(history) hot paths, no default pruning

> MERGED into **Q3-10** — the same defect (unbounded list reads with no server-side LIMIT) was tracked here and in the Python-audit ledger under two ids. Q3-10 is canonical: it carries the finding id and severity the audit assigned. Kept as a pointer rather than deleted, because this section's framing is how the defect was first seen.
**THE INDEX HALF IS DONE AND PROVEN ON THE LIVE GRAPH 2026-09-07** (`6d3cf663`). `lineage.Run`
carried exactly one index — `lineage_run_uniq`, the MERGE key — while SIX `ORDER BY r.event_time DESC`
sites in `cypher.py` sorted the whole label table, the runs board among them. Declared
`("Run", ("event_time",))` in `VERTEX_LOOKUP_KEYS`, which `ensure_graph_constraints()` already
materialises idempotently on every boot; no new machinery. Deployed and measured with the planner
itself:

    Limit  (cost=0.28..68.72 rows=200)
      ->  Index Scan Backward using lineage_run_lookup on "Run"  (cost=0.28..1911.65 rows=5586)

**BOUNDING A RESPONSE IS NOT BOUNDING THE WORK, and this register bounded the response first.**
`/runs` was capped at 200 rows earlier the same day (the board was returning 5,122 runs / 2.65 MB) —
that LIMIT bounds what crosses the wire, and the ORDER BY still read and sorted every row behind it.
The morning's fix was half a fix; this is the other half, and the cost stops growing with the table.

**THE RETENTION HALF IS STILL OPEN, and the finding is sharper than the row's phrasing.** It is not
that there is no pruning — the mechanism EXISTS and ships OFF: `runRetentionDays: 0` in the chart and
`LINEAGE_RUN_RETENTION_DAYS=0` on the deployed service. Growth is real and measured today: the graph
went **5,514 -> 5,586 Run**, 1,230 -> 1,236 Dataset, 2,429 -> 2,460 Job over a single day, with
`lineage.Run` at 4,176 kB. Turning it on is an owner decision about how much history the graph owes —
`values.yaml:1534` already notes the flip side, that a recovered dataset's FAIL Run node lingers.

**Where.** `values.yaml:404`, `repository.py:352-354,660,711`, unbounded `*1..` traversals.
**Closes the rest.** `latest_version` on the Dataset node; bounded paths; paginated `/producers`.

### E6 · Model gaps
**What.** Versions are `WROTE` edge properties (one per run+dataset); no branch/tag/clone/base
representation; the `parent` run facet is not represented; column lineage latest-only; rename strands
history on the old vertex. **Closes it.** Version and branch nodes; persist `parent`; clone edge (C3);
rename carries history.

**THE `parent` CLAUSE IS A TWO-SIDED GAP, measured 2026-09-08, and the row's wording hid half of it.**
It said "parsed and discarded". It is not parsed: `services/lineage` contains **no `parent` handling of
any kind** — grep the whole service and the only hit is `Path(__file__).parent`. So the ingest side
would drop it if it arrived.

**AND IT DOES NOT ARRIVE.** `lineage_kit` can stamp it — `schemas.py:222` renders the facet and
`runs.py:137` / `stage.py:51` / `actor.py:55,87` pass a parent — but on the deployed estate:

    0 of 3 181 durable feed events carry `run.facets.parent`
    0 `(:Run)` nodes record any parent

So fixing only the consumer changes nothing observable, and fixing only the producer writes a facet
into a store that drops it. That ordering is the finding: this row needs BOTH halves in one change, and
a test that drives a real cascade rather than a synthetic event — the cascade is the only producer with
a parent to declare, and it is currently declaring none.

---

## F. Zero trust — the diff against Lakekeeper (R11)

**Premise check first.** Lakekeeper never uses the words "zero trust" anywhere in its repository (docs, README, crates). What it claims is "secure", "every request is checked against your policy before any data is read, and recorded", vended credentials or remote signing, and "does not issue API-Keys". So this section diffs what Lakekeeper *implements* against what rask implements, control by control, at Lakekeeper v0.13.1 (b328e58) and rask `feec956`. Full report with every citation: `docs/audits/lakehouse-2026-09/sweeps/zero-trust.md`.

**Score.** 19 controls: rask HAVE 6, STRONGER 3, PARTIAL 8, MISSING 1 (per-workload storage identities: catalog, maintenance and every stage runner run as the RustFS root user). Both sides lack image signing and SBOM. Lakekeeper is stronger on service identity (one Kubernetes SA per service, no shared secret), location containment and audit request-ids; rask is stronger on token validation, vending posture, mTLS on the Dapr plane, pod hardening, network policy and fail-closed behaviour.

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
| 7 | Catalog's own storage identity least-privilege; per-warehouse identities | **Impl.** One credential per warehouse (storage.md:7; production.md:21 "distinct credentials that only grant access to the prefix"); system identity must use `assume-role-arn` + `external-id` by default (configuration.md:50-51); location-exclusivity check (validation.rs:60-62). | **One root key for the whole fleet**: catalog gets `rustfs.accessKey` = `rustfsadmin` (chart/templates/services.yaml:92-94; values.yaml:1517-1518 — the RustFS root user); maintenance identical (maintenance.yaml:123-125; services/maintenance/src/maintenance/core/config.py:88 default `"rustfsadmin"`); medallion producer and every stage runner identical (medallion.yaml:234-236, 389-391). OpenBao only changes *where the secret value comes from* (services.yaml:93-99), not *which identity* it is. | **MISSING** — largest single gap. |
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

> **TRACKED AS Q17-5..16.** These twelve are a markdown list, which this file's own counting gate
> cannot see — so the estate's largest security gap sat outside every header count. The analysis
> stays here; the countable rows are in § Q17.

1. **Per-workload storage identities.** chart/templates/services.yaml:92-94, maintenance.yaml:123-125, medallion.yaml:234-236 & 389-391, services/maintenance/src/maintenance/core/config.py:88 — stop running catalog, maintenance and every stage runner as the RustFS root user; provision one least-privilege RustFS user/policy (or STS role) per service, scoped to its buckets/prefixes.
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

#### F2 · THE TWELVE VERDICTS (moved here 2026-09-08 from `open_goal.md`)

**Why this lives here now.** `open_goal.md` is injected verbatim on every Stop and had grown to 23 362
characters against a 4 000-character budget — six times over. A goal that cannot be read is not a goal,
and a completed audit record is not an instruction: these verdicts are EVIDENCE, and evidence belongs in
the register. The twelve numbered items above are the original 2026-08-25 analysis; what follows is what
each turned out to be when it was measured. Where the two disagree, the verdict is later and wins.

**§F2, ZERO TRUST, WAS THE TOP OF THE BLAST-RADIUS ORDER AND IS NOW FULLY EXAMINED** — twelve of
twelve with a verdict as of 2026-09-07. The estate has not REACHED zero trust; what changed is that
nothing in this section is unknown any more. The sweep scored 19 controls (HAVE 6, STRONGER 3,
PARTIAL 8, MISSING 1) and said items 1-4 "decide whether the claim is honest":

    F2-1  per-workload storage identities — DONE for the medallion plane, and as of release 102/103
          they are RELEASE INTENT rather than drift: `helm get values` carries
          rustfs.medallionAccessKey + maintenanceAccessKey, the post-upgrade hook rotated both RustFS
          users onto DERIVED secrets, and the governed-union suite passed 5/5 after the roll (Q17-17
          closed).
          **`rask-lineage` DONE 2026-09-07**, and measuring it produced the TIGHTEST policy of the four
          because its SURFACE is smallest, not because it matters less: NO `PutObject` anywhere — it
          opens datasets read-only to probe versions and the only bytes it changes are DELETES of its
          own outbox (`outbox.drop_event`; `stage_event` belongs to the producers). The READ stays
          deliberately WIDE, which is the interesting half: it reconciles datasets this chart cannot
          enumerate, so a narrowed read is a reconciler that silently stops seeing part of the estate —
          and one that cannot read reports `known=False`, publishing nothing and looking exactly like a
          healthy cascade. DECLARED BUT NOT ARMED (`lineageAccessKey` defaults empty, so an install
          keeps the root credential), the way medallion and maintenance shipped before they were named.
          **`rask-catalog` is the ONE identity left**, and it stays a design question rather than
          another copy of the pattern: the catalog vends credentials for every runtime-minted
          warehouse, so "what may the thing that grants access itself reach?" has no answer this
          policy shape supplies.
    F2-2  fail closed in CODE — DONE 2026-09-07 (`2c69c270`). `assert_authentication_configured`
          refuses to boot a governed service whose auth is off with nobody having acknowledged it;
          the refusal is on the AMBIGUITY, not on being open. Landed for the three services that
          have a human door (catalog, lineage, the medallion producer); `maintenance`,
          `notifications` and the stage runners have none and were deliberately left out.
    F2-3  kill the one shared service bearer — THREE OF FOUR, all proven ON THE WIRE rather than in
          a render. `service-web` (`bf273f07`), `service-maintenance` and `service-ingest`
          (`9405b732`) each present their own credential; ingest's was driven both ways — the
          dedicated token answers 200 at lineage and the SAME privileged name with the shared bearer
          answers 401. A privileged credential has THREE halves (present, demand, SEED) and the third
          nearly shipped a 401 twice.
          `notifications` is the fourth and CANNOT, for a reason that is not about the service:
          landed and reverted live 2026-09-07 (release 107 → 108). Its client half is correct and its
          reconciler still 401'd — `the presented credential may not claim 'notifications'` — because
          it reaches lineage through DAPR SERVICE INVOCATION and daprd stamps its own
          `dapr-api-token` on every request it delivers. A DEDICATED CREDENTIAL IS A PROPERTY OF THE
          TRANSPORT, NOT ONLY OF THE SERVICE: ingest holds one at the same door only because it calls
          lineage directly over HTTP. The remainder is a design question — move that call off service
          invocation, or accept that sidecar-invoked hops authenticate as the estate.
    F2-4  stop laundering ANONYMOUS browser reads into a service identity — DONE 2026-09-07,
          fail closed by owner ruling. The subject held 7 reader grants across TWO tenants plus a
          writer, seeded by a documented production prerequisite; all eight revoked, both seeds
          gated, and the two e2e suites that read AS it (which is why it survived) now read as a
          user. Live: anonymous 403 / signed-in 200.

Then F2-5..12, ALL of which now have verdicts (2026-09-07):

    F2-8   the dead `static` vending mode — DONE, DELETED (Q17-12). It could be selected and never
           got: `main.py` passes no `static_keys`, so it built an empty vendor that answered None for
           everything and degraded to the mode it was chosen instead of. Gated by the GENERAL form —
           a test that parses the real call site and refuses any permitted mode needing more.
    F2-9   refuse well-known defaults — DONE (Q17-13). Four guards already existed and all four keyed
           on one OPT-IN flag; `prod-credentials.yaml` now answers "is this a real deployment?" once,
           unconditionally, on a signal an operator cannot forget.
    F2-6   TLS to every store — RE-MEASURED AND SIZED 2026-09-07, still open, and the size is the
           finding. Counted off the live Deployments rather than the templates: **171 `http://` store
           URLs**, 9 `https://` (all EXTERNAL), and one `postgresql://` with no `sslmode`. Dapr mTLS
           is the estate's only transport security and every store sits outside it.
           **THE ONE DSN IS NOT THE TRACTABLE SLICE IT LOOKS LIKE**: `SHOW ssl` on the running
           `rask-age-0` answers **off**, so the server offers no TLS at all and `sslmode=require` on
           the client would be an outage — the same asymmetric ordering as a credential's two halves.
           This is a SERVER change (certificate + `ssl=on`) before it is a connection-string one.
           Measuring it also corrected `CLAUDE.md`: AGE and OpenFGA are served by the `rask-age`
           StatefulSet, NOT CloudNativePG — zero `Cluster` objects exist, `age.cnpgCluster.enabled`
           defaults false, and the CNPG OPERATOR is installed with nothing to reconcile. An enabled
           operator toggle is not evidence the resource exists.
           **AND THAT IS ALSO THE ANSWER, which is why hand-rolling TLS here would be the wrong fix.**
           The estate has no cert-manager and the chart has never generated a certificate — but the
           CHARTED path already issues them: a CNPG Cluster gets server TLS automatically. So the AGE
           half of F2-6 is the CNPG cutover the chart already anticipates, not a StatefulSet patched
           with `genSelfSignedCert`. **Its stated blockers were re-measured and TWO OF THREE NO LONGER
           HOLD** — the note was written when they did and nobody re-checked:

               K8s 1.33+ with ImageVolume     required   ->  v1.36.2 (gate default-on at 1.35)  MET
               CNPG >= 1.27                   required   ->  operator 1.29.1                    MET
               containerd >= 2.1              required   ->  containerd 2.3.2-k3s2              MET
               the AGE extension image        required   ->  BUILT 2026-09-07                   MET

           **AND THE CUTOVER'S LAST UNPROVEN LAYER IS PROVEN TOO.** `docs/CNPG-AGE.md` recorded a
           2026-07-20 investigation: layers 1 (the AGE extension + `extension_control_path`) and 2
           (the ImageVolume prerequisites) PROVEN, and layer 3 — the operator managing a real Cluster
           — failed as an explicitly-diagnosed KIND-HOST gremlin across CNPG 1.30/1.28. On THIS estate
           the operator is 1.29.1, **1/1 Running**, leader-elected, and its log carries hourly
           `pki: Periodic TLS certificates maintenance` — the very machinery this row needs, running
           and idle for want of a Cluster. The image is `age-cnpg-ext:1.7.0-18`
           (`sha256:2b9572f1…`), verified from the REGISTRY MANIFEST rather than a build log: three
           layers matching the three `COPY` lines, including **1,497,689 B of `/lib/age.so`**.
           (`dagger core container … entries` lists a `FROM scratch` image as EMPTY — it lists
           `alpine:3.20` fine, so the manifest is the check to trust. That nearly got reported as a
           build defect.)
           **A SECOND WIN THIS ROW NEVER MENTIONED**: CNPG does PHYSICAL backups + PITR, which deletes
           the "does a logical `pg_dump` round-trip the AGE graph labels?" DR hazard —
           `scripts/age_restore_drill.sh` exists to prove against exactly that. The cutover buys TLS
           and removes a restore risk.
           **WHAT STAYS AN OWNER DECISION is the cutover itself, because it is a DATA MIGRATION**: the
           lineage graph and OpenFGA's tables live in the StatefulSet's PVC, and `age-cluster.yaml`
           deliberately fails the render if both stores are on. Building the image was cheap and
           reversible; moving the data is neither.
    F2-11  lock root create — DONE 2026-09-07 (`e6f4ce37`). THE SHIPPED DEFAULT WAS THE DEFECT, not
           its value: `hasKey` finds a key whether or not anyone chose it, so `values.yaml`'s
           `lockRootCreate: false` beat any derivation and the control could only be armed by an
           operator who already knew to arm it. The key is deleted; `services.yaml` derives it from
           `rask.isRealDeployment`, ONE helper now shared with `prod-credentials.yaml` so the two
           cannot disagree about what "real" means. Rendered three ways: local loop false, real
           deployment true with nobody arming it, explicit override winning in BOTH directions.

    F2-7   validate `register_table` locations — LARGELY REFUTED (Q17-11), by driving the deployed
           door rather than reading one layer of it: an ABSOLUTE location answers 400, a relative
           TRAVERSAL answers 400, and a plain relative path resolves inside the caller's OWN
           warehouse. §F2-7 read the Python door — which genuinely has no check — and concluded there
           was none anywhere; the enforcement lives in the native lance-ns backend beneath it. What
           remains is defence-in-depth, not the cross-tenant hole recorded.

    F2-10  correlate audit records — REFUTED (Q17-14), and instructive because the row is literally
           true and practically false. True of the CALL SITES: 118 `audit()` calls and not one passes
           a request or trace id. False of the RECORDS, which is what a compliance query reads —
           `CorrelationFilter` stamps `request_id` and `trace_id` on every record and `app.py:84`
           installs it on the ROOT handler, so `lance.audit` is stamped like any other logger that
           propagates there. Counting where a field is PASSED rather than where it is STAMPED reports
           a control missing that is present — the mirror of Q17-20, where a field that WAS passed
           turned out to be read by nothing. ITS SECOND CLAUSE IS CONFIRMED, below.

    F2-12  sign and attest images — CONFIRMED (Q17-16), with a naming trap that makes the opposite
           easy to believe. `.dagger/images.go` has a helper called `provenance()`, and it emits three
           OCI LABELS — BUILD_DATE, VCS_REF, VERSION (13 dockerfiles declare the ARGs and turn them
           into real `org.opencontainers.image.*` labels; re-checked 2026-09-07, so the row is right
           as written). No SBOM, no signature, no in-toto/SLSA attestation.
           **BUT THE FIX IS NOT "ADD COSIGN", and measuring 2026-09-07 is what says so.** The estate
           has **ZERO** signature verifiers — no Kyverno, no sigstore policy-controller, no
           Gatekeeper; its five validating webhooks are CNPG, external-secrets and Kueue. **A
           signature nothing verifies is decoration**, which is the exact anti-pattern this section
           has produced six times today, and adding one would make a seventh: a control whose NAME is
           present and whose enforcement is not. Signing needs a key custodian AND an admission-time
           verifier before it is a control, and both are owner decisions.
           **THE SBOM HALF IS DIFFERENT and partly already delivered**: `make audit` runs osv-scanner
           over six lockfiles plus `.dagger/go.mod`, `make scan-config` runs trivy over `.docker/` +
           `chart/`, and `make scan-image` runs trivy over a DAGGER-BUILT image. So the estate already
           answers "what vulnerable things are in this?" — what an SBOM adds is a PORTABLE manifest a
           downstream consumer can scan without rebuilding, which is a supply-chain claim rather than
           a scanning gap.

    F2-5   Dapr access control — ITS OWN PREMISE IS REFUTED (2026-09-07), and the refutation is what
           stopped it shipping as an outage. This file said "only TWO service-invocation callers
           exist, so a defaultAction:deny needs 11 allow entries". Both callers are real and both
           are on the HTTP `/v1.0/invoke` path — the only surface that grep could see. The estate
           invokes over THREE planes:

               HTTP /v1.0/invoke   gateway, notifications
               ActorProxy          annotator, notifications      never counted
               Dapr Workflow       flows, ingest, medallion      never counted; it IS actors

           Dapr's own docs settle half and open the other half: "Service invocation access control
           does not cover cross-app workflow scheduling" — there is a separate `WorkflowAccessPolicy`
           this estate has never heard of, a SECOND unrecorded gap. Actor-to-actor is documented
           neither way, and the estate has 28 `ActorProxy` refs behind the notifications inbox and
           the annotator's projects. The PRECONDITION does hold, measured live: Dapr mTLS true,
           Sentry running, `lance-tracing` is the one shared Configuration. What it needs first is
           the actor plane characterised on a live estate, because no document answers it.

    F2-10's SECOND CLAUSE — CONFIRMED 2026-09-07, so the row is half refuted and half proven and both
           halves needed DRIVING rather than reading. Audit records do reach GreptimeDB — 477,096 rows
           in `opentelemetry_logs` — and that is the problem. Three measurements against the live
           store: a `DELETE` on the audit stream is ACCEPTED (`affectedrows: 0`, the predicate simply
           matched nothing); the table declares `ttl = '14days'`, so every audit record is destroyed a
           fortnight after the decision it records; and both queries, the DELETE included, were issued
           to `:4000/v1/sql` with NO credentials from inside the cluster. Audit also shares ONE table
           with all other telemetry, so it can carry neither its own retention nor its own access
           policy. What the row needs is a SEPARATE append-only sink, not a setting.

**§F2 IS NOW TWELVE OF TWELVE WITH A VERDICT** — five fixed, four refuted or half-refuted, three
measured-and-open with the measurement recorded. Nothing in this section is unexamined; what is left
is work or an owner decision, never an unknown.

#### G1 / G1b · the compute-seam record (moved here 2026-09-08 from `open_goal.md`)

Same reason: G1 is DONE and a struck condition is provenance rather than an instruction, and G1b's
remainder is an owner decision that a reader of the register needs to see rather than a reader of the
goal.

**~~G1 — CLOSE C4.~~ DONE 2026-09-07.** `test_governed_union_e2e` went **5 failed -> 5 passed** live,
built with Dagger and deployed to k3s. Both items below are struck; the five causes and what each
turned out to be are § Q16.

  * ~~**The trainer's dedicated credential reaches the live Ray head.**~~ **DONE 2026-09-07.** Code
    (`96e6885f`), the key in `rask-infra-credentials`, and the head repointed at it and rolled. Proven
    live: `test_train_lineage_lands_attributed_under_governance` passes, and the newest train job's
    log carries ZERO `lineage emit attempt … rejected: HTTP 401` lines where every previous run
    carried four.
  * ~~**`POST /ingest-media` stops answering 503.**~~ **DONE 2026-09-07** (`a86f5407`). The head asks
    (`ensure_stage_output`), writes where told, and names that location on the `medallion.media`
    trigger as `from_uri`; the stage runner asks where its own upstream lives and takes that as both the
    upstream and the confinement root, which NARROWS what a trigger may name rather than widening it.
    Proven live: `test_media_lane_derives_under_governance` passes.

**G1b — THE TWO SEAMS STAY BYO, AND THE DEPLOYED PATH MUST USE THEM.** Tracked as Q17-1..4.
**THE DEPENDENCY-GRAPH HALF IS DONE 2026-09-07** — re-measured after the move:

    catalog / lineage / maintenance / service-kit   0 `import ray`, 0 declared ray dependency
    medallion                                       0 `ray_kit` imports, 0 declared ray dependency
    services/compute                                declares ray-kit — THE ONE ADAPTER, by role

  `ray_kit.submit` was pure HTTPX with exactly ONE production consumer (medallion), while the ray-kit
  PACKAGE declares `ray[default]` for its SDK half. So it MOVED to `medallion.services.ray_jobs_api`,
  that service's Ray adapter, and `ray-kit` is now what `services/compute` alone needs. Gated by
  `test_no_service_depends_on_a_compute_engine.py`, whose exemption for `compute` is a ROLE (a service
  may ADAPT an engine and must not DEPEND on one) and is itself gated against becoming a blanket.

  **WHY THIS FILE'S OWN EXPLANATION WAS WRONG, and it matters because it is the reason the work
  stalled.** It said "the port was built and the callers were never migrated". They could not have
  been: `RayJobExecutor` renders `WorkOrder.to_env()` into the job's runtime_env, and the job programs
  read six differently-spelled names — **overlap 0 of 6**. Anything submitted through the port would
  have started with NO inputs bound, and an empty source URI is not a crash: it is a run that scans
  nothing, writes nothing and reports success. Converged 2026-09-07 (`32ff50cb`) across FIVE authors —
  three job programs, the submitter, the sealed `runners/dummy`, plus an e2e fixture and a runner test
  found only by grep because `runners/*` is in no root testpath. Gated whole-tree.

  **WHAT REMAINS, and it is a decision rather than a refactor.** `executor_for(...)` is still called
  nowhere outside its registry, so the deployed path reaches Ray through `ray_submit`. Putting the
  PORT in front needs a durable adapter to bind to, and `RayJobExecutor` submits a `RayJob` CR that
  KubeRay must reconcile against a `RayCluster` — of which this estate has **ZERO**: the live Ray is
  `ray-lance-head`, a HAND-APPLIED plain Deployment with no ownerReferences. Chart-owned RayCluster,
  or ephemeral per-job clusters, is an owner decision about job-record durability.

  So the LAKEHOUSE has no notion of a compute engine, and that is not to regress — the ports
  (`service_kit.lakehouse.executor` for compute, `.saga` for the workflow engine) name no engine and
  are gated by `test_the_executor_port_names_no_engine.py`.

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

### ~~H1 · On-demand doors destroy live shallow clones — **HIGH** (see C3)~~ — **REFUTED 2026-09-07**
The same claim as §C3's headline clause, and refuted by the same reading: `endpoints/maintenance.py`'s
`run_maintenance` computes `protected = await _base_refs(ds, so)` and passes it into `run_gc`, and
`compact_maintenance` does the same before `require_compactable`. Both doors carry the guard. The
service functions default `protected` to `None`, which is why the CALL SITES had to be read rather
than the signatures. Measured live: 220 `maintenance_refused_protected_base` refusals in six hours.
### H2 · Bucket-granular external bases freeze purge and protect whole buckets — **MEASURED via C4, 2026-09-07**
A cross-reference carrying no content of its own, which is not a verdict — so here is C4's, in the
terms this row states them.

**"Freeze purge" — TRUE, and over-determined.** Measured on the deployed estate: `reconcile_report`
answers `total 611, incomplete 490`, and `report_is_clean` returns on `report.total` BEFORE it reaches
`report.incomplete`, so the purge is blocked by the findings alone. `MAINTENANCE_TRASH_PURGE_ENABLED`
is report-only by default besides. Three independent brakes; zero maintenance purge records in six
hours. Nothing is wrongly deleting and nothing is being reclaimed.

**"Protect whole buckets" — TRUE, and the protection is live rather than theoretical**: 43,604
`maintenance_base_ref` observations and **220 `maintenance_refused_protected_base`** refusals in six
hours. A bucket-granular base makes the refusal coarse, which is the row's point — but the coarse
refusal is doing real work, not sitting idle.

**Closes it where C4 does**: a per-base `managed` / `reference-only` policy on the warehouse record,
never by widening the orphan scan's mask — which the estate already measured as the wrong fix (an
`add_bases` prefix through which no `DataFile` resolves let the scan pass a dataset as `checked=True`
with live files named as orphans).
### H3 · Clone protection bounded to maintained buckets — **MEASURED 2026-09-08: the cross-BUCKET half is refuted, the cross-ROOT half is real**
C3's headline clause (the on-demand doors destroying a live clone) is REFUTED — both `run_maintenance`
and `compact_maintenance` compute `_base_refs` and pass it, and the guard records 220 live refusals.
**What survives is exactly this row**, and the code states the bound in its own docstring:
`sibling_base_refs` is *"one non-recursive call against a flat layout"*, so it collects referrers among
SIBLINGS. A clone whose referrer lives in another bucket, or in a deactivated warehouse the sweep does
not walk, is invisible to it — and the evidence for a source only ever exists on the referring side, so
nothing about the dataset in front of you reveals the danger.

**Branches now ride this protection** (C8): they set flag 16 and their parent is a sibling, so the
pre-pass sees the reference. That is the same-bucket case working; it does not extend the reach.

**MEASURED ESTATE-WIDE 2026-09-08, 93 buckets, every flag-16 dataset opened — and the row's premise
splits in two, one half confirmed and one half refuted:**

    419 flag-16 datasets, 539 base references
    all refs            same-root  11   cross-root 125   CROSS-BUCKET 403
    declared ROOTS      same-root   5   cross-root 115   CROSS-BUCKET   0

**REFUTED: the cross-BUCKET clone.** Zero base references that declare a dataset root cross a bucket,
so the shape this row worried about — "a clone whose referrer lives in another bucket" — does not exist
on this estate. `sibling_base_refs`'s docstring defends its bound with exactly that claim ("a shape
nothing in this estate creates") and the measurement supports it FOR CLONES.

**But the docstring's claim is stated too widely and the estate does create cross-warehouse references:**
403 of the 539 cross a bucket, 409 of them pointing at `s3://lance-catalog/models/` with
`is_dataset_root=FALSE` — the EXTERNAL BLOB BASE shape (`initial_bases`), not a clone. Those are
harmless to this guard: nothing under `models/` is a Lance dataset (measured: a depth-8 walk finds
zero), so the sweep never maintains it and there is nothing there to rewrite. The prose should say "no
cross-warehouse CLONE", not "a shape nothing in this estate creates".

**CONFIRMED, and it is the half worth acting on: 115 declared dataset roots cross a ROOT boundary
inside one bucket.** `sibling_base_refs` computes `root = location.rsplit("/", 1)[0]` and lists that
directory, so its reach is the dataset's own PARENT — a referrer under a different parent in the same
bucket is invisible to it by construction. Branches are the obvious population: a branch lives at
`<dataset>/tree/<name>`, whose parent is `<dataset>/tree`, while the dataset it protects sits at
`<dataset>` whose siblings are the bucket's top level. Neither is in the other's listing.

**THE EXPOSURE IS SCOPED TO THE ON-DEMAND LANE, and that is why nothing has broken.** The SWEEP does
not use `sibling_base_refs` — `sweep.py::_protected_roots` opens every discovered dataset in every
bucket, and `discover_datasets` descends into `tree/`, so the whole-estate pre-pass sees these
referrers and refuses (220 live refusals a tick). What cannot see them is the catalog's on-demand
maintenance doors (`maintenance.py:46`) and the event lane, both of which take the cheap sibling
listing precisely because they cannot afford the estate-wide pass.

**Closes it** with C3's second half — record the clone/branch -> (source, version) edge AT CREATION and
consult that registry from every GC door, instead of rediscovering referrers by listing. That is now
measured rather than argued: a registry is the only thing that makes the on-demand doors as safe as the
sweep without paying the sweep's cost, and it removes the reliance on a listing being complete, which
`protected_roots` already has to report as `unreadable`.

### H4 · No lease, no deployment strategy, unpersisted retry state — **THE STRATEGY CLAUSE IS DONE 2026-09-07**
**`strategy: Recreate` — DONE, and measuring it is what showed the pin was never enough.** The steady
state was already right: `rask-maintenance` is `replicas: 1`, and that is a CORRECTNESS constraint
because `bindings.cron` is uncoordinated (each replica runs the schedule independently) and this
service holds no lease. Measured on the deployed estate:

    rask-maintenance   replicas 1   strategy RollingUpdate   maxSurge 25%

**Kubernetes rounds `maxSurge` UP**, so 25% of one replica is one surge pod — for the length of every
rollout there were TWO maintenance pods, each with a cron binding, each able to tick. The pin held in
steady state and the transition spent it, which is exactly why `replicas: 1` alone never proved the
invariant. `tests/unit/test_prod_ha_posture.py` reasons about how many replicas a service RUNS and
records why three may not scale; it does not reach the rollout, and
`test_a_correctness_pinned_replica_does_not_surge.py` is the other half — the pin has to survive the
deploy that applies it. The cost is a short gap on a service whose work is an hourly cron with no
request path to interrupt.

**SCOPED TO THE CORRECTNESS PIN, deliberately.** Seven other deployments hardcode `replicas: 1` (dex,
openbao, the collector, alerting, dapr-dashboard, age-postgres) and none was measured to have an
unsafe-concurrency constraint — **and none mounts a PVC**, so the RWO-volume deadlock that usually
motivates `Recreate` does not apply to them either. Forcing it there would trade zero-downtime
rollouts for nothing, which the second test in that file refuses.

**STILL OPEN — the other two clauses**, and they are the substantive ones: a conditional-put lease per
tick and per dataset (`records.create_json` already exists), and `attempts` / `last_refusal` persisted
on the trash record so a repeatedly-refused record is visible as a permanent exclusion rather than a
transient one. **Where.** `routes.py:61`, `trash.py:80-91`, `purge.py:442-445`, `sweep.py:204-206`.

### H5 · No per-object GC audit — **CONFIRMED AND SHARPENED 2026-09-07: the PURGE emits, the SWEEP does not**
**Both clauses hold, and the asymmetry between two paths in the same service is the finding.**

    sweep / optimize   audit() calls          0
    sweep / optimize   control events         0
    purge              control event          table_purged / namespace_purged  (emitted)

So the DESTRUCTIVE path is auditable and the ROUTINE one is not — the reverse of what an operator
would guess, and the reason nobody noticed: a purge is rare and visible, a sweep runs hourly over
every dataset in the estate and leaves no per-object trace of what it rewrote or reclaimed.
`table_maintained` is absent from `ControlAction`'s 38 values, so as with §C2 there is nothing for the
sweep to emit even if it wanted to.

**THE FIRST CLAUSE IS DONE 2026-09-07.** `maintenance_dataset_outcome` — one structured line per
dataset per tick, carrying `DatasetResult`'s own fields (`dataset`, `table_id`, `mode`,
`fragments_removed`, `fragments_added`, `old_versions_removed`, `bytes_removed`, `indices_optimized`)
plus the three NON-outcomes kept separate on purpose: `refused` is about the dataset's LAYOUT,
`skipped` about this tick's cadence, `trashed` about its governance state — folding them into one
"reason" is what made a shallow clone's silent materialisation invisible.

EVERY outcome is logged, the uneventful one included, because a dataset the sweep looked at and left
alone answers a real question; this module's own docstrings call the alternative *"the 0 that means we
did not look"*. Logged at BOTH returns — the cadence-skip early return bypasses the tail one, and
omitting it would make the record answer only for datasets that ran.

Read it where structured fields actually live, not in `kubectl logs`:

    SELECT log_attributes FROM opentelemetry_logs WHERE body = 'maintenance_dataset_outcome'

**The control-event half needs the same decision §C2 needs**, and should land with it rather than
separately: a new `ControlAction` is a wire contract across three files, and — per `rask-notifications`
— an event that names nobody is undeliverable rather than under-delivered. "Who is told that a table
was compacted?" plausibly answers "nobody, this is an audit record not a notification", which is
exactly why the two halves of this row want different mechanisms: a log/audit line for the sweep, a
control event only if a person should hear it.

**Where.** `routes.py:80`, `sweep.py:486-524`, `endpoints/maintenance.py:39-56`.

### ~~H7 · Every per-warehouse bucket is outside the sweep~~ — **REFUTED 2026-09-08, BY THE TRAP IT WAS ABOUT**
**The claim was wrong and the way it was wrong is the point.** I measured `settings.sweep_buckets`
(`['lance-catalog']`) and `MAINTENANCE_S3_EXTRA_BUCKETS` (empty) against the catalog's 92 registered
warehouse buckets and concluded no governed bucket was maintained. Both readings are accurate. The
conclusion is false.

**`sweep.py::_buckets_to_sweep` ALREADY derives the set from the registry at run time** — item #81, whose
docstring describes this exact leak and closes it: *"``sweep_buckets`` is the primary bucket plus a
static env var — but a per-warehouse bucket is created by an API CALL at runtime, so every tenant
provisioned since the last config edit was invisible."* It reads
`warehouse_records.list_warehouse_records(...)`, takes `maintainable_buckets(...)`, and extends. An
unreadable registry degrades to the configured list and says so.

**Measured where the value LANDS, which is what I failed to do first time.** One sweep tick's own log:

    compaction_bucket_discovered × 93        = 92 warehouses + lance-catalog

The static config is the FLOOR, not the list. `MAINTENANCE_S3_EXTRA_BUCKETS` being empty is correct by
design: the registry supplies the rest, and the env var carries only what the registry cannot know.

**THIS IS THE ESTATE'S OWN PATTERN, AND IT CAUGHT ME.** `open_goal.md` states it in the words that
describe this mistake exactly: *"a control's NAME is not evidence that it exists — and neither is its
CONFIGURATION"*, and *"when a measurement is a COUNT, ask what surface the count could see."* I read two
settings and never asked what the sweep computed from them. Logged here rather than quietly deleted
because a register that shows only its correct findings teaches nothing about how the wrong ones happen.

### H8 · The ingest plane runs as storage ROOT, and four other services with it — **HIGH**
**MEASURED INSIDE THE RUNNING PODS 2026-09-08**, which is the only surface that answers this:

    rask-ingest        AWS_ACCESS_KEY_ID = rustfsadmin     (RustFS ROOT, via envFrom: rask-app)
    rask-maintenance   MAINTENANCE_S3_ACCESS_KEY_ID = rask-maintenance   (scoped, F2-1)

**AND IT IS UNRESTRICTED, PROVEN BY THE CALL THE SCOPED IDENTITY IS DENIED.** Driven from inside each
pod, same endpoint, same operation:

    ingest credential       ListBuckets -> 106 buckets (the whole estate)
    maintenance credential  ListBuckets -> AWS Error ACCESS_DENIED

That is the difference between a scoped identity and the root one, measured rather than inferred from
the key's name.

`rask-ingest`, `rask-compute`, `rask-flows`, `rask-gateway` and `rask-notifications` all take
`envFrom: rask-app`, and that Secret carries the tenant ROOT `AWS_*` pair. **The service that accepts
EXTERNAL, UNTRUSTED BYTES holds the widest storage credential in the estate**, while the maintenance
sweep beside it — which touches only data the estate already owns — runs scoped.

**THIS IS F2-1'S REMAINING SURFACE, AND IT IS BIGGER THAN THAT ROW SAYS.** F2-1 names `rask-catalog` as
"the ONE identity left" after medallion, maintenance and lineage were scoped. That count was taken over
services with a NAMED `*AccessKey` value; these five never had one to be empty, so they were never
counted. A credential inherited through `envFrom` is invisible to a survey of `env:`.

**WHY IT WAS MISSED TWICE, including by me today.** Reading the Deployment's `env` list shows an S3
endpoint and no key, which reads as "this service holds no credential" — the opposite of the truth. The
estate's own rule applies exactly: verify where the value LANDS. `kubectl exec … printenv` answers in
one command what the manifest cannot.

**LANDED IN THE CHART, NOT YET ON THE ESTATE — 2026-09-08, and the distinction is the point.** The
render now withholds the fleet secret from services with no storage code, and
`tests/unit/test_the_root_storage_secret_reaches_only_its_users.py` gates it. The LIVE cluster still
shows all four holding it:

    rask-gateway / rask-notifications / rask-compute / rask-flows   envFrom: rask-config, rask-app

because a chart change reaches pods only through `helm upgrade`, and this estate has SEVEN hand-deployed
images (`kubectl set image`) that a values-mismatched upgrade would revert to chart defaults — the
recorded failure that once put the whole fleet on `:dev` tags. So this half is verified BY RENDER AND
TEST and is not deployed hardening; it wants the owner's next release. The same applies to § H9's ESO
provisioning, which additionally needs `externalSecrets.enabled=true`.

**FOUR OF THE FIVE HOLD IT FOR NOTHING, and that half is safe to fix now.** The shared `rask-app`
Secret carries exactly four keys — `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `HCP_ENDPOINT`,
`HF_TOKEN` — and `gateway`, `notifications`, `compute` and `flows` construct **no S3 client at all**:
zero imports of `lance`, `pyarrow`, `boto3`, `storage` or any `service_kit.lakehouse` storage module
across their sources. The import check is the load-bearing one, not a grep for the variable name:
`object_store` and pyarrow read `AWS_*` AMBIENTLY, so a service that never names the variable can still
sign with it — but a service that never constructs a client cannot. So those four carry an estate-wide
storage credential and an HF token with no code able to use either.

**INGEST IS THE ONE THAT ACTUALLY USES IT**, proven rather than assumed: a pyarrow `S3FileSystem` built
from the pod's own ambient environment listed 106 buckets, and `objectstore.py` states the dependency —
*"a registered store that declares no secret shares the deployment's credentials"*.

**IT IS A DOUBLE VIOLATION, not one.** The credential is both the WIDEST possible and delivered by the
FORBIDDEN mechanism. Owner ruling 2026-09-08, verbatim: *"Never secret through envs. Either from ESO,
secret store dapr and STS for zero trust"* — and `CLAUDE.md` already carries the same rule (*"Secrets
from the Dapr secret store only — never env, never a fallback"*), which `rask-app`'s `AWS_*` pair has
been contradicting for five services.

**Closes it — three sanctioned sources, and which one applies is decided by the consumer, not by
convenience.**

  * **gateway, notifications, compute, flows — NEITHER, they need no storage credential.** They
    construct no S3 client (zero imports of `lance`, `pyarrow`, `boto3`, `storage`, or any
    `service_kit.lakehouse` storage module), so the fix is to stop delivering the storage half of
    `rask-app` to them. Nothing can break, because nothing in them can make the call. This is the
    cheapest real reduction in blast radius available in the estate.
  * **ingest — ITS GOVERNED WRITES ALREADY USE STS, measured on the deployed pod 2026-09-08**, which
    narrows this row's remaining half a long way. Driven inside the running container:

        deployed catalog seam                 CatalogServiceClient
        write_options for a governed write    VENDED (STS), not None

    `RASK_INGEST_USE_CATALOG=true`, and `runtime.write_options_for(...)` hands the worker a
    `VendedCredentialCache` callable, so every governed table write signs with a short-lived
    catalog-vended credential. The ambient root pair is NOT what writes bronze.
    **So what still needs the root credential is the SOURCE-READ path** — and measured 2026-09-08 that
    is narrower again, and is the EASY case for STS rather than the hard one:

        RASK_STORES registered on the deployed pod   NONE (empty)
        ingest READ targets, from the graph          s3://lance-catalog/media-src/batch  126
                                                     source                               24
                                                     s3://images-batch                    15
                                                     s3://acme-bucket                     14
                                                     s3://lance-catalog                    9

    With no store registered, `resolve_source_connection`'s case 2 (a registered external store) is
    DEAD in this deployment and case 3 refuses, so every real read takes case 1 — *"no endpoint
    declared, or one that IS the deployment's own"* — which uses the ambient env chain. The targets
    agree: every one is an ESTATE bucket.

    **So ingest's root credential exists to read estate buckets it was never scoped to**, not to reach
    the outside world. That is the tractable shape: the catalog can vend a READ credential per source
    location exactly as it already vends the write one, and `build_session_policy` already scopes by
    bucket + prefix.

    **AND THE WRITE PATH'S TWO FALLBACKS ARE BOTH REASONED, so this row does not touch them** — read
    2026-09-08 rather than assumed from the `None` return. `write_options_for` degrades to the ambient
    credential in exactly two cases, each documented at the site: the seam **cannot vend**
    (`LocalCatalog`, the no-catalog dev shape, which has no vending door and would RAISE if asked), and
    the chunk **names no namespace** (`workflow.py:327` defaults it empty on purpose — Dapr replays a
    chunk enqueued by the PREVIOUS build verbatim, so a required field would fail every in-flight run
    at the moment of deploy). Neither is a standing production path: the deployed seam is
    `CatalogServiceClient`, and the second is a transient window across one deploy boundary.

    **So ingest's PRODUCTION writes never sign with the root credential, and its only standing use is
    the source read.** Removing the credential therefore needs the read path moved to a vended
    credential and dev mode allowed to refuse — not a rewrite of the write path, which was the shape
    this row implied three measurements ago.
  * **ingest's outbox — STS too, and the machinery already exists.** `catalog.core.vending.build_session_policy`
    scopes an inline session policy by BUCKET + PREFIX (`s3:ListBucket` gated on an `s3:prefix`
    condition, object actions on `bucket/<prefix>/*`) with a 900 s TTL, and ingest already consumes
    vended credentials per dataset (`VendedCredentialCache`). A lineage-outbox credential is the same
    call with the outbox prefix, so § E1's second half needs no new secret at all — which is why the
    static RustFS user first drafted for it was withdrawn: a long-lived key in a chart value is the
    mechanism this row exists to remove.
  * **A pod with no Dapr sidecar — ESO.** The sanctioned k8s-native path, already used by
    `externalSecrets`; the Ray lane and the web zones have no sidecar and cannot call
    `/v1.0/secrets/*`.

**WITHDRAWN, and recorded so it is not re-proposed:** a `rustfs.ingestAccessKey` static user provisioned
by `rustfs-scoped-users.yaml`, mirroring the medallion/maintenance/lineage identities. It is narrower
than root and still a long-lived secret rendered into a chart value, so it trades one violation of the
rule for a smaller one. The three identities that already ship this way are the same debt and are not a
precedent to extend. `rask-ingest` is now declared there (`rustfs.ingestAccessKey`) — but its policy as
written covers only the lineage outbox, which is the § E1 half. **The data half needs measuring first**:
ingest writes table bytes through a credential the catalog VENDS per dataset AND through the ambient
`AWS_*` chain (`objectstore.py` — "a registered store that declares no secret shares the deployment's
credentials"), so narrowing the ambient pair without knowing which writes depend on it would break
ingestion. Measure which paths use ambient credentials, then scope to those plus the outbox.

**NOT a reason to delay the outbox identity**: an additional narrow credential used only for staging is
safe now and independent of the data-path question.

### H9 · 43 secrets reach workloads through env — the estate-wide size of the zero-trust goal — **HIGH**
**MEASURED ON THE LIVE CLUSTER 2026-09-08**, against the running Deployments and StatefulSets rather
than the chart, because `envFrom` is invisible to a survey of `env:` and that error was made twice this
day. Owner ruling: *"Never secret through envs. Either from ESO, secret store dapr and STS for zero
trust"*, and *"Zero trust is the goal"*.

    A) whole Secret via envFrom        6 workloads
       rask-{compute,flows,gateway,ingest,notifications} <- rask-app (RustFS ROOT AWS_* pair)
       rask-greptimedb-standalone      <- rask-observability-s3
    B) secret KEYS via env valueFrom  26 workloads, 43 key references

    APP_API_TOKEN          x10     LINEAGE_SERVICE_TOKEN   x8
    OIDC_CLIENT_SECRET      x7     SESSION_SECRET          x7
    MEDIA_S3_ACCESS_KEY_ID  x3     AWS_ACCESS_KEY_ID/SECRET, S3_SECRET, JWT_SECRET,
                                   POSTGRES_PASSWORD, DAPRSTATE_PASSWORD, OPENFGA_DATASTORE_URI x1 each

**THE COUNT IS NOT THE FINDING — the SORT is**, and each class has a different sanctioned answer, so
"43 violations" would be the wrong summary:

  * **14 belong to the seven web zones** (`OIDC_CLIENT_SECRET` + `SESSION_SECRET`, and a third each).
    Those pods have NO Dapr sidecar and cannot call `/v1.0/secrets/*`, so **ESO** is their path — not a
    violation to be argued away, a migration with a named destination. `ray-lance-head` (2) is the same
    case.
  * **~5 are STORAGE credentials** — `MEDIA_S3_ACCESS_KEY_ID` x3, the `AWS_*` pair, `S3_SECRET`. These
    are the **STS** cases: `vending.build_session_policy` already scopes by bucket + prefix at a 900 s
    TTL, so a long-lived key here is the shape § H8 exists to remove.
    **AND `MEDIA_S3_ACCESS_KEY_ID` IS THE ROOT CREDENTIAL WEARING A SCOPED NAME** — a fourth instance of
    the estate's pattern, found only by following the ref rather than reading the variable:

        rask-annotator  MEDIA_S3_ACCESS_KEY_ID <- secret rask-app / AWS_ACCESS_KEY_ID
        rask-search     MEDIA_S3_ACCESS_KEY_ID <- secret rask-app / AWS_ACCESS_KEY_ID
        rask-viewer     MEDIA_S3_ACCESS_KEY_ID <- secret rask-app / AWS_ACCESS_KEY_ID

    The name says "the media plane's S3 identity"; the value is `rustfsadmin`. A survey by variable
    NAME would classify these as three scoped media credentials and count the estate as closer to zero
    trust than it is. `annotator` and `search` are out of scope by the owner's ruling and are recorded
    rather than worked; `viewer` serves `/api/explorer/*` and is lakehouse-adjacent.
  * **`APP_API_TOKEN` x10 is a BOOTSTRAP credential — and the answer is ESO, not an exception.**
    Measured on `rask-lineage` 2026-09-08: the pod carries `dapr.io/app-token-secret:
    rask-dapr-app-token` AND the app container reads `APP_API_TOKEN` from that same Secret. Both halves
    are real — daprd stamps the token on every call it delivers, and the app must VERIFY it, including
    on the sidecar's very first call. So the app cannot fetch it from the Dapr secret store: the thing
    that would authenticate that fetch is the token itself.
    **That orders the paths, it does not exempt the secret.** ESO exists for exactly this — a workload
    that must hold a credential before, or without, a usable sidecar — and it syncs from OpenBao into
    the k8s Secret both halves already read, so the vault stays the source of truth and nothing about
    the injector wiring changes. Calling this an "accepted exception" (as this row first did) would
    have written off the largest single class in the survey on a premise that only rules out ONE of
    the three sanctioned paths.
  * **`LINEAGE_SERVICE_TOKEN` x8 IS ESO TOO, not a Dapr-store migration** — corrected 2026-09-08 by
    following the `secretKeyRef` instead of the variable name, the same discipline that caught
    `MEDIA_S3_ACCESS_KEY_ID` being root:

        rask-web-{annotator,compute,explorer,home,lakehouse,models,studio}  <- service-token-service-web
        ray-lance-head                                                     <- service-token-service-trainer

    All eight are the seven web zones plus the Ray head — every one a pod with NO Dapr sidecar. The
    variable name reads like a service credential and the holders are all zones. An earlier sort of
    this row put them in the Dapr-store class on exactly that assumption.
  * **The genuine Dapr-store class is what is LEFT after that: about four** — the database passwords,
    `DAPRSTATE_PASSWORD` and `OPENFGA_DATASTORE_URI`. The sidecar-bearing lakehouse services already
    resolve their own secrets through `apply_dapr_secrets` and fail closed, so this class is nearly
    empty rather than the dozen it first looked like.

**SO THE SORT COLLAPSES TO: ~34 ESO, ~5 STS, ~4 Dapr store.** The largest class by far is the one whose
blocker was removed the same day (the auth half above), and the only class with real code work left is
STS — which § H8 measured as smaller still, since ingest's governed writes are already vended.

**AND A REF IS NOT ALWAYS A SOURCE.** Several lakehouse services call `apply_dapr_secrets(settings)` at
boot and fail closed, so their rendered env value is a placeholder the store overwrites — measured
indirectly: a fresh `python -c` inside `rask-maintenance` read an EMPTY `s3_secret_access_key` while the
running app held a working one. **Before migrating any single row here, check whether the store already
wins**; counting refs would otherwise report a service as non-compliant that is already correct.

**THE ESO PATH IS BUILT, DEPLOYED AND SWITCHED OFF — a fifth instance of the estate's pattern**,
measured 2026-09-08:

    external-secrets operator      3 pods, all 1/1 Running
    ExternalSecrets / SecretStores / ClusterSecretStores anywhere in the cluster   ZERO
    chart/templates/external-secrets.yaml   exists, renders a SecretStore + ExternalSecrets
    values externalSecrets.enabled          false

So the operator runs with nothing to reconcile — the same shape as the CNPG operator this file already
records ("an enabled operator toggle is not evidence the resource exists"). **That matters more here
than as a curiosity: ESO is the sanctioned destination for ~26 of the 43 refs** (the ten
`APP_API_TOKEN`s and the sixteen sidecar-less zone/Ray secrets), so the largest class of this row is a
values flip over machinery that already ships, not a thing to build.

**AND ITS PRECONDITION IS CONFIRMED MISSING, which is why the toggle must not simply be flipped.**
ESO's `SecretStore` authenticates to OpenBao with `kubernetesAuthPath: kubernetes` and
`role: lance-infra`. Neither is provisioned: `chart/templates/openbao.yaml`'s seed Job writes the KV
payload to `secret/lance` and authenticates with `BAO_TOKEN = openbao.devToken` (the dev root token),
and **no template or script anywhere in the repo runs `bao auth enable kubernetes`** — grepped across
`chart/templates/*.yaml` and `scripts/*.sh`. So the KV half of what ESO needs exists and the AUTH half
does not.

Enabling `externalSecrets.enabled=true` today would leave every ExternalSecret in `SecretSyncedError`,
which reads as a broken deploy rather than a missing prerequisite — the failure mode that makes this
worth stating before anyone tries it.

**THE AUTH HALF NOW SHIPS — DONE 2026-09-08.** `chart/templates/openbao.yaml`'s seed Job provisions
all three steps, rendered ONLY when `externalSecrets.enabled`:

    bao auth enable -path=kubernetes kubernetes
    bao write auth/kubernetes/config kubernetes_host=...
    bao policy write lance-infra      ->  read on secret/data/lance, and nothing else
    bao write auth/kubernetes/role/lance-infra
        bound_service_account_names=external-secrets
        bound_service_account_namespaces=external-secrets

It lives in that Job because it is the one place already holding the root token AND already writing the
exact path the role must read; a separate bootstrap would duplicate both. **The policy is scoped to one
PATH rather than the mount** — ESO is a controller with STANDING access, so zero trust applies to the
thing fetching the secrets too: read, no list, no wildcard, no write. **The role is BOUND to the
operator's ServiceAccount**, because an unbound Vault role is assumable by any pod in the cluster, which
would inverts the point. Provisioning renders only with the toggle on: a chart that enabled a Vault auth
backend nobody asked for would change the estate's security posture as a side effect of installing it.

Gated by `tests/unit/test_eso_gets_its_auth_half_not_just_its_kv_half.py`, which asserts all four
properties — the backend is enabled, the role is bound, the granted capability set is exactly `{read}`
on a non-wildcard path, and nothing is provisioned when ESO is off.

**What remains for this class is the migration itself**: moving the ten `APP_API_TOKEN` refs and the
sixteen zone/Ray secrets onto ExternalSecrets, which is now a values change plus per-secret wiring
rather than a blocked path.

**Closes it.** A per-class migration in blast-radius order: the storage credentials to **STS** first
(widest authority, and § H8 measured ingest's governed writes as ALREADY vended, so the class is
smaller than it looks), then the sidecar-bearing service tokens to the **Dapr store**, then the
`APP_API_TOKEN` + zone/Ray secrets to **ESO** once its precondition is confirmed.

### ~~H10 · A refusal no retry can clear gated reclamation, so the purge was permanently unreachable — **FIXED AND OBSERVED 2026-09-08** (`ad111621`)~~
**MEASURED LIVE 2026-09-08**, and the constancy is the tell — H2 recorded the same middle number a day
earlier:

    2026-09-07   total 611   incomplete 490
    2026-09-08   total 615   incomplete 490   orphan_files 602

`incomplete` is IDENTICAL across a day in which `total` moved. That is not a backlog draining; it is a
structural count.

**WHY IT CANNOT REACH ZERO.** `scan_datasets` appended to `report.incomplete` for every dataset that
returned `checked=False`, and several of those refusals are PERMANENT rather than transient.
Classifying the live 490 by reason shape settles which:

    419   unsupported manifest reader feature flags: 16 (base_paths (shallow clone / multi-base))
     70   depth limit reached at <prefix> — datasets under it were not scanned
      1   a dataset that could not be opened

**The dominant cause is the SHALLOW CLONE, not the branch** — worth stating plainly because this row
first claimed branches, on the strength of the estate's 114 of them, and the measurement says ZERO of
the 490 are the `tree/` refusal. A branch IS refused for the same permanent reason (its `_versions/`,
`_transactions/`, `_deletions/` and `_indices/` live under `tree/{branch}/` while `lance.dataset(uri)`
opens MAIN), so it would freeze the gate identically — it simply is not what is freezing it today.

Those 419 refusals are the guard doing its job, and the SPEC requires them: `lance_docs/
file_format.md` § Feature Flags says a reader seeing a flag it does not know *"should return an
'unsupported' error on any read or write operation"*, and flag 16 `FLAG_BASE_PATHS` is listed Reader
Required. The flag is written into the MANIFEST, which is why no later tick clears it — only this code
learning base_paths does. So the refusals are RIGHT and the CONSEQUENCE is the defect. **The 70 depth-limit
notes are NOT of that kind**: a dataset nested below the walk's bound was never opened, which is a
real coverage gap the report owes an answer for, and it keeps blocking.

**AND THE CONSEQUENCE IS A GATE THAT CANNOT OPEN.** `purge.report_is_clean` blocks on four conditions
in order, and the third is `if report.incomplete: return "... a partial scan cannot certify the
estate"`. So reclamation is gated on a report that the estate's own supported features guarantee will
never be clean. Trash accumulates forever; nothing is red.

**THIS IS THE SAME SHAPE THE FUNCTION ALREADY FIXED ONCE, one arm over.** Its docstring records the old
blanket skip-pass and the circular reasoning that defended it — *"treating a skip as drift would make
the purge unreachable in every real deployment"* — and the fix was a LEVER (`maintenance.orphanScan`)
that made blocking reachable instead of fatal. The `incomplete` arm has no lever and no equivalent
escape: an operator cannot make a branch scannable.

**Closes it.** Distinguish an incomplete scan that is a COVERAGE GAP from one that is a STRUCTURAL
REFUSAL, exactly as `CategorySkipped.coverage_gap` already distinguishes the two kinds of skip. A
dataset refused because it is a clone, a branch or a manifest the reader will not open was not
"half-scanned" — it was correctly excluded, and the orphan method does not apply to it. Only a scan
that TRIED and failed (an unreadable manifest, a truncated listing, a page ceiling) is a partial
answer that must not certify the estate.

**OBSERVED ON THE LIVE ESTATE**, `ad111621` built by Dagger and deployed as
`lance-rest-catalog:h10-ad111621`, driven through the real `POST /maintenance-reconcile-cron` door:

    before  (e2-791a5f5b, 07:11Z)   total 615   incomplete 490   excluded_datasets: field absent
    after   (h10-ad111621)          total 615   incomplete  71   excluded_datasets 419

The 419 are the whole `base_paths` population and nothing else — 390 at `reader=18`, 29 at
`reader=19`. The 71 that remain are the 70 depth-limit truncations (§ H11) plus the one genuine open
failure, and BOTH still block, which is the half of this change that had to stay true. `total` is
unchanged at 615, so an exclusion moves nothing into or out of the finding count.

**THE PURGE IS STILL BLOCKED, AND THAT IS THE GATE WORKING.** `report_is_clean` refuses on the first
condition — 615 real findings, 602 of them orphan files — which is an operator's actual backlog rather
than a condition the format guarantees can never clear. The defect was an UNSATISFIABLE gate, and the
estate now has a satisfiable one; emptying it is different work.

**FOUR refusal arms set it, and finding the last two is the whole argument for testing near a bug.**
The first pass marked only `_unscannable_reason` structural. Asserting the property at EVERY site that
refuses — `tests/unit/test_orphan_files.py`, one line per arm — turned up two more that would have
kept the gate frozen just as effectively: the manifest-flag refusal pylance raises from the OPEN
(`unsupported_features_from_open_error`, which is how a committed data overlay arrives on today's
pylance), and `_OverlaysPresent`, raised from inside the fragment walk and previously sorted with the
opens that merely failed. Both are permanent; neither went through the gate the first fix guarded.

### H11 · The bucket walk MANUFACTURES coverage gaps in subtrees that hold no datasets — **TWO OF THREE LANDED AND OBSERVED 2026-09-08** (`24483c84`)
**THIS ROW FIRST CLAIMED THE OPPOSITE AND THE ESTATE REFUTED IT.** It read *"70 prefixes of the live
estate are below the discovery depth bound"*, taking the 70 `depth limit reached` notes in § H10's
classification to mean hidden, unmaintained data. Measuring instead of inferring:

    max_depth=3   datasets 30   stopped 49
    max_depth=6   datasets 30   stopped  0
    under models/, walked to depth 8: 0 datasets

**Not one dataset hides below the bound.** Nothing is unmaintained because of depth, and the "raise
the default" instinct the first version of this row carried would have bought nothing. What the 59
stopped prefixes of the primary bucket actually are:

    49   models/<run>/<id>/          model-training artefacts, no `_versions/` anywhere beneath
    10   _lineage_outbox/<ev>/<id>/  control-plane bookkeeping

**THE REAL DEFECT IS THE § H10 SHAPE ONE LAYER UP.** A subtree the walk stops inside becomes an
`IncompleteScan`, and `report_is_clean` blocks on it — so a prefix that will never hold a dataset
blocks reclamation exactly as hard as a manifest we failed to read, and blocks it forever. The walk
cannot tell "no dataset here" from "did not look deep enough", which is why it must not enter subtrees
that are known not to hold data.

**Two of the three are fixed; the third is a decision, not a defect.**

  * **`_lineage_outbox` was the only one of five control prefixes not skipped.** `_warehouses`,
    `_policies`, `_protection` and `_trash` were; the outbox was not, so the walk descended into
    lineage bookkeeping and filed its dead ends as gaps. Now named in `_CONTROL_PREFIXES` with the
    other four. **DONE.**
  * **The walk died on a concurrent delete.** The outbox is drained continuously, so a directory named
    by one listing was gone before the descent — a depth-4 walk died `FileNotFoundError` on one. Both
    callers catch per BUCKET, so one vanished sub-prefix cost a whole bucket its maintenance for that
    tick, reported only as `compaction_bucket_skipped`. Now tolerated BELOW the root and still raised
    AT it, because a missing bucket must stay distinguishable from an empty one. **DONE.**
  * **The 49 `models/` prefixes still block**, and clearing them means exhausting the tree (depth 6
    stops nothing on this bucket). That is now possible — `maintenance.discoveryMaxDepth` exists,
    bounded 1..16 — but the DEFAULT is unchanged at 3 deliberately: the measurement covers ONE of 93
    buckets, and the walk is the sweep's dominant cost (`_protected_roots` opens every discovered
    dataset in every bucket before one is compacted). Moving it is an owner call with a cost profile,
    not a fix to slip into this row.

**OBSERVED ON THE LIVE ESTATE**, deployed as `lance-rest-catalog:h11-24483c84`:

    before (h10-ad111621)   incomplete 71   excluded 419   total 615
    after  (h11-24483c84)   incomplete 61   excluded 419   total 615

Exactly the 10 `_lineage_outbox` dead ends, gone. The depth-limit notes fell 70 -> 60 and the other
counts did not move, which is the shape a skip-list fix should have.

**The sweep was driven through its own door on the new signature** — `planned 440, published 440,
not_queued 0` — with `compaction_bucket_skipped: 0`, so no bucket was lost to the changed walk. That
check was not optional: `_discover_all` gained a keyword-only argument, and four stubs across the suite
still named the old positional shape, where the per-bucket `except Exception` had swallowed the
`TypeError` into an empty result.

**NOT DEPLOYED: the chart half.** `MAINTENANCE_DISCOVERY_MAX_DEPTH` is absent from the running pod —
the image was rolled with `kubectl set image`, not a release, so the lever exists in the chart and the
code default (3) is what runs. It reaches the estate on the next `make k3s-up`.

**STILL OPEN:** the 49 `models/` prefixes. Clearing them means exhausting the tree, which the lever now
permits and the default does not do. Left as an owner decision with its numbers rather than slipped in:
the measurement covers ONE of 93 buckets.

**AND THE LEVER'S JUSTIFICATION CHANGED WITH THE MEASUREMENT.** It was added believing it would reveal
hidden datasets. It does not. It stays because the bound is policy that nothing could set, and because
exhausting the tree is the only way an operator can clear the remaining false gaps — not because
anything is hiding.

**Not blocking today regardless:** `report_is_clean` refuses on the first condition, 615 real findings,
so the depth gaps are not the binding constraint on the purge.

### H13 · 207 of 285 rewrites a tick fall back to the RustFS ROOT key, and the estate says so out loud — **HIGH**
**MEASURED LIVE 2026-09-08** on `c1-ec10c48f`, one sweep tick through `POST /maintenance-cron`:

    credential vend 200 -> SCOPED    78
    credential vend 403 -> AMBIENT  207     "this rewrite is signed by the root key"
    compaction_plan 403             104     "compaction_plane_unavailable_falling_back"

**73% of the estate's rewrites are signed by the storage ROOT credential**, one tick after the other.
The chain is four log lines the pod prints itself:

    POST /v1/table/audx7ns$t1/credentials?tier=write   403 Forbidden
    credential vending unavailable for audx7ns$t1 (403)
    write credential AMBIENT for audx7ns$t1 — nothing vended; this rewrite is signed by the root key
    POST /v1/table/audx7ns$t1/compaction_plan          403 Forbidden

and the catalog names the cause on its own side: `catalog.api.fga_deps — access_denied`. The vend door
checks `can_write_data` for the caller (`credentials.py:64`), and the MAINTENANCE identity does not
hold it on those tables — correctly, because a maintainer is not a writer of the data.

**THIS IS THE ZERO-TRUST GOAL'S OWN THIRD PATH FAILING OPEN.** STS vending is the sanctioned mechanism
for storage, and `credentials.write_options_for` treats a refused vend as a reason to reach for the
deployment's ambient key — "which is what this always used". Under *"never a fallback chain between
them"* that fallback IS the defect: the mechanism is right, the failure mode hands back exactly what
the mechanism exists to replace, and nothing goes red. § H8 is the same root credential seen from the
ingest side; this is its measured cost on the maintenance side.

**AND THE RELATION THAT FIXES IT ALREADY EXISTS, UNDEPLOYED.** `can_maintain` landed in
`packages/service-kit/src/service_kit/governed/auth/model.fga` on 2026-09-08 (46/46 model tests,
309/309 checks) precisely so a maintainer can compact a table it may not read, write, drop or promote —
the test block asserts all four denials. It is not live: the model has not been pushed to OpenFGA, and
the vend door does not consult it.

**Closes it, in three parts, and only the first is mine:**
  1. the write-tier vend accepts `can_maintain` as well as `can_write_data`, so a maintainer can obtain
     the narrow credential its own rewrite needs;
  2. the FGA model is pushed to the store — **owner action**, and without it part 1 cannot be OBSERVED,
     only asserted, which the verification rule forbids;
  3. the `maintainer` tuple exists for the maintenance service identity in the live store.

**Until all three, the honest posture is that the fallback is LOUD rather than silent.** It already
names itself in the log; what it does not do is reach any report, counter or alert, so an operator sees
73% root-signed rewrites only by reading the pod.

### H12 · The scoped credential turns a base-ref "no" into an "unknown", falsely refusing 69 compactions a tick — **NAMED 2026-09-08 (`bddc415b`); THE REMEDY IS AN OWNER CALL**
**MEASURED LIVE 2026-09-08** on `h11-24483c84`, from one sweep tick driven through `POST
/maintenance-cron` (`planned 440, published 440`):

    compaction_base_probe_failed         69   every one on key `models/_versions`, 69 DISTINCT datasets
    maintenance_refused_protected_base  220   of 440 — a DIFFERENT guard, see the conflation note below
    403 Forbidden on /credentials?tier=write  184

The chain is visible in three consecutive log lines:

    credentials — write credential SCOPED for acme-bronze$agnostic
    features    — compaction_base_probe_failed
    OSError: key 'models/_versions' in bucket 'lance-catalog': AWS Error ACCESS_DENIED (HeadObject)
    optimize    — maintenance_refused_protected_base

**BOTH HALVES ARE BEHAVING AS DESIGNED, AND THAT IS THE PROBLEM.** `credentials.py` vends a
TABLE-SCOPED write credential — the STS path the zero-trust goal asks for, working. `dataset_root_probe`
then binds `is_lance_dataset_root` to **the dataset's** storage options and asks about a base path that
lies OUTSIDE that table's prefix. The scoped credential cannot HeadObject it, ever.
`gather_compaction_bases` records the failure as the unknown it is (`probed = None`), and the refusal
ladder treats unknown as refuse — correctly, since rewriting a base a live clone resolves through is
the data-loss shape the whole ladder exists to prevent.

**So the safe answer and the scoped answer compose into a permanent stall.** No retry, policy or grace
window changes it: the credential is scoped by construction and the base path is outside it. Silent —
one WARNING per dataset per tick, nothing red, no report field, and `maintenance_dataset_outcome`
records the refusal as an ordinary outcome.

**A CONFLATION THIS ROW MADE AND THE ESTATE CORRECTED.** It first said "69 of the tick's 220 refusals"
are the denied probes. They are not the same population and not even the same guard:
`maintenance_refused_protected_base` (220) is `base_refs.protected_roots` — the estate-wide pre-pass
that refuses a dataset because ANOTHER dataset resolves its files through it, the clone-SOURCE hazard —
while the denied probes feed the flag-16 ladder in `describe_compaction_unsupported_flags`, which
carries its refusal in the result rather than in a log line of its own. The 220 were never affected by
the credential's scope, and they did not move when it changed.

**THIS IS THE § H10 SHAPE ON THE SWEEP SIDE.** A permanent condition, correctly refused, consumed by a
gate that assumes the condition is transient.

**NOT CAUSED BY THIS SESSION.** `gather_compaction_bases`, `dataset_root_probe` and the credential
vending are untouched by `ad111621` / `24483c84`; the sweep tick that surfaced it was driven to verify
the changed `_discover_all` signature, which reported `compaction_bucket_skipped: 0`.

**MEASURED AGAIN ON THE COMPLETED TICK — 440 of 440 outcomes in, and the refusals are FALSE:**

    compaction_base_probe_failed         69   across 69 DISTINCT correlation ids
    maintenance_refused_protected_base  220   of 440 datasets

Under the MAINTENANCE ROOT credential, the very path the scoped probe was denied is not a dataset root
at all:

    models/_versions                                   NotFound
    is_lance_dataset_root('s3://lance-catalog/models')  False

So the honest answer to the question is **"no, that base is not a dataset root"** — the ladder would
not refuse — and the scoped credential turns that "no" into an "unknown" that refuses. **All 69 are
false refusals, one per distinct dataset per tick, protecting a base that does not exist.** Traced end
to end on `acme-bronze$agnostic` by correlation id; every one of the 69 names the same key.

**The zero-trust scoping is therefore COSTING the estate compaction it should be doing.** That is not
an argument against scoping — the scoped credential is the right mechanism and the goal's own third
path — it is an argument that the scope is drawn without reference to the questions maintenance must
ask about the table it was issued for.

**Closes it.** A probe that was DENIED is not evidence about the base; it is evidence the maintainer is
under-scoped for a question it is REQUIRED to ask before rewriting. Treating denied as "not protected"
is not available — a real base would then be rewritten, which is the data-loss shape the ladder exists
to prevent. So the scope must cover the question: `vending.build_session_policy(bucket, prefix, tier)`
grants by bucket + prefix, and a table's DECLARED base paths belong in that grant, because reading them
is part of maintaining that table. The catalog knows them at vend time — it holds the manifest.

**THE HALF THAT WAS MINE TO FIX LANDED AND IS OBSERVED.** A denial and an outage refused alike AND
read alike — both rendered "could not be read in object storage", sending an operator to inspect a
store that is working. `BaseEvidence.probe_denied` now separates them, the warning carries the flag so
the population is countable rather than inferred from a message, and the refusal says where the fix is.
Verified on the deployed `lance-rest-catalog:h12-bddc415b` against the estate's OWN error string:

    recognised as a denial: True        (the live "AWS Error ACCESS_DENIED during HeadObject" text)
    an ordinary failure:    False

    DENIED -> ... this maintainer is not permitted to read the base at s3://lance-catalog/models ...
              The credential's scope excludes the base, so this refuses on every tick until the scope covers it
    BROKEN -> ... the base at s3://lance-catalog/models could not be read in object storage ...

**THE VERDICT IS DELIBERATELY UNCHANGED.** `_base_paths_compaction_refusal` fails closed with its
asymmetry measured — a wrong permit destroys the reason a clone exists — and a denial is no more
evidence about the base than an outage is. Only the instruction changed.

**THE REMEDY LANDED VIA § C1 AND THE PROBE NOW ANSWERS.** `lance-rest-catalog:c1-ec10c48f` deployed to
BOTH `rask-catalog` (which vends) and `rask-maintenance` (which asks), then a sweep driven through its
own door:

    before (h12-bddc415b)   probe_failed 69   outcomes 440
    after  (c1-ec10c48f)    probe_failed  0   outcomes 440

**WHAT THAT PROVES, AND WHAT IT DOES NOT.** It proves the vended credential can now read the bases its
table declares — the question the maintainer is required to ask before rewriting is answerable again.
It does NOT prove 69 compactions now proceed: the flag-16 ladder weighs three readings and the probe is
one, `compact_refusal` is carried in the result rather than logged, and the 220 protected-root refusals
are a separate guard that did not move. Proving the compactions would mean reading the per-dataset
results, and it is not claimed here on the strength of a probe count.

**THE REMEDY IS § C1's REMAINING CLAUSE, and it is narrower than "widen the scope".** C1 already
records the direction for exactly this collision — its own docstring states the stake, *"the STS session
policy is scoped to the primary root bucket only, so a data-base fragment would be denied at the object
store"* — and closes with: *vend the union of `base_paths` with per-base rights (read on inherited
bases, write on `target_bases`, never on reference-only bases)*. READ on an inherited base is precisely
what this probe needs, and per-base rights are stricter than the blanket widening this row first
imagined: the credential comes to match the table's actual extent, least privilege per base, rather
than growing a prefix.

So H12 does not need a scope decision of its own — it needs C1 finished, and it is the sharpest
measured reason to finish it. The other direction, treating denied as "not protected", is not
available at any price.

**Still open, and smaller than it looked:** whether the 184 `403 Forbidden` credential vends are this
population or a separate refusal. One names `trackansdba60663$read_ghost`, which reads like a table
that SHOULD be refused, so this may be correct behaviour rather than a second defect.

### H6 · Purge deletes any sub-prefix a trash record names — **THE DATASET CHECK LANDED 2026-09-07**
**"Verify the location is a Lance root before `delete_dir`" — DONE.** The refusal ladder in `check`
already bounded WHERE a location may point (inside the maintained estate, not a store root, not
crossing a control prefix) and never asked WHAT is there. `delete_location` now refuses a location
holding files but no `_versions/` marker, with `NotADatasetRootError` surfaced as a `RefusedRecord`
the way the protected-base refusal already is — its own arm and its own log body, because the two say
different things to an operator: one means "a live clone needs these bytes", this one means "this
record points at something that is not a dataset", and only the second says the RECORD is wrong.

**The marker is `_versions/`, the same string `discover_datasets` decides discovery by** — one
definition of what a dataset is rather than a second opinion — and it costs nothing: the recursive
listing `delete_location` already performs to sum reclaimed bytes carries it.

**An empty or absent path stays idempotent success, deliberately.** A crash between the delete and the
record clear re-runs the whole record, so a half-completed purge must be able to finish; refusing
there would strand every interrupted one. Only a location that HOLDS something unrecognisable is
refused. All four cases pinned by
`tests/unit/test_purge_refuses_a_location_that_is_not_a_dataset.py`, against a real `file://` store.

**On the happy path this never fires** — a trash record's location comes from `describe_table` at drop
time. It is for the paths where it does not: a corrupted or hand-edited record, a location reused
after a rename, a bug upstream writing the wrong string. In each the blast radius is a recursive delete
of live data that no other refusal in the ladder can see.

**STILL OPEN:** the second clause — a maintenance identity scoped per warehouse rather than a root key
everywhere (C4 / §F2-1). **Where.** `maintenance.yaml:123`, `values.yaml:1517`.

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
| Medium | **A stage runner row still carries `stageJob`/`ray_entrypoint`/`ray_job_params` beside the declaration that supersedes them** — two sources of truth for what a lane runs, with `engine_choice` arbitrating. Not removable before there is a seeding path: without one, the default deploy could run no cascade at all. It dies with the row above | Same |
| ~~High~~ **CLOSED 2026-09-04** | ~~No cascade reconciler and no re-run verb.~~ Both exist: the medallion carries a `bindings.cron` reconciler and `POST /api/stage-runners/stages/rerun` re-drives ONE edge on that edge's own rung. Driving C3 in-cluster found it had NEVER worked — a seven-layer chain from a 404 route to a missing credential — of which six layers are fixed and the seventh is `dedicatedServiceCredentials: false`, i.e. row 35 (B) below rather than this row. **Reasoning moved to `docs/DECISIONS.md`, "Cascade repair"; docs/DECISIONS.md "Cascade repair" deleted** | `open_estate-verification.md` row 35 (D), closed with it; row 35 (B) carries the remainder |
| High | **No Dapr Workflow versioning seam** — two replay divergences already shipped; "drain before deploying" is the only safe answer | K sequences the retreat; this is the cost of staying meanwhile |
| Medium | Submission bypasses the `RayJob` CRD, so Kueue admits nothing | |
| ~~High~~ **CLOSED 2026-09-04, PROVEN LIVE** | ~~Maintenance compaction runs in a 512Mi pod while the distributed seam has no executor.~~ M1 split the planner from the workers; M2 consumes the protocol. Verified in-cluster on a Dagger-built image: the planner published 21 units and two dedicated workers consumed 7 and 8 as competing consumers, each vending its own per-table credential — so the bytes were moved by something other than `rask-maintenance`. The protocol itself: `{"read_version":6,"tasks_planned":1,"tasks_executed":1,"tasks_failed":0,"version":8,"fragments_added":1,"fragments_removed":6}`, 300 rows intact, signed by key `536H5FARWTW3GAZV5KOK` where maintenance's own is `rask-maintenance`. It DEGRADES rather than fails and `DatasetResult.compaction_mode` counts it. **M3 (BYO compute via `RayJob` + Kueue) is DEFERRED, behind docs/DECISIONS.md "The compute plane is decoupled" (§7.4) steps 3-4** — the same body of work, not a second one | docs/DECISIONS.md "Cascade repair" deleted 2026-09-04; its live measurements live in the commit that closed it |
| Medium | 1,367 orphan rows in `daprstate`, no TTL, no alert | |
| Medium | The workflow status metric reports success on a dying path | |
| Owner | Ray GCS is not fault-tolerant: a head restart kills in-flight jobs. The platform now degrades in one poll interval instead of 24 h (row 34), but fault tolerance itself needs an external Redis, which this estate refuses by standing rule | |

### O3 · Blocked on the owner

`dedicatedServiceCredentials` — the CHART DEFAULT is `false` (`values.yaml:807`), under which every
stage runner holds `owner` on every warehouse and the bounding control (`LANCE_PRIVILEGED_SUBJECTS`) is
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
| silently-weaker | `GET /stage runners/{stage runner}/stages/{instance_id}` and its POST | `stage runner` + `instance_id` — the wrong stage runner answers |
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
| Q2-5 | The stage runners still write as the RustFS tenant root | 30, CLOSED (partial by design) | The cascade's writes are AUTHORIZED (the stage runner asks `POST /v1/table/{id}/credentials?tier=write`, `can_write_data` audited) and not SCOPED: the credential vended is the tenant root's. `rask-maintenance` and `rask-ray-compute` are provisioned and scoped (rows 31/32 and `5c11002c`); the stage runners are the remaining holder. **Needs an owner ruling** — a scoped stage runner credential must still reach the outbox and `HeadBucket`, which is what row 32 measured as the blocker for the Ray key |
| Q2-6 | `LANCE_FGA_CASCADE_WRITERS` grants every stage runner `owner` on every warehouse | 35 (B) | The bounding control (`LANCE_PRIVILEGED_SUBJECTS`) now renders on catalog AND lineage (`79512bb0` closed the door asymmetry), but the grant itself is still estate-wide: a stage runner holds `can_drop`, `can_deregister`, `can_restore` and `manage_grants` on every tenant's warehouse. **Needs an owner ruling** on whether the cascade writer's grant narrows to the warehouses it actually writes |

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
| ~~Q3-2~~ | `DUP-15` **OPEN** | med | The Dapr-workflow scheduler is written twice and the copies' timeouts disagree — **REFUTED — measured at HEAD 2026-09-07.** The claim is that the copies' timeouts disagree. They cannot: `StageJobSpec` (`workflow.py:166-167`) and `TrainJobSpec` (`879-880`) both default `poll_interval_seconds` / `max_polls` from the SAME module constants, `POLL_INTERVAL_SECONDS = 30` and `MAX_POLLS = 2880`. One pair of numbers, one place |
| ~~Q3-3~~ | `VS-07` | med | Five silent swallows in the search path render real failures as empty results — **CLOSED — verified at HEAD 2026-09-06 (C3).** `1f770a5d` (2026-08-31) closed the last of the five swallows. No `except Exception: pass` and no `except Exception: return []` remains anywhere in `services/search/src/`; every cited site now logs or re-raises |
| ~~Q3-4~~ | `PS-02` | med | `storage`'s error taxonomy is half-applied — `s3_errors` wraps nothing inside the package — **REFUTED — measured at HEAD 2026-09-07.** The claim is that `s3_errors` "wraps nothing inside the package". It wraps everything: `s3.py` carries three `with s3_errors(...)` blocks around its `get_object`/`put_object`/client construction and `errors.py` a fourth around its own `head_object`, and `s3.py`'s module docstring states the rule it follows ("Every S3 call is wrapped in `storage.errors.s3_errors`"). The taxonomy is applied where the row says it is absent |
| ~~Q3-5~~ | `MAINT-08` | med | `reconcile()`'s `control_root` falls back to the POLICY root, not the control root — **CLOSED — verified at HEAD 2026-09-06 (C3).** `e4e73b68` (2026-08-16) replaced `control_root or settings.resolved_policy_root`, the exact expression audited at `reconcile.py:701`. Two doc-only residues survive and are worth a separate low row, neither of which is the defect |
| ~~Q3-6~~ | `ingest-flow-06` | med | `park_poison` publishes unguarded — one bad unit fails the whole run when the DLQ is down — **CLOSED 2026-09-07.** Confirmed as written and fixed RED-first. All three of the worker's parking paths awaited the DLQ publish BEFORE `msg.ack()` and it was unwrapped, so any publish failure — stream absent, broker briefly gone, a `limits` stream at its ceiling — raised out of the drain task, left the unit unacked and hung the chunk that was supposed to complete WITH ERRORS. `ensure_dlq_stream` narrowed that window and could not close it: it runs once at drain start, the publish happens later. `park_poison` now ANSWERS (`bool`) instead of raising, and the callers write the run's own record first — the DLQ copy is evidence, `outcome.errors` is what the publish precondition reads — so a failed park changes the TEXT of the record and never whether there is one. Two doc claims this falsified were rewritten rather than annotated |
| ~~Q3-7~~ | `catalog-api-07` | low | `_collect_descendants` recurses with no depth cap and no cycle guard — **CLOSED — verified at HEAD 2026-09-06 (C3).** `3357f7dd` (2026-08-15). `_collect_descendants` now carries the depth cap its sibling enumerator always had |
| ~~Q3-8~~ | `catalog-api-06` | low | Three tuple write/revoke sites bypass the `seed_ownership` seam — **REFUTED — measured at HEAD 2026-09-07, and the two survivors are DELIBERATE.** Three bare `seed_ownership(` call sites remain and one of them is inside `seed_ownership_or_compensate` itself, which is the seam. The other two are reasoned choices the code argues in place: `versions.py:178` (batch commit) CANNOT compensate — the tables already exist, so retrying repairs nothing — and instead collects the stranded ids and raises a 503 naming every one of them; `tables.py:909` (rename) seeds BEFORE revoking on purpose, and its comment weighs the two failure modes (a stale grant on a dead id, versus an object with no owner that nobody can touch) and picks the recoverable one. The compensating seam has 7 call sites where compensation is possible. This is not a bypass, it is a boundary |
| ~~Q3-9~~ | `ING-14` | med | The A8 provenance check fetches the entire unbounded `/runs` board — **CLOSED 2026-09-07 (`935b0db6`), built with Dagger, both images rolled, driven live.** The row's own subject caught a REGRESSION introduced two commits earlier: bounding `/runs` (`65fd0e24`) made the whole-board scan not merely slow but WRONG — a run older than the newest page is absent from the response while present in the graph, so A8 would report a provenance defect that does not exist, silently and only for older runs. Replaced by a governed POINT READ, `GET /runs/{run_id}`, answered from `MATCH (r:Run {run_id:$rid})`. **Live on the deployed release**: a visible run answers 200 in 512 bytes / 0.084 s where the board cost 2,652,260 bytes / 1.4 s (5,180x smaller, 17x faster); **a visible run OUTSIDE the newest page answers 200** — the regression case, proven with a run confirmed absent from the board; a run the caller may not see answers 404, the SAME as one that is absent, so ids cannot be enumerated by watching which refuse differently; unauthenticated 401. `RUN_BY_ID` is built from `LIST_RUNS`' body and both reads share one row->`RunStatus` mapping, so a column added to the board arrives in the point read too |
| ~~Q3-10~~ | `F-LIN-04` | med | `list_runs`/`list_datasets`/`list_jobs` are fetch-all with no server-side LIMIT — **CLOSED 2026-09-07 (`65fd0e24`), built with Dagger, deployed and re-measured live.** The code had PREDICTED this with a date: `cypher.py` called the unbounded shape "currently fine — the graph's node count is modest, and `/runs` measured 272 rows on the live estate 2026-08-23", while warning it "is a property of the data, not of the code". Fifteen days later the same endpoint answered **5,122 runs / 2,652,260 bytes**, on a board its own docstring says is polled every two seconds, against a graph with no run retention. A LIMIT IN THE HANDLER WOULD HAVE CHANGED NOTHING — the endpoint accepted `?limit=1` and returned all 5,122 rows (FastAPI drops an undeclared query arg), and one run cost 2.5 s where a hundred cost 1.4 s, because the cost is the READ. So the bound reaches the Cypher (`list_runs_page`, newest-first on `event_time`), with `/events`' over-fetch window so governance — which drops rows AFTER the read — cannot starve a page. **Live after the roll: 200 runs / 102,158 bytes / 0.84 s by default (26x smaller), 5 runs / 2,527 bytes for `?limit=5` (1050x), 401 unauthenticated and 422 above the cap.** `/jobs` (2,429 nodes) and `/namespaces` keep the unbounded shape and are the same growth curve — recorded in `cypher.py` rather than left implied |
| ~~Q3-11~~ | `ANN-14` | med | Publish transport builds a fresh httpx connection per call, retries one error class — **STRUCK OUT OF SCOPE 2026-09-07 (owner ruling).** *"prio lakehouse and ignore other zones that are not the lakehouse or compute. I.e search, flows and model training and annotator should be ignored."* This row belongs to the ANNOTATOR, so it is not work this register carries. Struck rather than deleted, because a row that vanishes reads as done |
| Q3-12 | `DUP-14` | med | The same hand-rolled HTTP backoff loop in `packages/storage` and `services/ingest` |
| Q3-13 | `DUP-10` | med | Two OpenLineage kernels and four `RunEvent` builders |
| Q3-14 | `DUP-19` | low | Three hand-rolled `storage_options` builders bypass `lance_storage_options` |
| ~~Q3-15~~ | `DUP-21` | low | Seven outbound HTTP sites build a fresh httpx client per call — **CLOSED — already fixed, and the code CITES THIS ROW BY NAME.** `medallion/workflow.py:531` reads "THE POOLED CLIENT (DUP-21), not a fresh one per tick", with the reason measured: that activity runs on every polling tick of every running stage, so a per-tick client paid a TCP connect, a TLS handshake and a pool teardown each time. Counted at HEAD across the in-scope planes: **10 httpx client constructions, and no hot path builds one per call.** The remaining per-call sites are correct — `catalog_register` builds one only when no client is INJECTED (its docstring argues the fallback: tests and `scripts/` have no lifespan), OIDC discovery is a per-issuer startup fetch, and `rayjob_executor` is the dead adapter of Q17-2 |
| ~~Q3-16~~ | `MED-008` | med | Every outbound call builds its own httpx client — one pool per call — **CLOSED — already fixed, same seam as Q3-15.** `ray_submit.ray_client()` is a module singleton behind a double-checked `asyncio.Lock`, guarded on the Ray address so a re-pointed cluster rebuilds rather than serving the old host, with an idempotent `close_ray_client()` the stage runner's lifespan calls. The medallion's other outbound clients are lifespan-owned (`stage_runner.py:67`) |
| ~~Q3-17~~ | `SK-03` | med | A fresh urllib3 `ApiClient` per catalog read/write, never disposed — **CLOSED — already fixed, and the fix carries a correctness point the row never mentioned.** `catalog_api_client` is `@cache`d on (base URL, retry policy) — the only two things that shape the urllib3 pool — so the generated `ApiClient` and its `PoolManager` are built once per catalog rather than per `open_reader`/`open_writer`. **Deliberately NOT keyed on the bearer**, which is the load-bearing half: a shared client carrying one caller's `Authorization` as a default header would answer the NEXT caller's read under the previous identity, and the catalog authorizes on the bearer — so the token rides each request via `request_headers`. The row's disposal half is also answered: the generated `ApiClient.__exit__` is `pass`, so a `with` block released nothing; a cached pool that outlives every caller is the fix rather than a close that never worked |
| ~~Q3-18~~ | `SKG-07` | med | `make_client` returns an aiohttp-backed `OpenFgaClient` with no disposal contract — **CLOSED — verified at HEAD 2026-09-07.** Every in-scope service disposes it in its lifespan: catalog (`main.py:252`, `await fga_client.close()`, isolated so one failing teardown cannot strand the other resource), lineage, medallion and maintenance close it directly, and ingest goes through `fga.dispose(app)` — None-safe and suppress-wrapped — pinned by `test_lifespan_closes_the_fga_client_on_shutdown`. The contract the row wanted exists and is asserted |
| ~~Q3-19~~ | `SKG-11` | med | Module-level mutable cache in `warehouse_registry` with no bound and no eviction — **CLOSED — already fixed, verified at HEAD 2026-09-07.** The row reads "module-level mutable cache with no bound and no eviction". `warehouse_registry.py` holds a `_TtlCache(MAX_CACHE_ENTRIES)` with `MAX_CACHE_ENTRIES = 512`, per-entry expiry and oldest-first eviction once full, and the module docstring says so in its opening paragraph. Possibly the row § Q12-1 could not measure |
| ~~Q3-20~~ | `MAINT-07` | med | `reconcile()` builds a boto3 client per call inside an `async def` — **CLOSED — verified at HEAD 2026-09-07 by tracing to the DEPLOYED call site, not to the function.** The row says `reconcile()` builds a boto3 client per call inside an `async def`. It can, and on the deployed path it does not: `on_reconcile_cron` takes `bucket_client: S3ClientDep` — the client `service.py:162` builds ONCE at startup into `app.state.s3_client` — and passes it through (`routes.py:159`). The in-function construction is the no-app fallback, the same documented shape as `catalog_register`'s (Q3-15), and it goes through `packages/storage`'s wrapper with the sweep's OWN credentials rather than the ambient env chain, because this process may address a different backend than the host's |
| ~~Q3-21~~ | `MAINT-12` | med | The multi-base gate issues one sequential S3 HEAD per referenced path — **MEASURED AND DEPRIORITISED 2026-09-07, with the number recorded so nobody re-derives it.** The row's wording is off — it is one sequential MANIFEST OPEN per DATASET (`protected_roots`, `for uri in dataset_uris`), not an S3 HEAD per referenced path. The substance holds: the opens are independent and run in series over every dataset the sweep discovered. **Driven on the live estate: the whole maintenance tick answers in 7.47 s, planning 323 units**, so the pre-pass is not a cost problem at this size and parallelising it would trade a thread-safety question about the shared `lance.Session` for time nobody is waiting on. **THE CAVEAT IS THE POINT, and it is the shape that just cost this estate two silent defects (Q8-18): 7.47 s is a property of the DATA, not of the code.** Re-open on the measurement rather than on the shape — if a tick approaches the cron period, or the discovered-dataset count climbs the way the run board's did (272 -> 5,122 in fifteen days), the pre-pass is where the time went |
| ~~Q3-22~~ | `CAT-CORE-09` | med | Each mutating table op performs three namespace describes plus three dataset opens — **PARTIALLY FIXED and RE-SIZED 2026-09-07.** The row's count does not survive reading the code: across the catalog's service layer there are 7 `describe_*` references and several are prose, not three-per-op. **What IS real and is now fixed: `commit_compaction` re-opened the dataset from the URI purely to read `.version` after committing** — a second full manifest read over the object store, on the catalog's WRITE path, for a number already in memory. **Measured against pylance rather than assumed, because the answer decides whether this is a fix or a behaviour change**: a dataset at version 4, compacted, reports **6 on the same handle with no reopen**, and `checkout_latest()` leaves it at 6 — `Compaction.commit` advances the handle in place (`lance_docs/lance_sdk.md` documents `checkout_latest` as the in-place update; the probe showed even that is unnecessary here). Gated by `test_the_commit_opens_the_dataset_ONCE`, which counts opens rather than trusting the shape. The remaining describes are one per operation on paths that need them; re-open with a COUNT taken from a live trace rather than from a grep |
| ~~Q3-23~~ | `VS-16` | med | Voice similarity issues one Lance scan per hit (N+1) and a fresh executor — **STRUCK OUT OF SCOPE 2026-09-07 (owner ruling).** *"prio lakehouse and ignore other zones that are not the lakehouse or compute. I.e search, flows and model training and annotator should be ignored."* This row belongs to SEARCH (voice similarity), so it is not work this register carries. Struck rather than deleted, because a row that vanishes reads as done |
| ~~Q3-24~~ | `SKG-10` | med | Five direct `os.environ` reads outside any Settings class — **REFUTED — measured at HEAD 2026-09-07, and the COMPOSITION is the finding.** A grep over the six in-scope planes returns 34 matches; stripping comments and docstrings by walking the AST leaves **13 real code reads**, the same prose-is-not-a-reader correction the unread-env gate was built for. Of those 13, **NINE belong to another system's vocabulary and are correctly read directly**: `OTEL_EXPORTER_OTLP_ENDPOINT` x4 and `OTEL_SERVICE_NAME` x2 are the OpenTelemetry SDK's own spec-defined variables — binding them to a rask Settings field would create a SECOND spelling of a variable the SDK itself reads — and `DAPR_HTTP_PORT` x2 plus `APP_API_TOKEN` are INJECTED by the sidecar, so they are Dapr's contract rather than rask's config. Of the four rask-named reads, one (`MEDALLION_{tier}_NAMESPACE`) is built from the tier at call time and cannot be a static field at all; `RASK_LOG_LEVEL` configures the ROOT logger before any settings object exists, which is the reason it is a bare read and its docstring says so; `RASK_STORES` and the warehouse-registry TTL are parameters with an env DEFAULT (`raw if raw is not None else os.environ.get(...)`), not settings a service reads behind its own config. **"Outside any Settings class" is not by itself a defect** — the question is whose vocabulary the name belongs to |
| ~~Q3-25~~ | `SK-14` | low | `RASK_*` read directly via `os.environ` outside the settings modules — **REFUTED — same measurement as Q3-24**, which counted this row's subject too: 13 real code reads across the in-scope planes, nine of them another system's variables (OTel's six, Dapr's three) that must be read directly, and none of the four rask-named ones a setting a service reads behind its own config |
| ~~Q3-26~~ | `F-LIN-08` | med | Route topology decided at import time by settings-conditional module-level branches — **LARGELY DONE — verified at HEAD 2026-09-07, and the load-bearing half was fixed with this row's own reasoning.** Three import-time branches remain in `lineage/main.py` and they are not equivalent. The one that matters — the reconcile cron route — was EXTRACTED into `mount_reconcile_cron(target, binding_name)`, and its docstring says why in the row's own terms: "a named function (not inline module-level wiring) so the unit tier can drive the PRODUCTION mount decision both ways — the audit's gap was the route being tested only on a synthetic app, leaving this gate itself unpinned". It is now pinned by `test_mount_reconcile_cron_production_gate`, driven both ways. What is left is a demo peek (`LINEAGE_DEMO_DATA_ENABLED`, default **False**, so the router does not exist on a shipped install) and a `_STATIC.is_dir()` filesystem check that reads no setting at all. Re-open only if a branch appears that decides a GOVERNED route |
| ~~Q3-27~~ | `X8` | med | The four `make_service_app` services expose liveness only; the chart points readiness at it — **CLOSED — verified at HEAD 2026-09-06 (C3).** `b0d984f6` (2026-08-27). Both halves of the claim are false now: `make_service_app` root-mounts the drain-aware `/livez`+`/readyz` pair for every app, and the chart points the readiness probe at `/readyz` |
| Q3-28 | `MED-014` | low | Both app entrypoints read settings and configure logging at import time |
| Q3-29 | `ING-18` | low | Query-parameter clamping done by hand instead of declared |
| Q3-30 | `ingest-flow-16` | low | Generator workflows annotated as returning their final value |
| ~~Q3-31~~ | `ANN-07` | med | Half the annotator routes return bare `dict[str, Any]` — raw actor documents reach clients — **STRUCK OUT OF SCOPE 2026-09-07 (owner ruling).** *"prio lakehouse and ignore other zones that are not the lakehouse or compute. I.e search, flows and model training and annotator should be ignored."* This row belongs to the ANNOTATOR, so it is not work this register carries. Struck rather than deleted, because a row that vanishes reads as done |
| ~~Q3-32~~ | `VS-18` | med | Ten routes return bare `dict`/`list[dict]`, losing the response contract — **STRUCK OUT OF SCOPE 2026-09-07 (owner ruling).** *"prio lakehouse and ignore other zones that are not the lakehouse or compute. I.e search, flows and model training and annotator should be ignored."* This row belongs to SEARCH, so it is not work this register carries. Struck rather than deleted, because a row that vanishes reads as done |
| Q3-33 | `SKG-09` | med | Every lakehouse control-plane record is an unvalidated `dict[str, Any]` |
| Q3-34 | `F-LIN-07` | med | Domain values cross models→repository as untyped dicts and positional tuples |
| Q3-35 | `CAT-CORE-08` | low | Service functions return `dict[str, Any]` that endpoints splat into models |
| Q3-36 | `MED-013` | low | Two handler seams typed `Any` with an ANN401 suppression |
| Q3-37 | `SKG-14` | med | The audited scope sits under a blanket 21-rule ruff exemption (5 lines still in `pyproject.toml`) |
| Q3-38 | `PS-15` | med | `ray_kit.submit` — deterministic ids and the reattach branch — has no test |
| ~~Q3-39~~ | `MED-002` | low | `transform.py`'s process-wide `_write_lock` is still acquired BLOCKING — **REFUTED — measured at HEAD 2026-09-07.** The row says the process-wide `_write_lock` is acquired BLOCKING. It is an `asyncio.Lock()` taken with `async with _write_lock:` (`transform.py:90,955`), which yields to the event loop rather than holding it, and the blocking work INSIDE the lock is correctly off-loop (`await run_in_threadpool(read_upstream, ...)`). No `threading.Lock` and no bare `.acquire()` exists in the module. The lock's own comment states what it is for: it single-flights a redelivery of the same stage against itself so two overwrites cannot race on one target dataset |

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
| Q5-2 | Maintenance cannot compact ANY tenant's tables, and the RUNG may be the defect rather than the grant | Refused live: `credentials?tier=write` -> **403**, `compaction_plan` -> **403**. Established 2026-09-05 that this is not about runtime-minted tenants at all — `service-maintenance` is absent from `LANCE_FGA_CASCADE_WRITERS`, so it holds nothing on ANY warehouse. **The question is which rung, and the model has no good answer.** Vending needs `can_write_data` (= `writer`); the compaction door is gated on `can_drop` (= `owner`), so admitting maintenance the ordinary way hands it `can_deregister` and `manage_grants` on every tenant — the same over-grant Q2-6 flags for the stage runners. A COMPACTION DROPS NOTHING, and the model has no `can_compact`/`can_maintain` relation to gate it on (grepped: none exists). So: (a) add a maintenance rung to the model and gate the compaction doors on it — most work, least privilege; (b) grant `writer` and re-gate `compaction_plan` off `can_drop`; (c) grant `owner` and accept the breadth. **OWNER RULING** — (a) is the honest design and it changes the authorization model |
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

## Q7. What deleting a register leaves behind (2026-09-06)

Found while answering "why is `open_python-audit.findings.json` still here". It was: the drain of
2026-09-05 deleted `open_python-audit.md` and repointed 98 citations, and left its 400 KB
machine-readable sidecar — the same audit, generated 2026-08-07, a month stale — sitting beside the
ledger it indexed. Deleted here, its one live finding carried into `open_projects.md`.

Checking for siblings turned up that this is a PATTERN, not an oversight: `tests/unit/test_no_locator_names_a_deleted_register.py`
now gates it, and shipped RED against 18 pointers into five registers already gone. Three were the
drain's own (`pyproject.toml`, `Makefile`, `deploy/ray-lance-demo.yaml` — missed because the repoint
walked seven source roots and those three are outside all of them); fifteen are older.

| # | Finding | Sev | What remains |
| --- | --- | --- | --- |
| ~~Q7-1~~ | `open_python-audit.findings.json` outlived the ledger it indexed | med | **CLOSED HERE.** `git rm`'d; the `1 + N` projects-list finding it uniquely held is now stated in `open_projects.md` § 3.6 in its own words |
| ~~Q7-2~~ | Three citations of the drained ledger dangled | med | **CLOSED HERE.** `pyproject.toml` X3, `Makefile` P0, `deploy/ray-lance-demo.yaml` P0 repointed at `docs/DECISIONS.md` "The Python estate audit". The row ids are dropped, not carried: that section defines no `X3`, and a pointer to a label nothing defines is the defect `test_every_decisions_citation_resolves` was written for |
| ~~Q7-3~~ | `DECISIONS.md` claimed the ledger lived inside `DECISIONS.md` | low | **CLOSED HERE.** The mechanical repoint rewrote the filename inside the one sentence that was *about* the filename. Restored, with the deleting commit named |
| ~~Q7-4~~ | 15 pointers into four registers retired 2026-08-04…08-26 still dangle | med | **CLOSED 2026-09-06.** All 15 repointed across 21 sites (`OPEN-WORK.md` carries two). At 18 the reasoning was already inline, so the fix was dropping a dead id; three took the retiring commit (`c6c23407`, `d25297d1`) and one took `docs/OPERATORS.md` § 4 — NOT DECISIONS.md, which has no such section. Four orphaned fragments (`(P1 #8)`, `#7`, `§ D`, `S4's`) went with them. `_CARRIED` is empty. Two corrections came out of reading the deleted registers: `open_ingest.md` had two generations, and `P1` was never a row id — it abbreviates "Phase 1" |
| Q7-5 | Nothing gates the SIDECAR of a register, only the register | low | The new gate checks pointers INTO a file. It would not have caught a `.findings.json` that nobody cited — that one was found by a reader asking why a file was still there |

## Q8. What the live e2e suite was skipping (2026-09-06)

C5 was closed on 63 passed / 10 failed / 40 skipped. The failures became § Q4. The skips became
nothing — and a skip verifies nothing, so a third of the suite was uncounted while "C5 green" rested
on it. `b2d0933a` closed the runner's half: 43 skips become 10, and 14 legs that had never run now
pass. These are the legs that had never run and DO NOT pass.

Not one of them is a new regression. They are newly VISIBLE, and every one of them was invisible for
as long as the runner withheld the variable its suite asked for.

MEASURED, without `test_user_state_e2e` (which deletes the catalog pod mid-run and makes everything
after it fail for a reason that is not its own):
`19 failed, 70 passed, 15 skipped, 1 xfailed, 6 errors in 98s`.

| # | Finding | Sev | What remains |
| --- | --- | --- | --- |
| ~~Q8-1~~ | ~~`test_medallion_e2e::test_produce_cascades_bronze_to_gold` fails~~ | ~~high~~ | ~~CLOSED 2026-09-07 — `test_medallion_e2e` runs 5 passed live. The suite also stopped reading as the anonymous principal in the same pass (Q17-8).~~ |
| ~~Q8-2~~ | ~~`POST /ingest-media` answers 503 `media ingest catalog registration failed; retry`~~ | ~~high~~ | ~~CLOSED 2026-09-07 by `a86f5407` — recorded in full as Q16-9. The head ASKS the catalog and names the answer on the trigger; `test_media_lane_derives_under_governance` passes.~~ |
| ~~Q8-3~~ | `test_auth_e2e::test_oidc_and_openfga_authorization_chain` fails | high | The OIDC→FGA chain end to end. Skipped until today because `LANCE_E2E_AUTH_SERVER` was never exported — **CLOSED (`4555f2a3`).** It was `LANCE_E2E_WAREHOUSE` never being exported, not an authz defect. `test_auth_e2e` passes |
| ~~Q8-4~~ | `test_governance_e2e` — the flow, and `non_owner_cannot_rename_or_overwrite_anothers_table` | high | A governance leg asserting that a non-owner is refused. Unverified live until today — **CLOSED (`e0007d28`).** Two drifts: the root namespace door, and `bob` being a project admin where the suite called him a non-owner. `test_governance_e2e` 3/3 |
| ~~Q8-5~~ | `test_client_direct_e2e` — zero-byte ingress commit, and ACID concurrent commits | high | Both skipped as "stack not reachable" purely because `svc()` handed them OpenFGA's gRPC port — **CLOSED 2026-09-07, driven live: 3 passed in 6.48s.** `svc()` now picks the port NAMED `http` and falls back to `ports[0]`, which is the repair; measured on the live estate, `rask-openfga` publishes `grpc=8081 http=8080 playground=3000 metrics=2112`, so the positional pick had handed every suite the gRPC port. Both high-value legs — the zero-byte ingress commit and the ACID concurrent-commit no-lost-update — now run and pass. THE FIXTURE'S SKIP MESSAGE IS WHY IT SAT: it names three services and probes them under one `try`, so it could never say which one failed |
| Q8-6 | `test_catalog_live` — domain-error translation fails; schema round-trip and milestone loop error | med | The catalog's own error contract |
| Q8-7 | `test_e2e::test_unsupported_is_406` gets 401 | med | The auth door fires before content negotiation. May be correct fail-closed behaviour with a stale assertion — classify before fixing |
| Q8-8 | `test_e2e::test_full_lifecycle` fails | med | |
| ~~Q8-9~~ | `test_outbox_crash_e2e::test_sigkilled_producer_loses_nothing` fails | high | Durability under SIGKILL. The estate's claim that the outbox loses nothing — **CLOSED 2026-09-07 (`202e52d8`), and the row was right for the wrong reason.** Driving it found the STAGING half perfect and the RECOVERY half broken. Three separate defects, each hiding the next: (1) the assertion read `run_id in dict(list_events(...))`, comparing an id to a key — `_object_key` widened to `<run_id>@<eventType>` and this was never updated, so it reported a LOSS while the event sat in the outbox 15 seconds old; (2) the relay drive treated the single-flight guard's `{"skipped": true}` and then a read timeout as failures, when both mean "a sweep is running" — the drain is the LAST step of a tick that reconciles `checked: 400` datasets under one lock, so completed sweeps are minutes apart though the cron ticks every 30 s; (3) THE REAL DEFECT — `lineage_outbox_drain_failed` on EVERY sweep, `Entity failed to be updated: 3`, one event the AGE graph refused, and the only `try` wrapped the WHOLE drain, so that one event stranded every other staged event permanently while the tick answered 200 and depth climbed. Fixed with per-event isolation (stranded, never dropped) + a `DrainOutcome` pair + `outbox.events.stranded`. Live after the roll: `outbox_drained: 2, outbox_stranded: 0`, outbox at zero, no `drain_failed`, and the e2e leg passes in 361 s |
| ~~Q8-10~~ | `test_ray_train_e2e::test_train_to_blessed_with_full_reproducibility_capture` fails | med | Compute-plane adjacent; check the scope line before acting — **STRUCK OUT OF SCOPE 2026-09-07 (owner ruling).** *"prio lakehouse and ignore other zones that are not the lakehouse or compute. I.e search, flows and model training and annotator should be ignored."* This row belongs to MODEL TRAINING, so it is not work this register carries. Struck rather than deleted, because a row that vanishes reads as done |
| ~~Q8-11~~ | `test_user_state_e2e` deletes the catalog pod mid-suite | med | `test_user_state_e2e.py:235` runs `kubectl delete pod -l app.kubernetes.io/component=catalog`. Legitimate for what IT asserts (state survives a restart) and destructive for every suite that runs after it. Needs ordering, isolation, or its own invocation — not removal — **CLOSED as a finding — the behaviour stands and is now known.** `test_user_state_e2e.py:235` deletes the catalog pod deliberately, to assert state survives a restart. Every live measurement since excludes it explicitly, and the exclusion is stated wherever a number is quoted |
| Q8-12 | Five legs still need a SECOND tenant (`LANCE_E2E_PROJECT_B` + its token) | med | Cross-tenant credential isolation is unproven live. Provisioning a second project on the estate is the work |
| Q8-13 | Three legs skip on a 5s `/livez` timeout while the producer serves the cascade | low | The producer is up (1 restart, 20h ago; `/livez` answers 200 by hand). A suite-robustness row — the probe is too tight for an estate under load, and a timeout that reads as "not reachable" is a skip that hides a pass |
| ~~Q8-18~~ | Bounding `/runs` broke TWO callers that needed the board complete | high | **FOUND AND FIXED 2026-09-07** (`935b0db6`, `cb5c8766`), both built with Dagger, deployed and driven live. The bound (`65fd0e24`) was verified on the endpoint and was worthless for the estate: **a bound is a CONTRACT change, not a performance one** — "every row" and "the newest N" answer different questions, and code written against the first does not fail against the second, it silently answers a smaller one. (1) The A8 provenance check scanned the board for one run; a run older than the page reads ABSENT while present in the graph, so A8 would report a defect that does not exist. (2) The cascade lag reader scanned it for runs consuming a lane. **Measured live on `bind86-gold$catalog`: 0 of its 6 consuming runs are on the bounded board**, so that lane would have reported having consumed NOTHING and `lag_for_edge` would have published a confident number from it. Both were asking a POINT question of a LIST endpoint, so both became reads the graph already knew how to answer — `GET /runs/{id}` (`MATCH (r:Run {run_id:$rid})`) and `GET /datasets/{name}/producers` (`MATCH (r:Run)-[:WROTE]->(d:Dataset {name:$name})`, whose projection gained the run's consumed range). Live after the roll: the point read answers 512 B / 0.084 s where the board cost 2,652,260 B / 1.4 s, the per-dataset read returns 6 of 122 producers carrying real ranges (`245->247`, `243->245`), and the lag cron measures 261 edges with 13 published points and no errors. **A caller that scans a list for one thing is describing a query the API is missing** — recorded in `docs/DECISIONS.md` |
| Q8-15 | The live sweep reports 37 `storage_loss` and 79 `unreadable` datasets | high | **CAUSE FOUND 2026-09-07, and it is not a data problem — it is TEST RESIDUE IN THE PRODUCTION LINEAGE GRAPH.** All 79 `unreadable` are `NoSuchBucket` on `lakehouse` / `landing` — buckets that do not exist — and every name carries an e2e marker (`bronze$events_e2e12bc3bbc`, `e2e049e1466-gold$catalog`, …). The 37 `storage_loss` are the same shape: `advz1788208093ns$t1`, `qp77c7f7b2ns$qt`, `recon$t`, and literally `probe$nonexistent` — a test that asked about a dataset that never existed, whose node the graph kept forever. **THE MECHANISM: the e2e suites write into the estate's real lineage graph and NOTHING removes what they leave.** `prune_runs` is the only prune in the service and `LINEAGE_RUN_RETENTION_DAYS=0` on this deployment (opt-in, default keep-everything); there is no dataset-node prune at all. Measured in AGE: **1,230 Dataset nodes**, 400 of them still sweepable. **THE COST IS THE CONTROL, NOT THE DISK.** Both warnings fire on every tick with 116 dead rows in them, so a REAL storage loss or a genuinely unreadable dataset arrives invisible — the estate's own 'a control that cannot fire' pattern, reached by accumulation rather than by design. And each tick pays 79 failed S3 list calls INSIDE the single-flight lock that also gates the outbox drain (Q8-16). **WHAT IS LEFT IS A FORK, not a measurement**: make the suites clean up after themselves; give dataset nodes retention the way runs have it (and pick a rule — no successful run in N days? unreachable across N consecutive sweeps?); or set `LINEAGE_RUN_RETENTION_DAYS` and let the derived drop-stamp logic carry it. Reclassifying `NoSuchBucket` as its own quieter class is the ONE option to refuse: a production bucket actually being deleted is the emergency this warning exists for |
| ~~Q8-17~~ | The sweep checks 400 datasets while the graph holds 1,230 | high | Suspected 2026-09-07 that the reconciler silently covered a third of the estate — **REFUTED by reading the code it runs.** `list_datasets` is fetch-all with no cap. `reconcile_all` skips a dataset with no `source_uri`, and skips one whose dropped-ness is DERIVED AT READ TIME from run history (`DATASET_LAST_SUCCESS_OP` — the latest COMPLETE run that wrote it being a `drop_table`). So 400 is 'sourced and not deliberately dropped', which is the right population. Recorded struck rather than dropped because the arithmetic looks alarming and the next reader will suspect the same thing |
| Q8-16 | The outbox drain is gated behind a whole-estate storage scan | med | Measured 2026-09-07: `_on_cron` runs the 400-dataset `_sweep`, the outbox drain and the run prune under ONE advisory lock, in that order. So the durability-critical step waits on the slowest one, completed sweeps are minutes apart against a 30 s cron, and every tick in between is `skipped_locked`. A staged event's recovery latency is therefore set by how many datasets the estate has, which is the wrong variable. Splitting the drain onto its own lock (or its own tick) is the shape; the sweep's cost is the reason |
| Q8-14 | The new locator gate misses ~21 further dangling pointers | low | Its regex sees a row-id form only; the `§7.11` / `§2` / `Phase 1` forms escape it, across 16 files including `docs/DECISIONS.md`. Found while scouting Q7-4 |

## Q9. The acme drift, split and closed (2026-09-06)

Owner ruling: SPLIT THE ID. The two datasets were never one table — they were two tables that had
collided on one name, and the estate had been telling itself otherwise since 2026-08-24.

Measured before touching anything: the row sets are DISJOINT, not superset/subset. The vended
location held 4 rows of a blob/media schema written by `service-ingest`, with two indices and a
`published` tag; the composed path held 5,370 rows of `{id, payload, stage}` whose ids do not
intersect them at all. Silver and gold (5,370 each) derive from the composed path, and the AGE
lineage node already recorded it as this table's location — the catalog's `__manifest` row was the
single dissenting pointer in the estate.

| # | Finding | Sev | What remains |
| --- | --- | --- | --- |
| ~~Q9-1~~ | `POST /produce?project=<tenant>` had been 503 for TEN DAYS | high | **CLOSED.** Last successful tenant produce 2026-08-27 07:16:46; `register_written_dataset` landed 2026-08-29 (`531864e2`) and every produce since answered `503 medallion catalog registration failed; retry`. Nothing reported it because the suite that would have caught it was skipping. After the split: `202 {"status":"produced","dataset":"acme-bronze$events"}` |
| ~~Q9-2~~ | The catalog governed a 4-row table while 5,370 rows sat ungoverned | high | **CLOSED.** `acme-bronze$events` → `medallion/bronze` (5,370); `acme-bronze$objects` → `4750a5b9_acme-bronze$events` (4). Both verified by `count_rows` through the catalog |
| ~~Q9-3~~ | A repoint would have orphaned `service-ingest`'s real ingest data | high | **CLOSED, and it is why the ruling was re-put.** I had described the drift as "nothing is lost", which was wrong — the 4 rows are real blob data with a `published` tag. Registering them under their own id keeps them governed; `service-ingest` was re-granted `owner`, which `register` had re-seeded to the calling operator |
| Q9-4 | The cascade head still TELLS the catalog a composed location | high | The split fixes the DATA; the rule violation stands. `produce.py` composes `{root}/medallion/{ns}` and registers it, while every stage runner asks (`ensure_stage_output`, rule I2). `_require_same_location` correctly refuses a disagreement and the head has NO convergence path — it answers `503 Retry-After: 5`, promising a convergence no retry can produce. The next tenant whose catalog and head disagree is 503 again |
| ~~Q9-5~~ | ~~`/ingest-media` is the same defect at a second door~~ | ~~high~~ | ~~CLOSED 2026-09-07 with Q8-2/Q16-9 — the same fix closed both doors.~~ |

## Q10. Is this an idiomatic Lance lakehouse? (2026-09-06, fable 5.1 audit)

Ten dimensions audited against `lance_docs/` (171 files) and `lancemultibasebranchingblobv2.md`.
**111 findings: 28 IDIOMATIC, 29 DIVERGENT-ON-PURPOSE, 54 WRONG.**

READ THE STATUS OF THESE ROWS BEFORE ACTING ON THEM. The audit hit the session limit with 29 of 49
agents unrun — every adversarial refuter and both synthesis agents. The workflow's own "survived
refutation" filter is vacuous when no refuter ran, so **the 54 WRONG findings are UNVERIFIED except
the four below, which I probed against the installed library and the live estate myself.** A false
WRONG is more expensive than a missed one; the rest are leads, not findings.

| # | Finding | Sev | What remains |
| --- | --- | --- | --- |
| ~~Q10-1~~ | **The cascade enabled stable row ids at every create and destroyed them at every write** | high | **CLOSED FOR THE IN-PROCESS AND RAY-HEAD LANES 2026-09-06 (C1); the media lane is Q10-6.** Both in-process write sites are now FULL-SYNC MERGES — `merge_insert("id").when_matched_update_all().when_not_matched_insert_all().when_not_matched_by_source_delete()` — which keeps the tier's semantics (the run's output IS the whole dataset; a row it no longer produces is deleted) while preserving `_rowid`. Measured on pylance 10.0.0: overwrite moves `_rowid` [0,1,2]→[3,4,5], the merge leaves it [0,1,2], and a source dropping id=3 deletes it while id=1 keeps `_rowid` 0. **The live proof of the defect**: bronze held 8 rows across 20 versions with `_rowid` [2004..2011] while silver's `source_rowid` read [88..95] — 8 of 8 dangling, the whole D1 chain resolving to nothing, reported by nothing. A SECOND gain measured on the way: the merge PRESERVES indices the overwrite dropped (`list_indices()` empty after overwrite, `id_idx` standing after merge), which is the two-commit split behind the recorded 8→1000 row jump that published without asking anyone. Eight prose sites rewritten, not annotated |
| Q10-2 | Two modules hand-roll manifest protobuf parsing to read base paths the library exposes | med | **VERIFIED.** `ds._ds.base_paths()` exists on pylance 10.0.0 (returns `{}` on a base-less dataset). `blobs.py:61` states "THIS EXISTS BECAUSE PYLANCE EXPOSES NO WAY TO READ A DATASET'S REGISTERED BASES" and `lander.py:338` repeats it; `features.py:160-260` walks varints at manifest fields 18/4/3. Two second sources of truth, one of which travels with any schema copy |
| Q10-3 | `defer_index_remap=True` is requested on every dataset and refused on every governed one | med | Lance: stable row ids need no index remap. `optimize.py:237` asks anyway, catches the refusal by substring, retries plain — 211 `compact_defer_index_remap_unsupported` warnings in 48h. The pinning test builds its fixture WITHOUT stable row ids, so it pins the path no governed dataset takes |
| Q10-4 | `id` is the merge key the tier contract requires but is never declared as Lance's unenforced primary key | med | **NOT A NEW FINDING — the estate already records it.** `ingest/catalog.py::A14` states it plainly: "NOT an 'unenforced primary key' in Lance's sense… that feature is opt-in through field metadata (`lance-schema:unenforced-primary-key`), which this plane sets nowhere… Declaring it properly is open work; claiming it in a comment was not the same thing." So this is known open work, correctly described, not a divergence anyone was unaware of. It matters more now that both tiers merge on `id`: the key is load-bearing at every write and still only a convention |
| Q10-5 | The other 50 WRONG findings are UNVERIFIED | — | Headlines by dimension are in the workflow transcript. Governance: Lance specifies no authz model but DOES specify credential vending (`vend_credentials` → `storage_options` + `expires_at_millis`) — rask's rung model is a parallel invention, recorded. Lineage: the AGE graph holds what the manifest cannot (WHO, DERIVED_FROM, failures) but the two stores join on a bare version int because nothing stamps Lance's native `transaction_properties`. Blob: media DOES enter as `lance.blob.v2` at every door — format-level multimodality is real — but every live tier holds a MANAGED COPY and the external-pointer path is unreachable. Branching: routed in spec shape, driven by nothing. **Re-run the refuters before acting on any of these.** All 111 findings are now in `open_lance-idiomaticity.findings.md` with their `lance_says` / `we_do` / file:line evidence — all 111 reached a verdict in C2 and the file was DELETED — see § Q13. The leads it held are now 6 refuted-to-idiomatic, 13 refuted-to-divergent, 4 fixed today and 14 confirmed rows |

## Q11. The two engine seams, measured (2026-09-06)

The estate has TWO stacked seams and I described them as one, then reported the upper one as absent.
Correcting that here rather than leaving the wrong claim standing.

```
WORKFLOW ENGINE   Dapr Workflow — durable steps, timers, external events
                  swappable in principle for Argo / Flyte
       | activities
COMPUTE ENGINE    the Executor port — WorkOrder, task_registry, attestation
                  adapters: InProcessExecutor, RayJobExecutor
```

Ingest, batch processing and the quality gate are not separate planes — they are that same pair
instantiated three times.

MEASURED: 68 orchestration constructs (`yield ctx.*`, `DaprWorkflowContext`) live in FOUR files, two
of which carry 65 — `medallion/workflow.py` (36) and `ingest/workflow.py` (29). The other ten files
that import `dapr.ext.workflow` carry ZERO: they are runtime registration and client calls (start an
instance, poll status, terminate). So a workflow-engine swap rewrites two files, not twenty-four,
and the activities below it — `submit_stage`, `poll_stage`, `report_stage_outcome` — build a
`WorkOrder` and go through the compute port, surviving the swap untouched.

| # | Finding | Sev | What remains |
| --- | --- | --- | --- |
| ~~Q11-1~~ | The activity layer reaches UP to the orchestrator | med | **CLOSED 2026-09-06 (C5).** `service_kit.lakehouse.saga` is the `SagaClient` port — two operations (`start`, `exists`), no engine named, no engine dependency, mirroring `executor.py` one layer down. `medallion/services/dapr_saga.py` is the Dapr adapter and the ONE place the engine is named on the starting path. `transform.py` and `train.py` no longer import `dapr.ext.workflow` at all. `SagaStart.ALREADY_RUNNING` moved a distinction into the port that every caller used to re-derive: a schedule failure is two events wearing one exception — "already watched" (handled) and "nothing is watching" (must raise) — and the stage lane cannot swallow the second because the saga is what submits the job at all, while the train lane correctly can because its job was already submitted. `_stage_workflow_exists` was deleted rather than left orphaned; the adapter owns it now |
| Q11-2 | `ray_submit.py` goes around the compute port | med | Recorded in `docs/DECISIONS.md` "The compute plane is decoupled" as the remaining decoupling work: it is a module of functions naming no `capabilities`, so the port cannot describe it. The estate's one ray import outside a runner |
| ~~Q11-3~~ | I reported BYO-workflow as "no seam at all" | low | **CORRECTED HERE.** The count I quoted (24 imports) was registration plumbing plus orchestration added together, and the conclusion drawn from it was wrong. The seam is structurally present and concentrated; what is missing is Q11-1 |

| Q10-6 | The Ray MEDIA lane still overwrites, and it cannot take the same fix | med | `scripts/ray_stage_job.py:239` writes `mode="overwrite" if written == 0 else "append"` per BATCH. A per-batch `when_not_matched_by_source_delete` would delete the rows earlier batches just wrote, so the full-sync merge that fixed the other three write sites does not transpose. It needs ONE merge over the whole scan, or accumulate-then-sync — a streaming-shape change, not a call swap. Recorded rather than half-applied |
| Q10-7 | The Ray DISTRIBUTED branch creates its target with an overwrite | med | `ray_stage_job.py` writes an empty `out_schema` table with `mode="overwrite"` then distributed-appends into it. Deeper tabular stages CARRY `source_rowid` as a plain column so the chain survives, but the tier's own `_rowid` is re-minted every run, which is what the tier ABOVE it would resolve against. Same shape as Q10-6: the create is fine, the re-create is not |
| Q10-8 | THE DEPLOYED CASCADE RUNS THE RAY LANE, and that is how a half-fix nearly read as a whole one | high | Measured: after deploying the in-process fix, a `POST /produce` preserved bronze's `_rowid` (`[2012..2019]` twice running) — but the stage runner's own log said `stage-ray-silver-c1proof0002`, so silver was written by the Ray lane, not the lane the unit tests exercise. Silver's `source_rowid` stayed `[88..95]`, 8 of 8 still dangling. **Any claim about the cascade that rests on `services/medallion/tests` is a claim about a lane this estate does not run.** The Ray head now merges too; the media and distributed branches are Q10-6/Q10-7 |

| Q10-9 | A cascade fix reaches the estate through THREE images, and the obvious one is wrong | high | Measured while deploying C1. `services/medallion/*` ships in `lance-rest-catalog` (producer + the three stage runners). `scripts/ray_stage_job.py` — the code that actually writes silver on this estate — is baked into **`ray-lance`**, not `ray-cluster` (`.docker/ray-lance.dockerfile:81` copies it to `/home/ray/jobs/`), and the head running it is `ray-lance-head`, which is HAND-APPLIED from `deploy/ray-lance-demo.yaml` and outside the chart. The live head was on `ray-lance:main-0dd7a95f` from 2026-08-30 with `grep -c when_not_matched_by_source_delete` = **0**. So a cascade change verified by unit tests, deployed to the fleet image, and confirmed by a live produce can still be entirely absent from the code that does the work. **Any cascade deploy must name which of the three images carries the change and prove it in the running one** |

## Q12. The Q3 rows RE-MEASURED against HEAD (2026-09-06, C3)

39 rows were carried into § Q3 when `open_python-audit.md` was drained, ON THAT LEDGER'S WORD, and
never re-measured. This pass measured 38 of them against the code at HEAD — reading files, running
tests, and recovering each original finding verbatim from `git show 058da189^:open_python-audit.md`.

**34 STILL-OPEN · 4 ALREADY-CLOSED · 0 NEVER-TRUE.**

THE RESULT CORRECTS MY EXPECTATION, and that is the point of measuring rather than assuming. C3 was
written on the premise that "several are certainly already closed" and that re-verification would
SHRINK the register. It did not: the drained ledger was substantially accurate a month on, and only
four rows had been fixed since. The register loses four rows, not forty.

What the pass DID buy is precision. Every surviving row now carries a HEAD file:line instead of a
month-old summary — `CAT-CORE-13` names `catalog/core/config.py:26`, `DUP-15` turns out to be FIVE
implementations rather than two, `ingest-flow-06` names `queue.py:452-457`, `SKG-10` counts four bare
env reads rather than five. A backlog row a reader can act on today is worth more than one they must
first re-derive.

| # | Finding | Sev | What remains |
| --- | --- | --- | --- |
| Q12-1 | One Q3 row could not be measured | low | 38 of 39 returned; the 39th agent did not complete. Re-run before § Q3 is called fully verified |
| ~~Q12-2~~ | `DUP-15` is worse than recorded | med | Carried as "the Dapr-workflow scheduler is written twice and the copies' timeouts disagree". Measured at HEAD: the bounded schedule is implemented FIVE times with no shared seam. The row's severity was set against the smaller number — **REFUTED 2026-09-07, and the count is the finding.** There are FOUR `ctx.create_timer` call sites, not five: the fifth was DOCSTRING PROSE — `ray_jobs_api.py` and `workflow.py`'s module header both mention `ctx.create_timer` while calling nothing. Counting a mention as an implementation is the same error as counting a comment as a reader, which this estate has now made three times in one day. **AND THE FOUR ARE TWO PATTERNS, so a shared seam would merge things that differ**: two poll-WATCH loops (`stage_run`, `train_run` — timer, poll, watch-lost/vanished/never-registered, `continue_as_new` under `max_polls`) and two DEADLINE RACES (`when_any` over an external event and a timer — ingest's run limit, the medallion's approval gate). Only the two watch loops are genuinely duplicated, and **one of them is the TRAIN lane, which the 2026-09-07 scope ruling puts out of scope** — so the in-scope remainder is a single implementation with nothing to share it with. Re-open if the cascade grows a second in-scope watch loop |

## Q13. The audit's 54 WRONG findings, adversarially refuted (2026-09-06, C2)

The 2026-09-06 idiomaticity audit claimed 54 WRONG findings and its refuters all died on the session
limit, so `956c6d72` recorded them as LEADS and refused to act on them. This is that verification, run
to completion: 49 agents, 0 errors.

**54 claimed → 37 reached a refuter → 18 SURVIVED · 13 DIVERGENT-ON-PURPOSE · 6 IDIOMATIC.**

TWO THIRDS OF THE "WRONG" FINDINGS WERE FALSE, which is the result the goal's own wording anticipated
("a false WRONG costs more than a missed one") and the reason none was acted on unverified. Refuted
does not mean the auditor was careless — most refutations grant every citation and overturn the
VERDICT: the Lance claim holds, the rask claim holds, and a recorded reason or a measured library
behaviour makes the divergence deliberate rather than defective. Several were refuted because the
premise was measured rather than read: "compaction memory is bounded by batch_size, not fragment size"
and "lance_ray's write strips blob typing" were both falsified on the installed library.

FOUR OF THE 18 WERE FIXED EARLIER TODAY and are already struck as Q10-2/3/4 — `base_paths()` read from
the manifest, `defer_index_remap` branched on `has_stable_row_ids`, and `id` declared as Lance's
unenforced primary key. One more, the overwrite finding, was refuted TO IDIOMATIC *because* C1 had
already landed: the refuter named the commits and re-verified the baked Ray job independently.

The 14 that remain, each having survived an adversarial attempt to refute it:

| # | Finding | Sev | What remains |
| --- | --- | --- | --- |
| Q13-1 | "lance_ray's write strips blob typing" is false on the installed 0.5.0 — the multimodal lane is single-driver by mistake | med | Survived refutation. NOT REFUTED. I reproduced the auditor's measurement myself rather than trusting it, on the exact library set the cascade image pins (.docker/ray-lance.dockerfile:67 installs lance-ray==0.5.0 pylance==10.0.0 pyarrow==25.0.0; tests/unit/test_ray_job_images.py:105 holds those equal to the venv; ray 2.58.0). Script: /tmp/claude-1000/-home-bla |
| Q13-2 | '_transactions/*.txn accumulate forever, nothing prunes them' is false on the installed pylance — the sweep's own default cleanup reclaims them | med | Survived refutation. NOT REFUTED — the WRONG verdict stands, with one sub-claim corrected and one line-number drift.  1. LANCE CLAIM: TRUE on the installed pylance 10.0.0 (code, not just docs). Measured on a local-filesystem dataset (scratchpad, no docker, no repo edits): (a) a planted unreferenced `_transactions/999-…deadbeef.txn` aged 8 days is REMOVED by t |
| Q13-3 | Compaction gate: the hazard is real, but two of the three readings guard shapes the format excludes and the base parse reinvents `base_paths()` | med | Survived refutation. NOT REFUTED. I tried, by construction, and the auditor's thesis survived two shapes the auditor never built.  1. LANCE CLAIM — TRUE on the installed lance 10.0.0 (code, not docs; the pyi stub and the vendored docs never mention `base_paths()`). `dir(ds._ds)` lists `base_paths` beside `serialized_manifest` (the handle features.py:183 alrea |
| Q13-4 | Describe-vend omits the spec's `expires_at_millis` key from `storage_options` | med | Survived refutation. NOT REFUTED — every leg of the finding holds, and the load-bearing part (that a Lance client refreshes ONLY on this key) is now measured rather than inferred.  1. LANCE CLAIM: true. lance_docs/ns_catalog/spec.yaml:2878-2880 (also namespace.md:3822, DescribeTableResponse.md:16): "If the vended credentials are temporary, the `expires_at_mil |
| Q13-5 | Graph version identity is (dataset, "N"); Lance's is (branch, N) — a branch write collides with main and reconcile classifies it as storage loss | med | Survived refutation. NOT REFUTED — every link of the chain verified, then observed live.  1. LANCE CLAIM TRUE (doc and library agree). lance_docs/guide.md:4003-4008: "Each branch maintains its own linear version history, so version numbers may overlap across branches. Use (branch_name, version_number) tuples as global identifiers". Probed on the installed pyl |
| Q13-6 | NamespaceExists/TableExists answer `null` with application/json where the spec says 'Success, no content' | med | Survived refutation. Every rule was checked and the finding survives all four; severity is cosmetic and the fix is two lines, but it is not refutable.  (1) LANCE CLAIM TRUE, AND THE LIBRARY CORROBORATES IT. lance_docs/ns_catalog/spec.yaml :243-262 (NamespaceExists) and :443-470 (TableExists) read verbatim "REST namespace conveys the result through the HTTP st |
| Q13-7 | No branch-scoped governance, though the `tree/<branch>/` layout exists to give storage ACLs a boundary | med | Survived refutation. NOT REFUTED, but the finding is not new and two of its specifics are wrong. (1) LANCE CLAIM — true, with a measured caveat the auditor's fix ignores. file_format.md:2745-2770 documents `tree/{branch}/` for `_versions/_transactions/_deletions/_indices`, and I measured on the installed pylance 10.0.0: an APPEND on a branch touched only `tre |
| Q13-8 | Publication's "second door above the router" is provably dead; 'who may promote' has three answers | med | Survived refutation. NOT REFUTED on the load-bearing claim; the secondary "three answers" claim is overstated and I narrow it below.  1. LANCE CLAIM — holds. lancemultibasebranchingblobv2.md:258-262 describes write-audit-publish as branch → review → merge (Jack Ye's Iceberg retrospective, which Lance's branching adopts per the same brief); lance_docs/guide.md |
| Q13-9 | Request-validation failures answer 422 where the spec prescribes 400 BadRequest — and the register shows the estate knows it | med | Survived refutation. NOT refuted — the finding survives, and my verification made the Lance side STRONGER than the auditor cited, while confirming the severity is low.  1. LANCE CLAIM: true, and normative. The auditor cited spec.yaml's BadRequestErrorResponse description (:6512-6516: 400 "could be caused by an unexpected request body format or other forms of  |
| Q13-10 | Root ListNamespaces is not federated across warehouses — a spec client cannot discover governed data from the root | med | Survived refutation. NOT REFUTED — the finding survives all four checks and I reproduced its live numbers with the service's own code.  1. LANCE CLAIM: TRUE. Every citation reads as quoted: object-relationship.md:36-37 (root "ready to be connected to by a tool to explore"), spec.yaml:2296-2298 (root id = delimiter; `v1/namespace/$/list` lists the root), spec. |
| Q13-11 | The cascade's only built-in transform is an IMAGE deriver, in shared code, deployed live | med | Survived refutation. NOT REFUTED. Every factual leg of the finding checks out; what needs correcting is its precision, not its verdict.  1. LANCE CLAIM — TRUE, doc and code agree. lance_docs/guide.md:211 (blob.md): "Lance can store large binary objects (images, videos, audio, model artifacts) in blob columns, where they are treated like any other column paylo |
| Q13-12 | The decision register and six other prose sites still say Unsupported answers 501; the code, tests and deployed pod answer 406 | med | Survived refutation. NOT REFUTED — every cited line says what the auditor says, and the drift is worse than filed: one RUNTIME consumer still keys on 501.  LANCE CLAIM: TRUE (vendored spec, which is what governs — the installed lance_namespace 0.11.0 `errors.py` carries no HTTP-status map at all, the client dispatches on `code`, so status is a server/spec mat |
| Q13-13 | The governed-tier contract is name-only for `stage` and `lineage`: the platform writes JSONB, a sealed runner writes string, and the publish door pass | med | Survived refutation. UPHELD. (1) Lance claim true, by doc AND library: guide.md:1815-1821 (pa.json_() stored as JSONB, the json_* functions operate on it) and :2014-2044 (JSON scalar index is for pa.json_() columns); root .venv lance 10.0.0 refuses a JSON index on Utf8 ("A JSON index can only be created on a Binary or LargeBinary field", json.rs:726) and fail |
| Q13-14 | The orphan scanner re-implements Lance's own unverified-file detection and reports LIVE index files as garbage | med | Survived refutation. NOT REFUTED — every load-bearing claim reproduced against the installed library and the live estate.  1. LANCE CLAIM: TRUE on the code that runs (pylance 10.0.0, .venv). `cleanup_old_versions` (dataset.py:3112-3175) and `explain_cleanup_old_versions(include_files=True)` (dataset.py:3177-3238) exist with the stubbed shapes (`CleanupStats.i |

| Q13-15 | `open_lance-idiomaticity.findings.md` is DELETED | low | **CLOSED HERE.** It existed to hold 111 findings that were living in a 26 MB session transcript outside the repo. Every one has now reached a verdict: 28 IDIOMATIC and 29 DIVERGENT-ON-PURPOSE recorded as such, 6 more refuted TO idiomatic, 13 to divergent, 4 fixed today, 14 carried above. A working file with a defined end, deleted when it reached it |

## Q14. The live suite after a day of fixes (2026-09-06, C4 baseline)

Measured against the deployed estate carrying every fix from today, excluding `test_user_state_e2e`
(it deletes the catalog pod mid-run, so anything after it fails for a reason that is not its own):

```
    session start   70 passed   19 failed   15 skipped
    after C1/C5/C6  82 passed   18 failed   10 skipped
```

`test_dummy_lane_e2e` LEFT THE FAILURE LIST — the empty-service-identity fix (`845e4fc7`) working on the
deployed estate rather than only in a unit test.

`test_governed_union_e2e` ENTERED IT with five failures, and that is the runner fix showing its worth:
those five had been skipping on `medallion-producer not reachable` because `LANCE_E2E_LANCERAY_URL` was
never exported. They are newly visible, not newly broken — the same shape as § Q8.

| # | Finding | Sev | What remains |
| --- | --- | --- | --- |
| Q14-1 | 18 live failures await classification | med | Each becomes SUITE-DRIFT (repaired in the test), ESTATE-DEFECT (repaired in the service), CONTAMINATION (named) or ALREADY-FIXED. None is left as "failing" |
| Q14-2 | `test_governed_union_e2e`'s five legs have never run against a deployed estate | med | The governed cascade, the FGA-deny promotion drop, the quality gate's block-and-record, the media lane under governance, and train lineage attribution. Five of the estate's headline governance claims, unverified live until today |

## Q15. Three medallion namespaces were bound to warehouses holding nothing (2026-09-06, C4)

`POST /ingest-media` answers 503 `media ingest catalog registration failed` — Q9-5 — and the cause is
not the media head. Measured on the live registry:

```
  namespace      bound location    default root    verdict
  bronze-media          empty            2 rows    STALE — binding names a warehouse with no bytes
  gold                  empty            8 rows    STALE
  silver-media          empty             empty    STALE — names nothing either way
  silver               8 rows            8 rows    LIVE — the binding is real; leave it
```

A warehouse binding claims a TOP-LEVEL namespace, so `bronze-media` resolved to
`s3://lakehouse-wh/medallion/bronze-media` — where nothing exists — while the head writes to
`s3://lance-catalog/medallion/bronze-media`, where the two rows are. `_require_same_location`
correctly refuses the disagreement and the door answers 503 with a `Retry-After` no retry can satisfy.

THE SAME RESIDUE PATTERN AS THE ACME WAREHOUSES (Q11-2's cousin): five e2e-minted warehouses for one
project, and now three bindings naming empty warehouses. Something creates estate-level records in
tests and nothing removes them, and each one silently re-points a platform namespace.

`silver` is the instructive contrast: its binding is LIVE, the stage runner ASKS the catalog, and it works —
writing to `bind86-wh` without anyone noticing or minding. The head TELLS, so the same class of binding
breaks it. That is Q9-4 restated with evidence: the stage runners survive a moved namespace and the head
cannot.

| # | Finding | Sev | What remains |
| --- | --- | --- | --- |
| Q15-1 | Three stale bindings hijack medallion namespaces | high | `bronze-media`, `gold`, `silver-media` name warehouses holding no bytes. Deleting them is the repair and it is BLOCKED: the classifier refuses the registry delete, and the only API door (`POST /v1/namespace/{id}/drop`) is destructive beyond the binding. Records backed up to the session scratchpad. **Needs an owner decision or an unbind door** |
| Q15-2 | The catalog has no UNBIND door | med | A binding is write-once and cleared only by dropping the namespace or deleting the warehouse. So stale residue can only be removed by an operation that also destroys data, or by hand-editing the registry — which is how this estate accumulates the residue it cannot clear |
| Q15-3 | Nothing removes estate records a test creates | med | Five e2e warehouses for `acme`, three dead bindings. Each one silently re-points a platform namespace or makes a project unroutable (Q11 needed a `primary` marker for exactly this). A suite that mints an estate record owns removing it |

## Q16. The governed-union suite, driven to a verdict (2026-09-07, C4)

`test_governed_union_e2e` was the largest remaining C4 block: five failures, one estate, five
different causes. It had been SKIPPING for long enough to accumulate them — the runner withheld
`RASK_E2E_LANCERAY_URL`, so every repair pass that fixed what it found red passed this file by, and
`f0b97870`'s idempotency-header fix reached its two siblings and not this one.

Driven to green one cause at a time, the five split three ways, and the split is the useful part: two
were ESTATE defects, one of them serious and silent; two were suite drift that ALLEGED a governance
hole; one is estate residue that needs a door the catalog does not have.

**Live, against the deployed estate: 5 failed → 3 passed, 2 failed.**

THE PATTERN ACROSS ALL FIVE is a check that could not fail. A `DROP` that meant four different
things; a revoke whose own helper swallowed the batch failure that voided it; a poll for a run id the
catalog can never mint; an "outsider" who is a project admin. Each one passed, or failed, for a reason
unrelated to what it claimed to measure — the same shape as the tolerated 409 that hid a namespace
hijack, and the reason a green suite is not evidence until each leg is shown able to go red.

| # | Finding | Sev | What remains |
| --- | --- | --- | --- |
| ~~Q16-1~~ | ~~The cascade-lag tick held the event loop and the producer's own probes timed out~~ | ~~high~~ | ~~`_on_cron` awaited nothing: both readers are synchronous per-edge `httpx.get`, one tick measured fifteen seconds. 21 readiness and 7 liveness timeouts in 22 minutes against `timeoutSeconds: 1`; NotReady drops the pod from its Service, which is what made this suite skip on one run and execute on the next. `run_in_threadpool`, `bec74183`~~ |
| ~~Q16-2~~ | ~~The Ray head presents the SHARED app token as a PRIVILEGED subject~~ | ~~high~~ | ~~CLOSED 2026-09-07, end to end. `auth.dedicatedServiceCredentials` is ON here, so lineage requires `service-trainer`'s own credential and refuses the shared one by design — every training run logged `lineage emit attempt 1 rejected: HTTP 401`, published its model and exited SUCCEEDED, losing all provenance with nothing red. Fixed in code (`96e6885f`: one helper for seed and mount, the head off infra-credentials, the ESO entry, `deploy/ray-lance-demo.yaml`), then delivered live: the key added to `rask-infra-credentials` and the hand-applied head PATCHED at its env (never `kubectl apply -f`, which reverts the image and re-breaks the cascade). Proven: the head's token now matches the store, the e2e leg passes, and the newest train job's log carries ZERO 401s.~~ |
| ~~Q16-3~~ | ~~Every write door 422'd: the suite sent no `Idempotency-Key`~~ | ~~high~~ | ~~Required with no default since `2da0164c`, so FastAPI refused at header validation before auth, before the lane, before any governance. Compounded by `_produce` REPLACING its whole header dict on a project estate, which dropped the key again. `d6cecc03`, `eea08ae4`~~ |
| ~~Q16-4~~ | ~~The deny test revoked nothing, and `_tuples` hid it~~ | ~~high~~ | ~~OpenFGA fails a whole write BATCH on an absent tuple; `_tuples` tolerated that as idempotency, so one stale entry voided every revoke and reported success. `_owner_tuples` then omitted the NAMESPACE owner on the stated grounds that `seed_ownership` writes none — false here — and owner outranks the writer rung. One tuple per request, every owner listed, and the revoke VERIFIED with a `check` before anything is concluded from it. `7bc19af7`~~ |
| ~~Q16-5~~ | ~~Two governance legs named an identity instead of asking for one~~ | ~~med~~ | ~~`bob@example.com` is not an outsider here (`team:eng` → `project:acme`, and a team member is a project admin), and `WAREHOUSE` was hardcoded to `lakehouse-wh` on an estate whose project warehouse is `acme-bucket`. `topology.OUTSIDER` already carried the first; this was the fourth suite to miss it. `c8309297`~~ |
| ~~Q16-6~~ | ~~The quality leg corrupted a bronze nobody reads and asserted gold by an id that cannot exist~~ | ~~med~~ | ~~It composed the single-tenant path (rule I2, from the consuming end), sent a project-qualified id with no `project` so the stage runner answered `medallion_stage_other_lane`, and polled `run_id_for("aggregate_gold-<token>")` — which OPERATIONS already records as unmintable under `cascadeViaPublish`, so its negative passed vacuously. `db44c7f0`, `acef01a7`~~ |
| Q16-7 | `_DROP` renders every refusal reason identically | med | `{"status": "DROP"}` is the ack for a routing drop, an unresolvable lane, an FGA denial and a held promotion. The counters and logs distinguish them; the wire does not, so a caller cannot tell governance from misrouting — which is exactly how Q16-6's assertion passed on a trigger that never reached the gate. The ack contract is deliberate (Dapr neither redelivers nor dead-letters a DROP); the opacity of the REASON is not obviously load-bearing |
| Q16-8 | The Ray lane's pass-1 ack cannot carry a stage verdict | low | A direct drive answers SUCCESS at dispatch; the gate runs at pass 2 and answers the sidecar. Any suite asserting an outcome on the pass-1 response is asserting the in-process lane while the estate runs the Ray one. Recorded rather than fixed — the asymmetry is real and the suite now accepts both acks and asserts the verdict from lineage |
| ~~Q16-9~~ | ~~The media 503's mechanism, with the binding evidence~~ | ~~high~~ | ~~CLOSED 2026-09-07 (`a86f5407`), and the measurement settled which half was wrong: `bronze$events` resolves to `s3://lance-catalog/medallion/bronze` (no binding) while `bronze-media$objects` resolves to `s3://lakehouse-wh/medallion/bronze-media` (bound). The catalog resolves a registered RELATIVE path against the namespace's WAREHOUSE BINDING and `register_table` refuses an absolute one, so a caller cannot dictate its own location; `lance-catalog` is reserved from every warehouse, so a bound top-level namespace can never resolve into the platform root. The head now ASKS and names the answer on the trigger, and the stage runner asks where its upstream lives — both halves, because either alone strands the lane. Live: the whole governed-union suite passes, 5 failed -> 5 passed.~~ |

## Q17. BYO and zero trust, made countable (2026-09-07)

Two bodies of work that were ANALYSED but not TRACKED, which is how they stayed open without ever
appearing in a count. §Q11 measured the two engine seams and §F scored 19 zero-trust controls, and
neither produced a row this file's own gate can see: the gate counts `### A1 ·` headings and
`| Q<n>-<n> |` rows, and §F2's twelve items are a markdown ordered list. So the single largest
security gap in the estate — every service running as the storage ROOT user — was carried in prose
that no header count included. §F2 stays as the analysis; these are its tracked rows.

**BYO is TRUE of the lakehouse and FALSE of the lane the estate runs.** Measured 2026-09-07:
`catalog`, `lineage`, `maintenance` and `service-kit` have zero `import ray` and zero declared ray
dependency (their only matches are comments and one blocklist literal). The ports name no engine and
are gated. What is missing is callers.

| # | Finding | Sev | What remains |
| --- | --- | --- | --- |
| Q17-1 | The medallion bypasses its own `Executor` port on the deployed lane | high | **THE MISSING SEAM LANDED 2026-09-07**: `engine_registry.executor_for` resolves a chosen engine NAME to an adapter, which nothing did before — `engine_for` answered with a string and the two adapters were reached, or not reached, by hand. `hosted_engines()` is asserted against `engine_choice.HOSTED_ENGINES` so an engine cannot be choosable-but-unresolvable or the reverse, and an unknown engine RAISES rather than defaulting onto whatever is configured. **STILL OPEN: the nine call sites are not migrated** — the deployed stage lane still calls `ray_submit` directly, so the seam exists and the lane does not use it. That migration is what lets `services/medallion/pyproject.toml` drop `ray-kit`. Original measurement: 9 direct `ray_submit` call sites across 4 modules — `workflow.py` x4, `train.py` x3, `transform.py` x1, `stage_runner.py` x1 — while `transform.py:760` builds the only adapter anyone constructs (`InProcessExecutor`). The decoupling is real for the in-process lane and fictional for the Ray one |
| Q17-2 | `RayJobExecutor` is a DEAD adapter | high | **AND IT COULD NOT HAVE WORKED — measured 2026-09-07, which is a better answer than "dead".** The adapter renders `WorkOrder.to_env()` into the RayJob's `runtime_env`, and the job programs read `FROM_URI`/`TO_URI`/`STAGE`/`STAGE_CARDINALITY`/`BASE_VERSION`/`LINEAGE_JSON` — **overlap 0 of 6**. A job submitted through the port would have started with NONE of its inputs bound, and an empty source URI is not a crash: it is a run that scans nothing, writes nothing and reports success. Nothing gated it — `test_ray_job_wire_parity.py` compares the Python and TypeScript RayJob SCHEMA, not the env contract between a submitter and the program it submits. Converged 2026-09-07 (`32ff50cb`): three programs plus the sealed `runners/dummy` now read the order's names, the submitter emits `to_env()` and its legacy duplicates are deleted, and the missing gate (`test_the_submitter_and_the_job_agree_on_the_wire.py`) covers every program a stage submission feeds. **A FOURTH AUTHOR surfaced in the e2e**: `test_dummy_lane_e2e`'s fixture submits to the Jobs API directly and hand-rolled the old spellings — it passed for as long as it was the last one that had not converged. **A SECOND BLOCKER REMAINS for the CR path**, and it is infrastructure rather than code: `RayJobExecutor` needs a `RayCluster` CR to select or an ephemeral one to create, and this estate has **zero** — the live Ray is `ray-lance-head`, a hand-applied plain Deployment with no ownerReferences. Choosing between a chart-owned RayCluster and ephemeral per-job clusters is an owner decision about job-record durability. **REACHABLE 2026-09-07** — `engine_registry.executor_for("ray", ...)` constructs it, so it is no longer constructed nowhere; it is still not on the deployed PATH, which is Q17-1's remaining half. Original measurement: Constructed nowhere outside tests. It conforms to the port, is tested by `test_a_stage_can_be_a_rayjob_custom_resource`, and nothing wires it up — so the port has two implementations and one caller, which is what let Q17-1 persist unnoticed |
| Q17-3 | `services/medallion` declares `ray-kit` | med | And need not: `rayjob_executor.py` has ZERO ray imports — it submits a RayJob CR over httpx. Migrating Q17-1 lets the dependency go, and then NO service in the estate depends on a compute engine. BYO stops being a claim about ports and becomes a property of the dependency graph |
| Q17-4 | `maintenance/services/compaction_executor.py` does not use the `Executor` port | med | **CONTEXT MEASURED 2026-09-07: the estate runs THREE worker models on purpose, and only one of them is an engine.** (a) A NATS WORK QUEUE — maintenance's `work_queue.py`, and `ingest/worker.py`, which is the fuller example: a queue of `UnitTask`s where a worker fetches and validates (I/O bound), units are held UNACKED until a batch is worth one fragment, then ONE `lance.fragment.write_fragments`, and the run ends in ONE commit (D6). That is Lance's own distributed-write pattern with no compute engine at all, and `max_ack_pending` is what bounds in-flight work — back-pressure an engine would have to re-implement. (b) RAY, for the cascade stages. (c) NONE — the quality gate materialises nothing (`count_rows` pushdown). So "BYO workers" is not one question: for ingest and maintenance the workers ARE their own replicas, which is BYO by having no engine to bring; the `Executor` port is the right seam only where work becomes CPU/GPU-shaped. **Ingest should NOT be migrated to Ray** — it would be a second worker fleet in front of a tuned one, add a scheduler hop to I/O-bound work, and pull a compute dependency into a service that declares none. Revisit only if a unit stops being I/O-bound (decode/transcode at scale, embeddings at ingest time). Original finding: The maintenance plane has its own worker lane (`work_queue.py`, `IndexWorkItem`/`DatasetWorkItem`, competing consumers on `queueGroupName`) but resolves its own execution rather than going through the port — so "BYO workers for maintenance" is true of the QUEUE and not of the EXECUTION |
| Q17-5 | Per-workload storage identities (§F2-1) | high | **THE LINEAGE PLANE IS DONE TOO, 2026-09-07, and measuring it produced the TIGHTEST policy of the four** — because its surface is the smallest, not because it matters less. **NO `PutObject` anywhere**, which no other scoped plane can say: lineage opens datasets READ-ONLY to probe versions, schema and dangling blobs (`core/reconcile.py:58,76,93,108`, `demo.py:112`), and the only bytes it changes are DELETES of outbox objects the relay has already re-ingested (`outbox.drop_event`). `outbox.stage_event` — the write half — belongs to the PRODUCERS and is called here never, so a write grant would be a capability with no code to use it. **The READ stays deliberately WIDE**, which is the interesting half: lineage reconciles whatever datasets the graph names across warehouses this chart cannot enumerate, so a narrowed read is a reconciler that silently stops seeing part of the estate — and a reader that cannot read reports `known=False`, publishing nothing and looking exactly like a healthy cascade. Gated in BOTH directions by `test_the_lineage_plane_writes_nothing_it_does_not_own.py`, including the pair that has bitten twice: a scoped access key left on the default secret field is signed against the tenant ROOT's secret and fails `SignatureDoesNotMatch` on every operation, so the field is asserted to move WITH the key. **`rask-catalog` is now the ONE identity left**, and it stays a design question rather than another copy of the pattern: it vends for every runtime-minted warehouse, so "what may the thing that grants access itself reach?" has no answer this policy shape supplies. **THE MEDALLION PLANE IS DONE 2026-09-07** (`e4cdd7f4`), leaving ONE identity. Live now: `rask-medallion-producer` and all three stage runners hold `rask-medallion`, `rask-maintenance` holds its own, and the Ray lane holds `rask-ray-compute` ON THE RAY POD ITSELF (`S3_KEY` + a `secretKeyRef` secret — no credential rides `runtime_env`, and the stage runner env that appeared to say so bound to nothing: Q17-20) — only `rask-catalog` is still `rustfsadmin`, with `rask-lineage` beside it. The medallion policy needed a THIRD shape: the Ray lane's total control-plane deny is right for a stage job and wrong for the services that drive it, which read `_tasks/`, `_transforms/`, `_gates/` and the warehouse registry over S3; and `_tasks/` is writable because this plane REGISTERS them while the Ray lane is their untrusted consumer. Probed against live RustFS: list-cascade ALLOW, write `_tasks/` ALLOW, read `_warehouses/` ALLOW, write `_transforms/` DENY, write `_gates/` DENY, observability DENY — then the whole governed-union suite passed on the scoped credential. **A SECOND ROOT IDENTITY THE SWEEP NEVER COUNTED: `rask-lineage`** (`LINEAGE_S3_ACCESS_KEY_ID=rustfsadmin`, measured live 2026-09-07). Its S3 surface is small and scopeable, unlike the catalog's — it opens datasets READ-ONLY to probe versions (`demo.py:112,135`, `lance.dataset(uri, storage_options=opts)`) and reads/writes exactly one prefix it owns, the outbox at `LINEAGE_OUTBOX_URI` (`s3://lance-catalog/_lineage_outbox`), where the reconciler stages, re-ingests and drops events. So a policy shape exists: read anywhere the cascade writes, write only `_lineage_outbox/`, deny every other control prefix — the medallion's third shape with the write set narrowed to one prefix. **What remains is `rask-catalog`**, and it is the hard one on purpose: the catalog serves the data plane in-process and vends credentials for every runtime-minted warehouse, so a least-privilege policy for it is a design question (what may the thing that grants access itself reach?) rather than another copy of this pattern |
| ~~Q17-6~~ | ~~Fail closed in CODE, not only in the chart (§F2-2)~~ | ~~high~~ | ~~CLOSED 2026-09-07. `assert_authentication_configured` refuses to boot a governed service whose auth is off with nobody having acknowledged it, a sibling of `assert_app_token_configured` in shape and reasoning. THE REFUSAL IS ON THE AMBIGUITY, not on being open: flipping the `oidc_enabled` default would change what a configured deployment does and red every suite that constructs settings without naming auth (49 files), while what is actually indefensible is that 'off because I meant it' and 'off because nothing set it' look identical. `RASK_INSECURE_ALLOW_UNAUTHENTICATED` is the acknowledgement — it turns nothing off. SCOPE WAS THE HARD PART and cost three reverts: `maintenance` and `notifications` mix in `FgaSettings` alone ('no human door: its routes are gated by the Dapr app token and it only ever READS tuples, as itself'), and the medallion STAGE RUNNERS render no OIDC either — wiring any of them refuses a boot over a mode they never had. Landed for the three that do have a door: catalog, lineage, the medallion producer. The chart answers in BOTH `auth.enabled` modes, and `test_every_governed_service_can_actually_boot` DISCOVERS the wired set from the sources rather than listing it, so the next service wired without its env reds a test instead of crash-looping the estate~~ |
| Q17-7 | Kill the one shared service bearer (§F2-3) | high | **THE WIDEST HOLDER IS DONE AND LIVE 2026-09-07** (`bf273f07` + the estate rollout). Seven web pods no longer mount the estate's shared bearer; `service-web` is privileged at lineage and holds its own credential off infra-credentials. Proven on the estate: the dedicated token answers **200** and the shared bearer presented as `service-web` answers **401**, with all seven zones healthy and no real web read refused. **A SEQUENCING CORRECTION worth keeping**: "client half first" is right when the subject is ALREADY privileged (the trainer), and WRONG here — an unprivileged subject's door still expects the shared token, so moving the pods first opens a 401 window. For a subject being ADDED to the privileged set the two halves must land together; there is no safe gap in either direction. **`service-maintenance` DONE 2026-09-07** — client half (`catalog_compaction.dedicated_token_for`), server half (on the catalog's `LANCE_PRIVILEGED_SUBJECTS`) and the SEED all landed together, because a privileged credential has THREE halves and the third nearly shipped a 401: `openbao.yaml` derives what to mint from a DIFFERENT list than `services.yaml` derives what to demand, and an unseeded privileged identity resolves to `None` — correct for "not provisioned" — falls back to the shared bearer, and is refused for being privileged. Gated by `test_a_privileged_subject_can_present_its_own_credential.py`, which asserts all three. **`service-ingest` DONE AND VERIFIED LIVE 2026-09-07** (`9405b732`, release 105) — ONE builder (`ingest.service_identity`) for BOTH doors it calls, because `service_principal` refuses the shared token from a privileged name AND a dedicated token from an ordinary one, so a subject privileged at one door and ordinary at the other cannot satisfy both. Both server halves are DERIVED from the service's own `RASK_*_SERVICE_IDENTITY` rather than typed twice. Proven on the live pod: the resolver reads `service-token-service-ingest` from the store and the token on the wire is the dedicated one, not the shared bearer; then driven at the quiet door — `GET /runs` with the dedicated token answers **200** and the SAME privileged name with the shared bearer answers **401**. A positive and a negative case, which is what makes it a control rather than an absence of errors. Ingest needed an EXPLICIT gate maintenance did not (`RASK_INGEST_SECRETS_FROM_DAPR`): `catalog_token()` deliberately skips the store when identity and token are both set, so an unconditional resolver would fail closed on a dev stack that has no store and needs none. **`notifications` — CLIENT HALF DONE, SERVER HALF IMPOSSIBLE OVER ITS TRANSPORT, and the row does NOT close. Measured live 2026-09-07 (release 107, reverted in 108).** The client half works: the resolver reads `service-token-notifications` from the store and `feed_token` puts it on the wire. The door still refused it — `401 the presented credential may not claim 'notifications'` — and the cause is the TRANSPORT, not the service. The reconciler reaches lineage through DAPR SERVICE INVOCATION (`127.0.0.1:3500/v1.0/invoke/lineage/method/events`), and daprd stamps its OWN `dapr-api-token` on every request it delivers, so the credential lineage's door sees is the estate's shared one whatever the caller sets. Naming the subject privileged therefore made the door demand a token the transport cannot carry: every walk 401'd, and because a refused walk returns no rows rather than an error, the symptom was a silently incomplete inbox — the exact failure the change was meant to prevent, caused by the change. Reverted; feed read back to **200**. **THE GENERAL FINDING, which is bigger than this row: a dedicated credential is a property of the TRANSPORT, not only of the service.** Ingest holds one at this same door because it calls lineage DIRECTLY over HTTP; anything reached through service invocation cannot present one at all. So F2-3's remaining work is not a credential change but a design question — move this call off service invocation, or accept that sidecar-invoked hops authenticate as the estate. **THE ORDERING RULE GAINS A THIRD CASE**: the client half alone is inert, the server half alone is an outage, and now — a subject whose transport overwrites the credential makes the PAIR an outage. Rendering both halves is not evidence; only the wire is The last holder of the shared bearer. It calls ONE door (lineage's durable feed), so unlike ingest there is no second list to agree with; what earned it the same treatment is the DIRECTION its refusal fails. `reconcile` walks `GET /events` precisely because the bus alone is provably incomplete — ingest, Ray TRAIN and external OpenLineage producers emit over HTTP only — and a refused walk returns no rows rather than an error, so a 401 there is not a failure anyone sees: it is an inbox quietly missing exactly the events the walk existed to catch. All three halves again: the client half (`notifications.api.service_identity.feed_token`, resolved in the LIFESPAN so a store read happens once per process rather than once per cron tick), the server half on `LINEAGE_PRIVILEGED_SUBJECTS`, and the seed. `SecretStr` is preserved end to end so resolving a different token cannot lose the property that kept the old one out of logs. **F2-3 IS CLOSED**, and `test_a_privileged_subject_can_present_its_own_credential.py` now asserts the row itself — no privileged subject lacks a client half — over the RENDERED set rather than a written list, so the next subject is covered by the file existing — each still needs its client half (both have 0 `dedicated_token` refs), and the site count is measured so neither repeats maintenance's near-miss: **ingest stamps the identity at TWO sites** (`provenance.py:75`, `catalog_service.py:227`) and must go behind one builder like maintenance did; **notifications has ONE** (`reconciler.py:246`). Each also needs its token added to the OpenBao seed's own list — the third half |
| ~~Q17-8~~ | ~~Anonymous browser reads are laundered into a service identity (§F2-4)~~ | ~~high~~ | ~~CLOSED 2026-09-07, FAIL CLOSED (owner ruling). `bff.ts:191-196` and `runs-feed.ts:224-231` send `x-lance-service-identity` ONLY when there is no session, so that subject is the ANONYMOUS PRINCIPAL and whatever it holds is what the public can read. It held 7 reader grants across 6 namespaces spanning TWO tenants plus a `writer` on `table:bronze$events` — four readers seeded by `seed_medallion_fga.sh` (which `values-prod.yaml:18` names a PRODUCTION PREREQUISITE, so this was not demo-only), three by `seed_estate.py`, and the writer by nothing in the repo at all (Q15-3 residue). All eight revoked live, both seeds stopped granting it, and the prose that justified them rewritten. THE E2E SUITES WERE THE REASON IT SURVIVED: `test_medallion_e2e` and `test_media_e2e` read lineage AS `service-web`, so they asserted what a logged-out visitor sees while appearing to assert governance — both read as a user now, 6 passed. Proven live: anonymous -> a tenant dataset **403**, anonymous -> the run feed **200 with an empty list** (the DatasetFilter dropping what it cannot see, not leaking), signed-in -> **200**. Gated by `test_the_anonymous_principal_is_granted_nothing`, in both directions — no seed may grant it, and no suite may read as it~~ |
| Q17-23 | A hand-applied `mc-drift` pod has sat in `Error` since 2026-09-06 | low | Measured 2026-09-07: `kubectl get pod mc-drift` shows **no ownerReferences** — hand-applied, not chart-owned — created 2026-09-06T04:09:39Z, and its entire log is one line: `ALIAS_FAILED`, i.e. it could not reach RustFS. Nothing recreates it and nothing reads it, so it is residue rather than a symptom. **Left in place deliberately**: it is someone's diagnostic and deleting another operator's artifact is not a tidy-up to make unasked. Recorded because it is the same hazard class the estate already pays for with `ray-lance-head` — a hand-applied object the chart does not know about, which every health check has to learn to ignore, and which reads as a broken estate to anyone who has not been told otherwise |
| Q17-25 | The CNPG cutover's blockers are two-thirds stale, and it is F2-6's answer for AGE | med | **RE-MEASURED 2026-09-07.** The estate has NO cert-manager and the chart has never generated a certificate, so hand-rolling TLS on the AGE StatefulSet would introduce machinery this estate has never had — while the CHARTED path already issues certs, because a CNPG Cluster gets server TLS automatically. `values.yaml` gates that path off with three named blockers, and **two no longer hold**: K8s 1.33+ with ImageVolume (live: **v1.36.2**, gate default-on since 1.35), CNPG >= 1.27 (live: **1.29.1**), and containerd >= 2.1 from the dockerfile's own header (live: **2.3.2-k3s2**). **THE IMAGE IS NOW BUILT** (2026-09-07, Dagger, `age-cnpg-ext:1.7.0-18`, digest `sha256:2b9572f1…`) and verified from the REGISTRY MANIFEST rather than a build log: three layers matching the three `COPY` lines — 8,559 B of `/share/extension/age*`, **1,497,689 B of `/lib/age.so`**, 4,667 B of the licence. (Inspecting it with `dagger core container … entries` returned nothing and would have read as an empty image; the same command lists `alpine:3.20` fine, so the tooling was the problem and the manifest is the authoritative check.) `.docker/cnpg-age-ext.dockerfile` builds it and `scripts/dagger-image.sh --name cnpg-age-ext` invokes it; `docs/CNPG-AGE.md` carries the design. **AND ITS LAST UNPROVEN LAYER IS NO LONGER UNPROVEN.** `docs/CNPG-AGE.md` records a 2026-07-20 investigation: layers 1 (the AGE extension + `extension_control_path`) and 2 (the ImageVolume prerequisites) were PROVEN, and layer 3 — the operator managing a real Cluster — failed as an explicitly-diagnosed KIND-HOST gremlin across CNPG 1.30/1.28. Re-measured on this estate 2026-09-07: the operator is **1.29.1, 1/1 Running, leader-elected and reconciling**, its log carrying hourly `pki: Periodic TLS certificates maintenance` — the very machinery §F2-6 needs, running and idle for want of a `Cluster` to manage. **The doc also names a second win the F2-6 row never mentions**: CNPG does PHYSICAL backups + PITR, which removes the "does a logical `pg_dump` round-trip the AGE graph labels?" DR hazard outright — the risk `scripts/age_restore_drill.sh` exists to prove against. So the cutover buys TLS and deletes a restore risk. **The cutover stays an OWNER decision because it is a DATA MIGRATION** — the lineage graph and OpenFGA's tables live in the StatefulSet's PVC, and `age-cluster.yaml` fails the render if both stores are on. Building the image is cheap and reversible; moving the data is not. A note listing three blockers, two of which quietly stopped being blockers, is the same shape as every other row here: the claim was true when written and nobody re-measured |
| Q17-24 | `CLAUDE.md` said the AGE and OpenFGA databases are CloudNativePG; they are not | med | **CORRECTED 2026-09-07.** Measured live: **zero** `cluster.postgresql.cnpg.io` objects exist, `rask-age-0` is owned by `StatefulSet/rask-age`, and `age.cnpgCluster.enabled` defaults **false** with the chart's own reason — the AGE-on-CNPG cutover needs a built extension image (`.docker/cnpg-age-ext.dockerfile`), K8s 1.33+ and CNPG >= 1.27, and `age-cluster.yaml` FAILS the render if both paths are on. **What made the false line easy to believe is the estate's recurring pattern**: `cnpg.enabled: true` installs the OPERATOR and `rask-cloudnative-pg` runs, so a reader asking "are our databases CNPG-managed?" finds a component by that name running with nothing to reconcile. The operator toggle and the custom-resource gate are DIFFERENT values, and only the second decides whether the resource exists. Both sentences in `CLAUDE.md` §Architecture rewritten |
| Q17-26 | Signing images would add a control nothing verifies (§F2-12) | med | **MEASURED 2026-09-07, and it changes what the row asks for.** §F2-12 is right that there is no SBOM, no signature and no in-toto/SLSA attestation — re-checked: `provenance()` emits three build args that 13 dockerfiles turn into real `org.opencontainers.image.*` labels, and nothing more. **But the estate has ZERO signature verifiers**: no Kyverno, no sigstore policy-controller, no Gatekeeper — its five validating webhooks are CNPG, external-secrets and Kueue. So adding cosign signatures today would produce a control whose NAME is present and whose enforcement is absent, which is the anti-pattern §F2 has produced SIX times in one day. Signing needs a key custodian AND an admission-time verifier before it is a control, and both are owner decisions rather than code. **The SBOM half is genuinely different and partly already delivered**: `make audit` (osv-scanner over six lockfiles + `.dagger/go.mod`), `make scan-config` (trivy over `.docker/` + `chart/`) and `make scan-image` (trivy over a Dagger-built image) already answer "what vulnerable things are in this?". What an SBOM adds is a PORTABLE manifest a downstream consumer can scan without rebuilding — a supply-chain claim, not a scanning gap. Worth doing on its own terms; not worth doing as half of a signature story that has no verifier |
| Q17-9 | No Dapr access-control policy; NetworkPolicy off by default (§F2-5) | med | **THE ROW'S OWN PREMISE IS REFUTED, measured 2026-09-07 — and the count it rests on was taken on ONE plane of three.** The row says "only TWO service-invocation callers exist, so a `defaultAction: deny` needs 11 allow entries". Those two (`gateway`, `notifications`) are the callers on the HTTP `/v1.0/invoke` path, which is the only surface a grep for that string can see. The estate invokes over **three** planes:

    HTTP /v1.0/invoke   gateway, notifications
    ActorProxy          annotator, notifications      <- never counted
    Dapr Workflow       flows, ingest, medallion      <- never counted; it IS actors

**Dapr's own documentation settles half of it and opens the other half.** Verbatim: *"To restrict which applications can schedule workflows and activities cross-app, use a `WorkflowAccessPolicy` instead. Service invocation access control does not cover cross-app workflow scheduling."* So the workflow plane is EXCLUDED from this control by construction — and the estate has never heard of `WorkflowAccessPolicy`, which is a second, unrecorded gap this row now carries. Actor-to-actor invocation is documented NEITHER way, and the estate has 28 `ActorProxy` references behind the notifications inbox and the annotator's project plane.

**So this is not the eleven-entry change the row describes**, and deploying it on that count would have been the day's second outage: a `defaultAction: deny` keyed on the HTTP callers alone, against an undocumented actor behaviour, with the cascade's workflow plane in the blast radius. **The precondition DOES hold** — measured live: Dapr mTLS `true`, Sentry running, and `lance-tracing` is the shared Configuration every sidecar references, so there is exactly one home for the policy. **What it needs first is the actor plane characterised on a live estate**, because no document answers it. Corrected shape while measuring: the key is `policies:`, not `appPolicies:`.

**The original design note, kept because the HTTP half of it is right:** Re-confirmed: `accessControl` and `defaultAction` appear in **zero** chart templates, so the control is absent rather than weak. The caller side is genuinely small: only TWO components invoke over Dapr — the gateway (`_target_base`, `/v1.0/invoke/{app_id}/method`) and notifications (`api/settings.py:165`, → `lineage`). The gateway's route table names its targets exactly, and there are **eleven**: `annotator`, `catalog`, `compute`, `controlplane`, `flows`, `ingest`, `lineage`, `medallion-producer`, `notifications`, `search`, `viewer`. So a `defaultAction: deny` needs eleven `appPolicies` entries admitting caller `gateway`, plus `lineage` admitting caller `notifications` — and it has ONE home, the shared `lance-tracing` Configuration (`observability.yaml:72`) every sidecar already references. **The risk is stated rather than discovered**: a deny-by-default with an incomplete list locks the estate out of itself, and the gateway is the public edge, so this lands with a live drive of every route rather than a render. Original measurement: `grep accessControl chart/` returns 0, so any of the 16 sidecars may invoke any app-id; `networkPolicy.enabled: false` in the base values. **MEASURED 2026-09-07 AND IT IS SMALLER THAN IT LOOKS.** The estate has exactly TWO service-invocation callers: the gateway (10 targets — compute, controlplane, catalog, lineage, medallion-producer, viewer, search, annotator, ingest, flows) and notifications (one target, lineage). Nothing else calls `/v1.0/invoke/`; the stage runners, maintenance and the producer are driven by pub/sub and cron bindings, which `accessControl` does not govern at all. So a `defaultAction: deny` needs 11 allow entries, not an audit of 16×16. It also has ONE home: every sidecar already references the single `lance-tracing` Configuration (`_helpers.tpl:228`), so the policy is written once. Two caveats to carry into the change: a SHARED Configuration makes the policy list a union across callees, so the control it actually buys is "only gateway and notifications may invoke anything" rather than per-callee least privilege — still a real narrowing from "anyone may invoke anything"; and Dapr identifies the caller by SPIFFE identity, so this depends on the Dapr mTLS that Q17-10 confirms is the one place mTLS exists. NetworkPolicy is a separate half and is a no-op on k3s's flannel, so it cannot be verified on this estate — it is prod hardening, and turning it on by default needs a policy-enforcing CNI to test against |
| Q17-10 | TLS to every store (§F2-6) | med | **CONFIRMED LIVE 2026-09-07 by reading the schemes the running pods dial**, so this is per-store fact rather than a general claim. EVERY one is plaintext: RustFS S3 over `http://` from five services (`LANCE_`, `LINEAGE_`, `MAINTENANCE_`, `MEDALLION_`, `MEDIA_S3_ENDPOINT`) plus the STS endpoint, OpenFGA (`RASK_FGA_API_URL`), the AGE Postgres DSN (`postgresql://`, no sslmode), OpenBao (`BAO_ADDR`), the NATS monitor and the OTLP collector. Nothing dials `https://`. The estate's only transport security is the Dapr plane's mTLS, and every store sits outside it. mTLS exists on the Dapr plane and NOTHING else: OpenFGA, RustFS, NATS, OpenBao, Dex and both Postgres DSNs are plaintext or `sslmode=disable` by default |
| ~~Q17-11~~ | ~~`register_table` does not validate its location (§F2-7)~~ | ~~high~~ | ~~LARGELY REFUTED 2026-09-07, by driving the deployed door rather than reading one layer of it. The claim was that a writer "attaches any prefix the root key reaches" and then gets scoped credentials vended for it. Measured: an ABSOLUTE location answers **400 InvalidInput**; a RELATIVE traversal (`../../other-bucket/...`) answers **400**; a plain relative path answers 200 and resolves to `s3://<the caller's OWN warehouse>/<path>`. So containment exists and a caller cannot name another tenant's bucket. §F2-7 read the PYTHON door — which genuinely has no check (format, parent, trash and FGA only, `tables.py:596-650`) — and concluded there was none anywhere; the enforcement is in the native lance-ns backend beneath it. WHAT REMAINS is defence-in-depth, not a vulnerability: the door would be the only line if a backend without that enforcement were ever used, and within a caller's own warehouse any relative path is accepted. Both are worth a door-level check; neither is the cross-tenant hole that was recorded. The probe table this measurement minted was deregistered — Q15-3's own rule~~ |
| ~~Q17-12~~ | The `static` vending mode is dead (§F2-8) | low | **CLOSED 2026-09-07 by DELETION.** Confirmed first at the single construction site: `catalog/main.py:137` is the only `make_vendor` call outside tests and it passes no `static_keys`, so `LANCE_VENDING_MODE=static` built `StaticPrefixVendor({})` — and with an empty map every bucket is unknown, so it returned `None` for everything and silently degraded to the very mode it was chosen instead of. **"Returns None" was NOT the defect**, which is what made this hard to see: `ModeBVendor.vend` returns `None` deliberately. The defect was one layer up — the mode needed an argument nothing supplies, making it UNREACHABLE rather than merely quiet. Deleted: the Literal in `config.py` and `vending.py`, the branch, `StaticPrefixVendor`, the `static_keys` kwarg, its tests, and the `normalise_credential_keys` import that cascaded out with it. Gated by `test_every_vending_mode_is_reachable.py`, which parses `main.py`'s real call for the keywords it passes and refuses any permitted mode needing more — the general form, so the next inert mode reds instead of shipping. `sts` is kept as the reference shape: it RAISES without a role arn rather than degrading |`make_vendor` never passes `static_keys`, so a configured mode silently returns `None`. A control that does nothing is worse than an absent one — delete it or wire it |
| ~~Q17-13~~ | Well-known defaults are refused only on `devMode=false` (§F2-9) | med | **CLOSED 2026-09-07. The row named the wrong defect and the right title.** Four of the five values already had a guard — `age.password`, `rustfs.secretKey` and `dex.clientSecret` in `infra-credentials.yaml`, `dapr.appToken` in `dapr-app-token.yaml` (with the two-case shape) — and the fifth, `openbao.devMode`, is the signal rather than a credential. The defect was that ALL FOUR keyed on one opt-in flag: an operator who ships production without flipping `devMode` gets no refusal from any of them, and every one of these values is published in `chart/values.yaml`. Fixed by `chart/templates/prod-credentials.yaml`, an assertion-only template on the `auth-consistency.yaml` precedent: it always renders, answers "is this a real deployment?" ONCE, and fails with every offending value in one message. The second signal is `image.repository` set with `image.localImages=false` — an operator cannot forget it because the deploy already depends on it, `make k3s-up` renders `localImages=true`, and `rask.image` refuses a bare `<component>:<tag>` otherwise. Carve-outs measured, not assumed: `localhost:`/`127.0.0.1:`/`172.17.0.1:` registries are the dev loop and stay exempt; `externalSecrets.enabled` exempts the three infra values (ESO owns that Secret) but NOT `dapr.appToken`, whose Secret it does not own; `ingress.enabled` was considered as the signal and rejected because it defaults ON and the local loop runs on it — keying there would break the one render this guard must never break. `rustfs.accessKey: rustfsadmin` stays unguarded deliberately: it is the tenant root's NAME, not a secret, and the pairing is guarded on `secretKey`. Gated by `test_a_real_registry_refuses_dev_credentials.py` (7 tests) |
| ~~Q17-14~~ | Audit records carry no request or trace id (§F2-10) | med | **REFUTED 2026-09-07 by reading where the field is stamped rather than where it is passed.** The claim is literally true of the CALL SITES — 118 `audit()` calls and not one passes a request or trace id — and false of the RECORDS, which is what a compliance query reads. `CorrelationFilter` (`context.py:36`) stamps `record.request_id` and `record.trace_id` on every record, and `app.py:84` installs it on the ROOT handler; its docstring states the reason it is a filter and not a middleware: "a filter on the root handler covers every module in every service — including libraries — with no per-service edit". `lance.audit` is an ordinary logger that propagates to root, so its records are stamped like any other. It also never raises and never drops a record, and derives `trace_id` rather than naming it in the formatter, because `logging` raises on a format field the record lacks and an absent `otelTraceID` is normal in dev and tests. **THE SECOND CLAUSE IS NOW CONFIRMED TOO, measured against the live store 2026-09-07 — so the row is half refuted and half proven, and both halves needed driving rather than reading.** Audit records DO land: 477,096 rows in `opentelemetry_logs`. That is also the whole problem, and three measurements settle it. **(1) The stream is MUTABLE**: `DELETE FROM opentelemetry_logs WHERE scope_name = '<no match>'` was ACCEPTED and returned `affectedrows: 0` — the statement executed, it simply matched nothing, so nothing about the store refuses a delete that matches something. **(2) It EXPIRES**: the table declares `ttl = '14days'`, so every audit record is destroyed automatically a fortnight after the decision it records — a retention floor no compliance regime accepts, and the opposite of append-only. **(3) It is UNAUTHENTICATED in-cluster**: both queries above, the DELETE included, were issued to `:4000/v1/sql` with NO credentials from inside the cluster. And because audit shares ONE table with every other log, it can carry neither its own retention nor its own access policy — the properties would have to change for all telemetry at once. **What the row asked for is therefore a SEPARATE sink, not a setting**: an append-only destination the audit logger ships to on its own, with retention measured in years and writes nobody in the cluster can revoke. GreptimeDB stays right for telemetry, which is what it was chosen for |Lakekeeper stamps a uuid7 request id on every audit event; rask's `lance.audit` stream cannot correlate one decision to one request, and ships to no append-only sink |
| Q17-15 | Root create is open by default (§F2-11) | med | **DONE 2026-09-07** (`e6f4ce37`). THE SHIPPED DEFAULT WAS THE DEFECT, not its value: `hasKey` finds a key whether or not anyone chose it, so `values.yaml`'s `lockRootCreate: false` beat any derivation, and the control could only ever be armed by an operator who already knew to arm it. The key is deleted and `services.yaml` derives the value from `rask.isRealDeployment` — ONE helper, now shared with `prod-credentials.yaml`, so the credential guard and the root-create lock cannot answer "is this a real deployment?" differently. Rendered in all three directions rather than reasoned about: local loop `false` (self-serve preserved), real deployment `true` with nobody arming it, and an explicit `auth.lockRootCreate` still winning in BOTH directions. Gated by `test_root_create_is_locked_on_a_real_deployment.py`, whose second test refuses a `values.yaml` default so the opt-in cannot come back. **The original measurement, kept because it is what the fix answers**: **CONFIRMED LIVE 2026-09-07, not merely a chart default**: the running catalog carries `LANCE_FGA_LOCK_ROOT_CREATE=false`, so on THIS estate any authenticated subject may mint a top-level namespace. `config.py:245` defaults the field False and `fga_deps.py:290` gates the root-object check on it, so an absent lock reads as open self-serve rather than as missing configuration. `lockRootCreate: false`, and `fga_deps.py` reads a missing lock as "open top-level create" — any authenticated subject may mint a top-level namespace |
| Q17-16 | No image signing, no SBOM (§F2-12) | low | **CONFIRMED 2026-09-07, and there is a naming trap in the build that makes it easy to believe otherwise.** `.dagger/images.go` has a helper called `provenance()`, and it emits exactly three OCI LABELS — `BUILD_DATE`, `VCS_REF`, `VERSION` — passed as build args. No SBOM, no signature, no in-toto/SLSA attestation. A reader checking "do we have provenance?" finds a function by that name on the publish path and can reasonably conclude yes. The labels are worth having and are not the control this row names. Scanners only (osv-scanner, trivy, trufflehog). Lakekeeper lacks this too and even disables provenance, so this is parity, not a regression — but it is the last row of the zero-trust list |
| ~~Q17-17~~ | The estate's only two scoped identities are DRIFT that no chart renders | high | **CLOSED AND VERIFIED LIVE 2026-09-07 (release 102).** `helm get values rask` now carries `rustfs.medallionAccessKey: rask-medallion` and `rustfs.maintenanceAccessKey: rask-maintenance`, so both identities are RELEASE INTENT rather than hand patches and survive the next upgrade. The post-upgrade hook (`rask-rustfs-scoped-users-r102`) created both policies and rotated both RustFS users onto the DERIVED secrets; the producer, three stage runners and maintenance all came back 2/2 with zero restarts on `MEDALLION_S3_ACCESS_KEY_ID=rask-medallion` / `MAINTENANCE_S3_ACCESS_KEY_ID=rask-maintenance`. The upgrade also cleared a `failed` revision that had stood since 2026-09-03. **THE DEPLOY TAUGHT THE ROW'S OWN LESSON THE HARD WAY**: a first attempt HELD the identities back (`--set-string rustfs.medallionAccessKey=`) so the rename would land as one attributable change — and that crash-looped the producer and maintenance, because emptying the value made the OpenBao seed rewrite the store WITHOUT `medallion-s3-secret-key` while the Deployments still demanded it from the very drift this row is about. Fail-closed plus the old ReplicaSet staying up made it visible rather than an outage. Recorded in docs/DECISIONS.md — on a drifted estate, omitting a value is not a no-op. Original measurement: Measured 2026-09-07: `helm get manifest` renders `MAINTENANCE_S3_ACCESS_KEY_ID=rustfsadmin` while the live object holds `rask-maintenance`; the Ray lane is worse than drift on a rendered field — the live head is `ray-lance-head`, hand-applied and NOT chart-owned at all (the chart's own `rayservice.yaml` is not what runs here), holding its secret in a hand-created `rask-ray-compute-s3` Secret while `infra-credentials`' `ray-compute-*` keys render `rustfsadmin` because the values are empty. So the two controls Q17-5 credits the estate with exist only as a hand patch — `helm get values` carries no `rustfs` section at all. They survive today because Helm only patches fields that CHANGED between releases, which means they are one values edit away from silently reverting to the ROOT credential with nothing going red. The chart half landed for the medallion plane (values -> OpenBao -> both Deployments, gated by `test_the_medallion_runs_as_its_own_storage_identity`); maintenance and ray-compute still need their live drift turned into rendered values, and this estate's release is stuck at a FAILED revision 100 (post-upgrade hooks) so that upgrade is not a free action |
| Q17-19 | The medallion cascade hard-codes its steps in the workflow engine's own model | **DEFERRED BY OWNER RULING 2026-09-07** | **RULING: keep the option open, do not build for it.** The requirement is that the LAKEHOUSE depends on no workflow engine, and that already holds — catalog, lineage, maintenance, viewer, search and annotator import one in 0 files. Exercising a second engine is not an intent, so this is recorded and NOT scheduled; effort goes to the COMPUTE port (Q17-1..4), where the deployed path really does bypass its own seam. The measurement, kept so the decision stays reversible: `medallion/workflow.py` carries **42** engine-specific orchestration references because each step is written directly against Dapr's generator-replay model, while the ten other medallion files importing `dapr.ext.workflow` carry ZERO — they are registration and client calls, and both submission sites already go through `SagaClient`, which names no engine. So the gap is the step DEFINITIONS alone. `saga.py`'s own docstring rules on why they do not reduce to one interface: an engine replays a generator, another walks a YAML DAG, another composes decorated tasks, and a port over all three is their lowest common denominator. Making the cascade interpret a declared graph is the only thing that would collapse the 42; nothing else in the medallion needs to move. **Do not argue this row from `services/flows`** — owner ruling 2026-09-07: flows is the studio flow-builder's backend, a separate plane that needs no work, and citing it as a model implies a convergence that is not wanted |
| ~~Q17-20~~ | The Ray lane's storage identity is real, and every artefact that CITED it was wrong | high | Measured 2026-09-07 while working Q17-17. Three live stage runner Deployments carried `MEDALLION_RAY_S3_ACCESS_KEY_ID=rask-ray-compute`, and that string was the evidence quoted by `open_goal.md`, by Q17-5's row and by `test_the_medallion_runs_as_its_own_storage_identity`'s own docstring. **It bound to nothing**: a repo-wide search for the variable and for a `ray_s3_access_key_id` settings field returns only that docstring — no service reads it and no chart template renders it. It is residue of the approach `ray_submit.py` abandoned on 2026-08-30, when the key stopped riding `runtime_env` because the Jobs API echoes that back on an unauthenticated `GET /api/jobs/<id>`. **The control itself is fine and better than the claim**: the live `ray-lance-head` pod holds `S3_KEY=rask-ray-compute` with its secret by `secretKeyRef`, so the stage job runs scoped from the pod, not from a submitted env. Removed from all three Deployments and rolled; the docstring rewritten to say where the identity actually comes from. **The lasting lesson is the audit failure, not the variable**: an env var that binds to no setting is indistinguishable from a control when read, and three separate documents credited the estate with a control on the strength of one. A gate that refuses a rendered env no settings field binds catches the chart-side half of this class, and `test_no_rendered_env_binds_to_nothing.py` is it. **COMPLETED 2026-09-07: that gate covered the PYTHON plane only** — it skips `web-` workloads by name and never saw the sealed runners, so the zone and runner planes were checked by nothing while its docstring recorded a measurement ('those four are the ONLY prefixed envs on a zone pod') that would go stale the moment somebody added a fifth. A second test now covers every plane by a different mechanism — does any first-party SOURCE read the name — **and PROSE IS NOT A READER**, which is the whole difference between it and a grep: this variable WAS in the tree, in the docstring that wrongly credited it, so a text search would have found it and passed. Measured while building it: 13 first-party rendered envs bind to no pydantic field and ALL THIRTEEN are correctly wired — three read by SvelteKit zones, two by a sealed runner, eight by a settings mixin. A gate seeing one plane would have reported 13 false defects, which is F2-5's mistake exactly. Genuinely unread today: ZERO. The live-drift half stays Q17-17 |
| Q17-21 | The catalog cannot scale with the planes that call it | med | Measured 2026-09-07, answering "can a workflow bring down the catalog?". It cannot crash it — the workflow engine is a LIBRARY (`dapr-ext-workflow` is declared by exactly three services: medallion, ingest, flows; the catalog declares it zero times and imports `dapr.ext.workflow` in zero files), and those three import the catalog's Python package in zero files, so the only coupling is HTTP across separate Deployments. But the pressure has nowhere to go: `values.yaml:941` records that `controlEmit`'s ring buffer AND its cursor are PER-REPLICA, so the catalog is "correct at services.catalog.replicas=1 (the default)" and scaling it past one needs session affinity or a shared buffer. Every stage calls it — describe, create, vend — and the workflow hosts DO scale (`values.yaml:312`: replicas:1 is the fleet default, not an actor constraint; placement spreads instances and `queueGroupName` makes replicas competing consumers). So the one component that cannot scale out is the one every scaled-out stage runner calls. **OWNER RULING 2026-09-07: stage runners stay at 1 replica for now, so this is a recorded ceiling rather than a defect.** THE TRIGGER IS EXPLICIT — this must be answered BEFORE `medallion.stageRunnerReplicas` is raised above 1, because that is the change that turns a ceiling into an outage, and the symptom would be catalog latency rather than anything naming the cause |
| Q17-22 | The lineage seed registers datasets in buckets that do not exist, and the drift report can never clear them | med | Measured 2026-09-07. `lineage/seed.py:51-54` HARDCODES `s3://lakehouse/bronze/events`, `s3://lakehouse/silver/features`, `s3://lakehouse/gold/catalog` and `s3://landing/raw/events` while the configured bucket is `LINEAGE_S3_BUCKET`, which is `lance-catalog` on this estate. **Neither `lakehouse` nor `landing` exists**: the live RustFS volume holds 106 buckets — `lance-catalog`, `rask-observability` and ~104 runtime-minted `*-wh` — and no `lakehouse`. So every dataset carrying a seed-shaped location is permanently `unreadable` to the reconciler, which reports it each tick as `NoSuchBucket ... prefix=bronze/events/_versions/`. Seen in the wild: a reconcile sweep over `checked: 389` datasets came back with a long `storage_loss` list and an `unreadable` map led by `bronze$events_e2e12bc3bbc`, `e2e3367167a-bronze$events` and `e2e3367167a-gold$catalog` — all pointing at `s3://lakehouse/...`. **The cost is the EVIDENCE, not the data**: the drift report is the estate's own instrument for spotting a real divergence, and one dominated by entries that can never clear is an instrument nobody reads — the same failure as a count that treats one defect recorded three times as three items. **ATTRIBUTION, kept honest**: the seed HARDCODES those URIs and `tests/e2e-py/test_lineage_e2e.py:142` asserts the spelling (`source_uri == "s3://lakehouse/silver/features"`), so the convention is real and exercised; whether the seed also runs on a production estate is NOT measured here and must not be assumed. The demo endpoint next to it does the right thing — `demo.py:181` composes `f"s3://{bucket}/{path}"` from the CONFIGURED bucket — so the fix is the one-line shape already used ten lines away. The residue already registered needs deregistering separately |
| Q17-18 | The cascade needs settling time after a multi-service rollout, and the suite cannot tell that from a defect | med | Measured 2026-09-07: rolling catalog + lineage + medallion-producer together, then running `test_governed_union_e2e` immediately, gave 3 failed / 2 passed — `lance_ray_ingest` and `embed_features` both `None` for the first drive. Every tuple checked out (`can_create_table`, `owner`, `can_promote` all True) and the one 403 in the stage runner log was test 2's own deny window working. A re-run minutes later: 5 passed, unchanged code. So the suite reports a settling estate and a broken one identically, and the only way to tell them apart today is to run it twice — which is how a real regression gets waved through as "probably just settling". Either the drives need a readiness precondition (the stage runners' Dapr subscriptions re-registered, the workflow runtime resumed) or the runner needs to wait for one |
