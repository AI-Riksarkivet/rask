# open-lakehouse-diff2 — the catalog layer diffed against Lakekeeper / Unity / Gravitino, and what came back wrong

Working findings file, **2026-08-14**. Unsettled work; this file is deleted when its items land
(`docs/` is for settled architecture only). Written so another agent can pick up any single finding,
re-verify it, and implement the fix without re-running the analysis.

**Provenance.** Produced by two multi-agent workflow runs on 2026-08-14:
`wf_ee7e7283` (5 code readers over `services/catalog` + `packages/service-kit`, 3 web researchers on
Lakekeeper / Unity Catalog / Gravitino, 3 independent diff agents) and `wf_88eb56e8` (4 readers over
the vendored `lance_docs/` — `namespace.md` 7 563 lines, `file_format.md` 5 489, `lance_sdk.md`
6 441, `ray.md`, `ns_catalog/spec.yaml` 6 663, `ns_catalog/catalog/{dir,rest}`,
`ns_catalog/namespace/**`). Comparator sources are listed in §6.

**Evidence convention** (same as `open_dapr.md`):

- `path:line (verified)` — the cited lines were **opened and read in the authoring session** on this
  working tree. Safe to trust modulo later commits.
- `path:line (agent-read)` — read by a workflow subagent from the same tree, **not re-opened** by the
  author. An implementing agent MUST re-open these before changing code; line numbers may have
  drifted a few lines but the cited symbol/docstring is the anchor.
- `(spec)` — read from the vendored `lance_docs/`, which is this repo's pinned contract.

**Branch note.** Authored on `claude/catalog-layer-lance-comparison-cdfikh`. Nothing in this file
has been fixed yet; every finding describes the tree as of this commit.

**ADVERSARIAL VERIFICATION PASS — 2026-08-14, same day (run `wf_8943ea70`, 11 agents).** Every
finding was put through a skeptical re-read against the ACTUAL CODE (docstrings, comments, skills
and docs/ disallowed as evidence of behavior; every absence claim re-established by documented
grep), and the ten other `open_*.md` specs were scanned for overlaps, contradictions and binding
rulings. Outcome: **F1, F4, F5 CONFIRMED as written (F5 strengthened); F2, F3, F6, F7, F8, F10
CORRECTED in place** — the corrections are folded into each finding below, marked
`[verify]`, and the material ones are honest: F2's failure mode is a 503 (not 403) and its own
premise had trusted a drifted docstring; F3's comparator claim was wrong (rask HAS a
Lakekeeper-style creation guard on one door — it just doesn't cover the others and dies in the
same outage); F7 described a pre-2026-08-05 chart state; F6(c) is WORSE than filed (the leak is
invisible to the reconciler, the original acceptance criterion was vacuous). No finding was
REFUTED outright; F10.3's consequence was cut down to its real residual. Where this record
confirms a claim, the `(agent-read)` caveat on its citations is superseded. Cross-spec results
are in §5.5 — several findings are ALREADY FILED elsewhere (notably F1/F4 = open_python-audit.md
CAT-CORE-05) and two contradictions with other specs need resolving on THEIR side.

---

## §0 The question that was asked, and the answer

The question: *do the services here actually make up a catalog layer for lance-ns, and did we
design the catalog's state correctly?* — diffed against Lakekeeper's Generic Table API
(v0.13.0, PR lakekeeper/lakekeeper#1673), Unity Catalog (OSS + Databricks), and Apache Gravitino.

**The answer, condensed:**

1. **Yes, it is a real catalog layer.** 54/54 Lance Namespace ops routed
   (`tests/integration/test_spec_conformance.py` pins both halves), 48 backend-backed, 6 answering
   the `dir` backend's genuine stubs; plus ~88 extension routes forming the governance superstructure
   (projects, warehouses, members, grants, policies, protection, trash, vending, publication, model
   registry, control events). The Lance Namespace spec deliberately defines **nothing above the root
   namespace** — no tenancy, no authz semantics, no governance — and its
   `POST /v1/<object>/{id}/<action>` route grammar exists explicitly so a governing layer can
   authorize from the path without parsing bodies (`namespace.md:1110-1119` (spec)). rask's
   hierarchy + OpenFGA layer is the component the spec anticipates but does not define.

2. **Unlike every comparator, rask's catalog is format-AWARE and Lance-only by explicit policy.**
   It imports pylance in four modules (`services/dataplane.py:23` (verified), `services/models.py:24`,
   `api/v1/endpoints/credentials.py:19`, `core/namespace.py:11` (agent-read)), serves the data plane
   in-process (Arrow-IPC insert/query, blob Range/ETag streaming), and is a **governed commit
   coordinator** (`POST /v1/table/{id}/commit`, `data.py:331` (agent-read)). It 400s any non-Lance
   create: `data.py:96-108` (agent-read) — "this catalog stores Lance only". This is the exact
   inverse of Lakekeeper's Generic Table boundary ("no Lance in the catalog", commit coordination an
   explicit non-goal). Both are coherent positions; rask's buys authz + lineage + control events on
   every write at the cost of the catalog being an availability/OOM chokepoint on the
   read-modify-write and query paths.

3. **The no-relational-DB state design is CORRECT IN SHAPE.** Three independent facts support it:
   - **The commit-arbitration asymmetry.** Lakekeeper/Unity/Gravitino need a DB primarily because
     Iceberg puts the table-commit pointer IN the catalog (every commit is a CAS on that pointer,
     executed as a DB transaction). Lance puts the commit CAS in the object store itself —
     put-if-not-exists on `_versions/N.manifest` (`file_format.md`, Commit Protocol,
     ~:4763-4796 (spec)) — and `tests/e2e-py/test_object_store_cas_e2e.py` (verified) proves RustFS
     enforces it under 8-writer contention. The job that forces the comparators to run Postgres does
     not exist in a Lance catalog.
   - **The spec's own no-RDBMS reference design.** The Directory Catalog V2 keeps ALL catalog state
     in a storage-backed `__manifest` table (`namespace.md:966-1013` (spec)) — with the condition
     that uniqueness of its unenforced primary key "must be enforced via atomic conditional
     commits" (`namespace.md:996-999` (spec)). rask matches the shape and currently fails the
     condition (→ F1).
   - **Independent convergence.** rask's trash-keeps-tuples rule (revoking made undrop unreachable
     for exactly the owner) is byte-for-byte Lakekeeper's hard-drop-only tuple deletion; the
     credential-tier derivation (visibility gate, then `can_write_data` ⇒ RW / `can_read_data` ⇒ RO)
     is identical across rask, Lakekeeper, and Gravitino. Two systems reaching the same nonobvious
     answers independently is evidence the shape is right.

4. **The implementation is one primitive short of its own documentation** — and all three diff
   agents converged on the same list. The registry plane claims CAS and does plain overwrites (F1);
   the FGA dual-write has three non-retry-convergent failure states with a report-only reconciler
   behind them (F3); time-boxed grants are dead on the enforcement path (F2). None of the fixes
   requires a database.

---

## §0.5 Method — how the verdict was derived, and how to challenge it

The verdict is not a bug hunt and not a feature checklist. It rests on three legs, and an auditor
should attack each on its own terms:

1. **A requirements baseline from the format itself** (leg for "what must a Lance catalog own?").
   The `lance_docs/` scan established which responsibilities EXIST and which layer the format
   assigns them to: commit arbitration → the object store's manifest CAS (so a Lance catalog does
   not need a transactional DB for it — the neutralizer for "but Lakekeeper/Unity run Postgres");
   table state → the manifest (a catalog duplicating it mints a second source of truth); everything
   above the root namespace → deliberately unowned, with attachment points provided (path-grammar
   authz, `vend_credentials`, codes 15/16); registry uniqueness → "MUST be enforced via atomic
   conditional commits" (dir-V2). "Correct in shape" means each state class sits in a layer capable
   of owning it, judged against THIS ledger — not against any comparator.
2. **Comparator triangulation** (leg for "is the shape sound?"). Three researchers built dossiers;
   three SEPARATE diff agents each received rask's code analysis plus ONE dossier and produced an
   independent state-design verdict. Convergence was read as validation (rask independently
   reaching Lakekeeper's exact trash-keeps-tuples rule; the identical credential-tier derivation in
   all three systems); divergence was read as a tradeoff to cost both ways (→ §2), demoted to a GAP
   only where the comparator's feature covers a failure rask demonstrably has (Lakekeeper's
   creation cleanup guard vs F3). All three diff agents converged on "correct in shape, one
   primitive short" without seeing each other's output.
3. **Claims-vs-code tracing** (leg the findings came from). Every property the code, docstrings,
   and skills CLAIM was traced to whether the implementation delivers it: "CAS'd JSON registries" →
   every write path opened → overwrites (F1); the takeover-guard comments → the read-decide-write
   sequence → check-then-act (F1); the conditions apparatus → the `context` parameter traced grant
   to check → never passed (F2); "until reconciled" → the reconciler's repair path → report-only by
   AST gate (F3); "spec-correct 501" → the vendored spec → 406 (F8a). **Admission rule:** a
   candidate finding was dropped unless it produced a concrete failure scenario (specific inputs or
   interleaving → specific wrong outcome). That is why every P0/P1 above reads as a reproduction
   sketch, not a "there might be a race".

**Limits, stated plainly:** this is static analysis + spec reading + comparator research. Nothing
was executed against a live cluster in the authoring session; runtime facts cited (the store
honoring `If-None-Match:*`, OpenFGA's all-or-nothing batch Write) come from the repo's EXISTING
e2e suites and live-probed notes. The `(agent-read)` marker exists because subagent-cited lines
were not all re-opened by the author. Consequently: an implementer re-verifies Evidence first,
writes the failing test from the Failure scenario second, and fixes third — the acceptance
criteria are what convert this file's static claims into runtime truth. To CHALLENGE a finding,
attack its leg: show the spec assigns the responsibility differently (leg 1), show the comparator
premise is wrong from its cited sources (leg 2), or show the traced code path does deliver the
claim (leg 3).

---

## §1 Findings — each self-contained for audit + implementation

Severity: **P0** = security/correctness on tenant-isolation or authz paths. **P1** = real defect or
unpaid design bill. **P2** = drift/hygiene/deliberate-tradeoff gaps to confirm.

Every finding has: Evidence · Failure scenario · Comparator precedent · Fix specification ·
Acceptance criteria. An implementing agent should re-verify Evidence first (especially
`(agent-read)` lines), then write the failing test from the Failure scenario BEFORE the fix.

---

### F1 (P0) — Registry writes are unconditional overwrites; the "CAS'd JSON registries" claim is aspirational, and the TOCTOU windows sit on exactly the tenant-isolation guards

**STATUS: LANDED 2026-08-14 (same session as the verification pass).** Shipped:
`service_kit.lakehouse.records.create_json` — the ONE conditional-create seam (S3: boto3
`put_object(IfNoneMatch="*")`, 412 → `RecordExistsError`; local FS: `open(..., "xb")`, same
exactly-one-winner semantics so unit tests prove the door logic without object storage); the
warehouse id-mint (`create_warehouse_record` + endpoint re-read-and-re-guard on a lost race,
including the same-project convergence carrying the winner's `status` — the racing-create half of
F4's quarantine rule), the write-once binding (`bind_namespace` now refuses a conflicting bind AT
THE STORE, idempotent on identical re-bind), and the project mint (`create_project_record` +
convergence on the winner's identity fields). Tests: `tests/unit/test_registry_cas.py` (seam +
door semantics, 10 tests), two endpoint race tests in `tests/integration/test_warehouses.py`
(guard-blinded first read — the exact TOCTOU — refused 409 by store arbitration; same-project race
converges 200 without resurrecting a deactivated warehouse), and the live contended half
`tests/e2e-py/test_registry_cas_e2e.py` (`cas` marker, 8-way barrier race, exactly-one-winner —
the silent-ignore detector). Skill corrected in the same commit.
**Residuals, deliberately NOT covered here:** (a) the cross-ID bucket-claim race — two creates
with DIFFERENT warehouse ids claiming the same bucket create two distinct records, so id-keyed
conditional creates cannot arbitrate it; closing it needs a bucket-keyed claim record
(`_warehouses/bucket-claims/<bucket>.json`), which should be designed WITH #85's four-store
collapse, not before it; (b) trash/protection creates stay unconditional — they move with F6's
record-first reordering; (c) mutable-field RMW (status/protection toggles) is F4's ETag seam,
unchanged. The original finding text below is kept for the record.

**Evidence.**
- `services/catalog/src/catalog/services/warehouses.py:78-83` `_write_json` (verified via
  `put_warehouse` docstring at :96-97 "overwrite — create is idempotent"): plain
  `pyarrow open_output_stream` PUT, no `If-None-Match`, no ETag.
- `services/catalog/src/catalog/services/projects.py:33-40` `put_project` (verified): delegates to
  the same `_write_json`, "overwrite — create is idempotent".
- `packages/service-kit/src/service_kit/lakehouse/protection.py` `set_protection` and
  `.../trash.py` `put` (agent-read): same unconditional overwrite shape.
- The guards that depend on read-then-write being atomic, all in
  `services/catalog/src/catalog/api/v1/endpoints/warehouses.py` (verified 2026-08-14):
  - **:154-156** — cross-tenant warehouse-id takeover guard (`existing.get("project") != project`
    → 409). The comment block above it (:148-153) documents the exact takeover it closes — but only
    against sequential attempts.
  - **:170-172** — cross-project bucket-claim guard (`projects_claiming_bucket(...) - {project}`).
    Same shape: read the registry, decide, then overwrite.
  - **:441-443** — binding write-once guard (`existing_binding != root_uri` → 409). The comment
    (:436-440) itself narrates the disaster: tenant B binds tenant A's namespace name, A's tables
    become unreachable, A's new writes land in B's bucket, "positive-cached-forever routing makes
    replicas disagree".
- The store PROVABLY honors put-if-not-exists: `tests/e2e-py/test_object_store_cas_e2e.py`
  (verified) — tier 1 asserts a second `If-None-Match: *` PUT of a live key is rejected (412);
  tier 2 is an 8-thread contended-key stress asserting exactly one winner. **But no registry write
  path uses it** — the primitive is exercised only by Lance's own manifest commits.
- The overstated claim to correct: `.claude/skills/rask-lance-catalog/SKILL.md`, Storage table —
  "project registry …, warehouse registry … | JSON records on the control root, **CAS'd conditional
  writes (the `cas` e2e marker proves the primitive)**" (verified). The e2e proves the STORE
  supports the primitive; it does not prove the registries use it. They don't.
- Multi-replica is a supported config: the #46 broadcast-eviction machinery and the chart guard
  `services.catalog.replicas > 1 requires catalog.controlEmit` exist precisely because >1 catalog
  replica is expected (`chart/templates/services.yaml` (agent-read); skill §gotchas).

**Failure scenario (concrete).** Two admins (or one admin + one retrying GitOps controller) POST
`/v1/warehouses` with the same `warehouse_id` under different projects, hitting two replicas.
Both `get_warehouse` reads return `None`; both pass the :154 guard; both `put_warehouse`. Last
writer wins silently — the loser's project now believes it owns a warehouse whose record names the
winner's project, and `seed_warehouse` has written BOTH projects' FGA edges
(`fga_deps.py:995-1027` (agent-read)), which is precisely the cross-tenant disclosure the guard's
own comment describes. The binding variant (:441) is worse: concurrent binds of one `top_ns` to two
warehouses both pass, the overwrite re-routes tenant A's tables to tenant B's bucket, and the
forever-positive `warehouse_binding_cache` (`api/dependencies.py:33-98` (agent-read)) pins the
wrong answer per replica.

**Comparator precedent.** Lakekeeper: uniqueness is a Postgres constraint inside a transaction
(shared `tabular` table; migration `20260529000000_add_generic_table.sql`). Gravitino: atomic
check-and-insert in the JDBC entity store. The Lance spec's own dir-V2:
"implementations MUST enforce [object_id uniqueness] … atomically check if the table already exists
… as well as if any concurrent operation writes the same entry" (`namespace.md:996-999` (spec)).
rask is the only one of the four whose id-minting is check-then-act.

**Fix specification.**
1. Add a conditional-write mode to the registry write helper (one seam:
   `_write_json(..., if_none_match: bool = False)` or a sibling `_create_json`), implemented with
   the S3 `If-None-Match: *` conditional PUT (boto3 `put_object(..., IfNoneMatch="*")` — pyarrow's
   fs API cannot express it, so this write goes through the boto3 client the service already builds
   for `provision_bucket`, `warehouses.py:35-50` (agent-read)).
2. Use create-mode (conditional) writes at the four id-minting doors:
   `_projects/<id>.json` (first create only — the idempotent re-POST path re-reads and may then
   overwrite-with-precondition, see 3), `_warehouses/<id>.json` (when `existing is None`),
   `_warehouses/bindings/<top_ns>.json` (always — bindings are declared write-once), and
   `_trash/<key>.json` / `_protection/<key>.json` creates.
3. A lost race surfaces as the spec's own error: catch the 412 and raise
   `ConcurrentModification` (code 14 → 409) or the relevant `*AlreadyExists` — never a bare 500.
   NOTE the spec-doc trap: `lance-dir.md`/`lance-rest.md` say "return error code 12" for
   version-CAS conflicts, but 12 is `TableColumnNotFound`; 14 is correct (§4, spec-bug list).
4. Idempotent same-payload re-create must STAY convergent: on 412, re-read; if the existing record
   is semantically identical (same project/bucket for a warehouse; same warehouse for a binding),
   return success as today; if different, 409.
5. Update `.claude/skills/rask-lance-catalog` in the same commit (CLAUDE.md rule: skills drift is
   fixed with the code): the Storage row's claim becomes true rather than being deleted.

**Acceptance criteria.**
- New unit tests: two concurrent creates of one warehouse id under different projects → exactly one
  201, one 409 (thread the race with a barrier around the conditional PUT, or fake the 412).
- New e2e (extend `test_object_store_cas_e2e.py` or a sibling with the `cas` marker): registry
  create path against the DEPLOYED RustFS, contended 8-way, exactly one winner — mirroring tier 2
  but through `put_warehouse`/`bind_namespace`.
- Existing idempotent re-POST tests (`warehouses` endpoint suite) stay green unchanged.
- `grep -rn "open_output_stream" services/catalog/src/catalog/services/{projects,warehouses}.py`
  shows no unconditional write on a create path.

---

### F2 (P0) — Time-boxed grants are dead on the enforcement path: `_require` never passes `context`, and a conditional tuple without context is a DENY

**Evidence.**
- `services/catalog/src/catalog/api/fga_deps.py:274-285` (verified): `_require` calls
  `await fga.check(client, user=user, relation=relation, obj=obj)` — no `context` kwarg. Same for
  `_require_any` (:287) and the `fga.list_objects`-based list filters
  (`namespaces.py:484-519`, `tables.py:166-170` (agent-read)).
- `packages/service-kit/src/service_kit/governed/fga.py:400-411` (verified), `check` docstring:
  "Omitting it against a conditional tuple is not 'no opinion', it is a DENY — OpenFGA cannot
  evaluate the CEL expression without its parameters — so a caller that forgets the context sees a
  time-boxed grant as already expired."
- The model condition: `non_expired_grant(current_time, grant_time, grant_duration)` on use rungs
  (`model.fga:419-439` (agent-read)); written via the estate-admin tuple editor with parameter
  validation (`access_admin.py:237-255` (agent-read)).
- The ONE caller that does it right: the access simulator, `access_admin.py:347` (agent-read),
  passes context.

**Failure scenario `[verify]` — CORRECTED: the symptom is a 503 authz-outage, not a 403.** Estate
admin writes `reader` on `table:acme$bronze$pages` for `user:eve` with a live window via
`POST /v1/access/tuples`. The simulator says ALLOW. On every real route, OpenFGA cannot evaluate
the CEL condition without `current_time` — it does NOT return `allowed=false`, it FAILS the query
with a 400 evaluation error (OpenFGA docs; openfga/openfga#1511; rask's own
`access_admin.py:243-246` comment states exactly this). rask's `check()` catches that via
`_FAIL_CLOSED` (`fga.py:100, 444-446` (verified by pass)) and raises `ServiceUnavailableError`, so
`_require` audits FAILURE `authz_unavailable` and the route answers **503**. Consequence is worse
than the original filing: writing a time-boxed grant MANUFACTURES authz-outage symptoms for that
subject (503s + FAILURE audit rows that look like an OpenFGA incident), and a `list_objects`
evaluation error can break the LIST endpoints for them entirely. The feature is still write-only.
Note for the record: the original finding's "DENY" premise came from `fga.py:403-408`'s check()
docstring, which is itself drifted about OpenFGA semantics — fix that docstring in the same
commit (a verifier catching the auditor trusting a docstring is exactly why §0.5's rules exist).

**Fix specification `[verify — scope enlarged]`.** The library wrappers must be extended FIRST:
`batch_check`, `list_objects` and `list_users` in `service_kit/governed/fga.py` accept **no
`context` parameter at all** (`fga.py:449-476, 479-517, 525-588` (verified by pass)), so a helper
in `fga_deps` alone cannot reach them. Then thread `context={"current_time": <RFC3339 now>}`
through every enforcement call: `_require` (:277), `_require_any` (:300), the grant-surface
pre-checks (:459, :467, :530, :536, :541), and every listing filter (`credentials.py:65`,
`tables.py:167`, `namespaces.py:517`, `models.py:100`, `warehouses.py:230,299`, `me.py:88,108`,
`access.py:114,170,233` — the pass's complete enumeration). Centralize the clock in ONE helper so
no call site can forget it — the `_audit_tuples`-inside-the-library pattern. Constraint from
§5.5: the provision-side test `test_fga_provision.py` pins that the conditions block is written —
the fix must not break it. Alternative (if the feature is judged unwanted): delete the condition
from the model + tuple-editor validation and document per-object grants as permanent — but note
the model-edit gate in §5.5 (no `fga` CLI in-sandbox) applies to that route.

**Acceptance criteria.**
- Integration test (FGA on, real OpenFGA container as in the existing fga suites): write a
  conditional reader tuple with a live window → `GET /v1/table/{id}/describe` 200; with an expired
  window → 403. Both must go through the real router gate, not `POST /access/check`.
- List-filter twin: a live-window reader sees the table in `ListTables`; expired does not.

---

### F3 (P0) — Three partial-failure states are not retry-convergent, and the reconciler is forbidden (by test) from repairing them

**Evidence.**
- Create-orphan: table create runs native write THEN `seed_ownership`
  (`tables.py:189-229` + `fga_deps.py:803-829` (agent-read)). If the seed 503s (OpenFGA outage),
  the retry hits native `TableAlreadyExists` → 409 and NEVER re-reaches the seed. The object has
  neither owner nor parent edge (they were one batch), so it is invisible to every cascade and every
  list. `data.py:249-250` (agent-read) documents the residual crash window on the data-plane create
  path too.
- Stale-tuples-after-drop: `fga_deps.py:851-853` (agent-read), in the code's own words: "Revoke runs
  AFTER the (irreversible) native mutation, so on an OpenFGA outage it fails closed (503) with the
  tuples left stale **until reconciled**." Retrying the drop 404s at the native layer — the revoke is
  unreachable. The hazard is id-reuse privilege bleed, which `fga.py:1245-1250` (agent-read) states
  is the reason revoke exists.
- Unbound-namespace: warehouse-scoped namespace create lands natively, `bind_namespace` fails; plain
  retry 409s at native `NamespaceAlreadyExists`; the documented recovery is the caller knowing to
  pass `adopt_existing=true` (`warehouses.py` endpoint :459-470 (agent-read)) — a migration flag
  doing double duty as an undocumented repair path.
- The reconciler CANNOT fix any of these: report-only by contract, enforced by an AST gate test
  (`tests/unit/test_reconcile_report.py`, referenced from `maintenance` purge module docstring
  (agent-read)); only the expired-trash purge holds a revoke path.

**Failure scenario.** OpenFGA has a 30-second blip during a medallion bronze-table create. The
dataset exists, is listed by no one (no tuples → per-item list filtering hides it from every
caller), cannot be dropped through the API by its creator (no `can_drop`), and no automated process
will ever repair it. Operator remediation today: hand-written tuples via the estate-admin editor —
the exact "ghost" class `scripts/seed_estate.py` was built to prevent.

**Comparator precedent `[verify]` — CORRECTED: rask HAS mitigation (a), on one door.** The
original claim "two mitigations rask lacks" was wrong: the Arrow create door carries a
Lakekeeper-style compensating cleanup (`data.py:255-266` (verified by pass) — on seed failure it
revokes tuples then natively drops the fresh table, gated by `_compensation_allowed`,
`data.py:70-79`, fresh-id only, logged if the cleanup itself fails). Two things keep the finding
alive: (i) the guard exists ONLY there — `declare_table` (`tables.py:207-208`), `register_table`
(`:461-462`) and `undrop` (`:545-548`, where `trash.clear` at :547 runs BEFORE the seed, so a
failed seed also destroys the retry's trash record) are all bare native-then-seed; and (ii) the
compensation's FIRST step is itself an FGA call (`revoke_ownership` → `read_object_tuples`), which
raises against the same down OpenFGA before the native drop is ever reached (retry budget: 3
attempts, 0.2s backoff — a 30s blip is not ridden out), so the stranded state is reachable even on
the guarded door. Also verified: NO retry mode converges on the Arrow door by design —
`exist_ok` deliberately skips the seed (anti-seizure, `data.py:255`) and `overwrite` 403s at
`require_can_drop_table` (`:217`) because the creator holds no tuples. Lakekeeper's second
mitigation — a write-capable `openfga reconcile` CLI (`add-missing`/`add-and-delete-drift`,
`--dry-run`) — rask genuinely lacks. `[verify]` scoping: the stale-tuples-after-drop state applies
to the DESTRUCTIVE drop path; a trashed drop defers its revoke to the purge, which IS an
automated cron-retried path (`purge.py:339`) — but the destructive path is the code default
(`trash_grace_days=0` in code; the chart ships 7, see F7) and always the `purge=true` path.
`adopt_existing` is in the public OpenAPI schema and generated TS client, but description-less
and framed only as bytes-first migration, never as bind-failure repair (`schemas.py:620-628`) —
"undocumented as a repair path" is the accurate form.

**Fix specification** (either/both; (a) is smaller, (b) is more general):
1. **Compensating cleanup on create.** If `seed_ownership` fails after a native create, best-effort
   native drop of the just-created object (ONLY when the create was fresh — never after an
   Overwrite that replaced history; `data.py:70-79` (agent-read) already draws this exact
   distinction for its own compensation). Log loudly if the cleanup itself fails.
2. **A write-capable structural reconcile.** New maintenance mode (separate endpoint + explicit
   opt-in flag, mirroring the purge's posture): for each native object missing its `parent`/owner
   edges, rebuild the STRUCTURAL tuples (parent, child) from the registry/native tree and seed
   owner to a configured fallback (project admin), never touching user grants; for each tuple set
   on a native-absent object (and not in `_trash/`), revoke via the existing
   `revoke_object_tuples`. Keep the AST gate for the REPORT module; the repair lives in a new
   module the gate doesn't cover, with its own `--dry-run`-style preview.
3. Adopt the retry-convergence test discipline: for each of the three doors, a test that kills the
   FGA write on attempt 1 and asserts attempt 2 CONVERGES (object usable or object gone — either is
   fine, half-states are not).

**Acceptance criteria.** The three scenario tests above; plus the drop docstring's "until
reconciled" becomes true (points at the new repair mode) or is reworded.

---

### F4 (P1) — No lost-update detection on registry read-modify-write: a quarantine can be silently lifted

**Evidence.** `services/catalog/src/catalog/services/warehouses.py:177-186` (verified):
`set_warehouse_status` = `get_warehouse` → mutate dict → `put_warehouse` (unconditional). The
idempotent re-create path (`endpoints/warehouses.py:181-187` (verified)) carries `status` forward
from ITS OWN earlier read — the comment even names the hazard it defends against ("a GitOps
reconcile … would otherwise silently lift a quarantine"), but defends only the sequential case.

**Failure scenario.** t0: GitOps re-POST of warehouse `acme-wh` reads record (status=active).
t1: operator `POST /v1/warehouses/acme-wh/deactivate` writes status=deactivated.
t2: the re-POST's `put_warehouse` lands, writing the record it built at t0 — status=active. The
quarantine is lifted with no `/activate` call and no audit signal: the exact outcome the
carry-forward exists to prevent, via interleaving instead of omission.

**Fix specification.** Version every mutable registry record: add a monotonically increasing
`"version": n` (or store the S3 ETag read alongside), and make `put_warehouse`-for-existing /
`set_warehouse_status` / protection toggles / trash mutations conditional
(`If-Match: <etag>` — RustFS supports conditional PUT per the cas e2e; verify `If-Match` support
in the same e2e before relying on it, else read-back-verify + retry loop). On precondition failure:
re-read, re-apply the mutation on the fresh record, retry (bounded), surfacing
`ConcurrentModification` after N attempts.

**Acceptance criteria.** A test interleaving deactivate between a re-create's read and write —
final status MUST be `deactivated`. Same shape for `protected` on projects
(`projects.py:37-39` (verified) carries `protected` forward with the same pattern).

---

### F5 (P1) — The flagship credential-isolation e2e cannot run against the real endpoint: key-name and shape mismatch

**Evidence.**
- Producer: `services/catalog/src/catalog/core/vending.py:210-218` (verified) — vended
  `storage_options` keys are `access_key_id`, `secret_access_key`, `session_token`, `region`
  (bare, Lance-style — matching `DescribeTableResponse.storage_options` being "passed directly to
  Lance", `namespace.md:3822` (spec)).
- Response shape: `CredentialResponse{mode, credentials: {storage_options, expires_at_millis},
  location, read_version}` — `schemas.py:636-647` (agent-read).
- Consumer: `tests/e2e-py/test_credential_isolation_e2e.py:101-116` (verified) — takes
  `body["credentials"]` and reads `creds["aws_access_key_id"]` / `creds["aws_secret_access_key"]` /
  `creds.get("aws_session_token")`. Two mismatches: it never descends into `storage_options`, and
  it uses `aws_`-prefixed names the vendor never emits. First real run → `KeyError`.
- Why it was never caught: the suite is env-gated (`make e2e-isolation`, needs two tenants' tokens
  + a vending-enabled deployed stack) and skips by default; CI wiring is tracked as #84 (skill,
  agent-read).

**Consequence.** The #74 tenant-isolation claim is currently proven ONLY by the offline
IAM-semantics policy evaluator (`tests/unit/test_vending.py:174-228` (agent-read)) — which
validates the policy DOCUMENT rask builds, not the store's enforcement of it. RustFS's actual
session-policy fidelity is untested anywhere that runs.

**Fix specification `[verify — strengthened]`.** In the e2e: `creds =
body["credentials"]["storage_options"]` and switch to the bare key names (mirror `_client` kwargs;
note line 113's `creds.get("region")` also misses — region lives inside `storage_options`, so even
a patched key lookup silently defaults to us-east-1). Additionally: **`make e2e-isolation` does
not exist** — the test's own docstring (line 19) and this file's earlier text cite a Makefile
target that no Makefile defines (verified: e2e targets are `e2e-ci`/`e2e-ray-ci`/`e2e` only), so
the fix must also CREATE the target (or wire the suite into an existing one). Then run it once
against a deployed vending-enabled stack, record the result here, and wire it to CI (#84).
Placement constraint from §5.5: `services/catalog/tests` is NOT in the root `testpaths` — any new
regression pin must land in a collected path (`tests/unit/test_vending.py` is the natural home)
or add its directory to `testpaths` in the same commit.

**Acceptance criteria.** The e2e passes against a deployed vending-enabled stack; a deliberate
sabotage run (widen the session policy to the bucket root) makes the cross-tenant GET assertion
fail — proving the test can detect the failure it exists for.

---

### F6 (P1) — Trash-plane incoherences: silent orphan window, doors that disagree, a leaked binding, and the sweep rewriting frozen data

Four related defects; can be one work item.

**(a) Crash window leaves a table invisible to BOTH undrop and purge.**
Evidence: recoverable drop = `describe → deregister → trash.make_record → put`
(`tables.py:341-351` (agent-read)) — non-atomic, no ordering protection. Crash after deregister,
before `trash.put`: bytes exist, registry says gone, no trash record → undrop 404s
(`tables.py:530-537` (agent-read)), purge sees nothing, only the report-only, default-off orphan
scan could ever notice.
Fix: write the trash record FIRST (a trash record for a still-registered table is harmless — undrop
re-registration is `exist_ok`/recovered-idempotent per the #96 semantics), THEN deregister; or a
two-phase `state: detaching → detached` field on the record. Acceptance: kill-between-steps test
converges on retry.

**(b) Declared-only tables: the two drop doors disagree.**
Evidence: single-table drop with grace ON requires `described.location` truthy to trash — a
declared-only table (no location) falls through to the DESTRUCTIVE native drop
(`tables.py:340-353` (agent-read)); the cascade path handles declared-only explicitly with a
`location=''` record (skill #96 (verified against skill); `namespaces.py` (agent-read)).
Fix: unify — single-door drop of a declared-only table with grace on writes the same `location=''`
record. `[verify]` Note the cascade UNDROP currently SKIPS declared-only records with a warning
(`namespaces.py:395-406`, `undrop_skipped_declared_only_table`) rather than re-declaring — so
either implement re-declare on undrop for both doors, or document the record as informational-
only; today's cascade record is effectively the latter. Acceptance: drop+undrop a declared-only
table through BOTH doors with grace on; no destructive native call observed on either; undrop
behavior identical on both.

**(c) The purge leaks warehouse bindings — `[verify]` WORSE than filed: the leak is INVISIBLE.**
Evidence: recoverable cascade deliberately KEEPS the top-level binding so undrop can route
(`namespaces.py:311-312` (verified by pass)); the purge walk is check → revoke → delete_location →
`trash.clear` → emit, with NO unbind anywhere (`purge.py:354-430`; `grep -rn unbind
services/maintenance/src/` → zero hits — documented absence). The original claim that this is
"the exact class `dangling_bindings` reports" was WRONG: `reconcile.py:398-403` defines
`dangling_bindings` as bindings whose **warehouse_id has no warehouse record** — the cascade→
expire→purge leak leaves the warehouse record intact and only the namespace gone, and NO
reconciler category compares a binding's `top_ns` against live namespaces (verified against every
`top_ns` use in reconcile.py). The leaked binding is therefore undetected by anything. This also
falsifies `open_dapr.md §4.1`'s "every failure leaves a RECOVERABLE state / idempotent-retry-to-
completion" claim for the purge — residue exists that nothing retries (→ §5.5 contradictions).
Fix: purge of a namespace-kind trash record whose id is a bound `top_ns` unbinds after the last
record of that subtree is reclaimed (emitting `namespace_dropped` so #46 evicts caches); AND add a
reconciler category (`orphaned_bindings`: binding whose `top_ns` names no live namespace) so the
class is at least visible if the fix regresses.
Acceptance `[verify — original criterion was vacuous]`: cascade-drop a bound top-level namespace
with grace on → expire → purge → assert `_warehouses/bindings/` no longer contains the `top_ns`
entry (asserting "reconciler reports zero dangling_bindings" passes even WITH the leak — do not
use it).

**(d) The sweep keeps maintaining trashed datasets. `[verify: CONFIRMED]`**
Evidence: a trashed table is deregistered but its bytes keep their `_versions/` marker in a swept
bucket; discovery (`discover_datasets`, `optimize.py:68-101` — the name `discover_dataset_uris`
survives only in the stale module docstring at `optimize.py:3`) is purely marker-based, excluding
only `__`-prefixed dirs and the control registries; it consults neither trash records nor
`__manifest`. Every discovered URI reaches `compact_one` (`sweep.py:232`), whose only skips are
policy-disabled/interval and feature-flag refusals; the sweep's sole trash contact is the
report-only expiry log. So `compact_one` keeps compacting AND `cleanup_old_versions`
(cleanup_enabled default True) keeps deleting version history of data the owner believes frozen
pending undrop.
Fix: sweep loads the trash index for each bucket's control root and SKIPS any dataset URI matching
a live trash record (report them as their own summarize() line, like refusals — never silently).
Acceptance: sweep tick over a bucket containing a trashed dataset → dataset untouched, counted in
the new line.
Related (smaller): a trashed multi-base table's purge deletes only the recorded primary root; F9's
asset-record work should record base URIs so purge can at least NAME what it did not delete.

---

### F7 (P1) — The shipped default posture exercises almost none of the safety apparatus, and the fleet bypasses vending entirely

**Evidence `[verify — two claims corrected, rest confirmed exact]`:**
- ~~`LANCE_TRASH_GRACE_DAYS` default 0 → drops destroy immediately~~ **CORRECTED: the DEPLOY path
  ships a 7-day grace.** `chart/values.yaml:731` sets `trashGraceDays: 7` and
  `chart/templates/services.yaml:77` renders `LANCE_TRASH_GRACE_DAYS=7` even when the key is
  missing (`hasKey|ternary` with a hardcoded 7 fallback) — fixed 2026-08-05 per the values.yaml
  comment; the original claim described the pre-fix state. The code default 0 (`config.py:81`)
  bites only bare, chart-less runs (dev-micro, tests).
- `vending.mode` default `mode_b` (`config.py:246`, `chart/values.yaml:692-693`) → NO scoped
  credentials exist; **confirmed including prod** — grep of `values-prod.yaml` /
  `values-local.yaml` / `values-live-pins.yaml` finds no overlay overriding it. The #74 isolation
  apparatus is dormant in every shipped posture.
- `MAINTENANCE_TRASH_PURGE_ENABLED` default False (`maintenance config.py:141`,
  `values.yaml:957`); orphan scan default False (`config.py:160`). Confirmed.
- `warehouses_enabled` default False (`config.py:71`, `services.yaml:82`) →
  `require_warehouse_scoped` is a documented no-op. Confirmed.
- ~~`fga_lock_root_create` off → open self-serve~~ **CORRECTED: prod already locks it.**
  `chart/values-prod.yaml:20-22` sets `auth.enabled: true` + `lockRootCreate: true` ("prod: a
  token alone can't create root namespaces"), rendered by `services.yaml:153`. Only the base/dev
  posture leaves it off, and the template comment documents that as deliberate ("Off in dev
  (self-serve); ON in prod"). The open-create gate itself is `api/fga_deps.py:269-271` as claimed.
- The fleet holds static root S3 keys — **confirmed and completed by the pass**: medallion
  (`MEDALLION_S3_ACCESS_KEY_ID`, `config.py:209-222`; chart injects `rustfs.accessKey` at
  `medallion.yaml:179,313`), maintenance (`config.py:82`, default literally `rustfsadmin`;
  `maintenance.yaml:118`), lineage (`config.py:126-129`; `services.yaml:314`), viewer via
  `service_kit.media` (`media/config.py:78-80`; `explorer.yaml:200`). With `secrets_from_dapr`
  the plaintext moves to OpenBao but it is the same static root key. Credential-level tenant
  isolation exists only for EXTERNAL clients, on an opt-in path. Databricks ships vending as THE
  access mechanism; Gravitino's model has every consumer (incl. GVFS) on vended creds.

**This is a decision list, not a bug list — and `[verify]` two of the four decisions are already
made.** Each default is individually reasoned (mode_b is the RustFS-without-STS reality). The
remaining asks: (1) ~~prod grace days~~ DONE (7d since 2026-08-05); (2) prod vending =
web_identity? (RustFS-native, chart wiring exists, `services.yaml:104-111`) — still mode_b
everywhere, the REAL remaining posture gap; (3) ~~`fga_lock_root_create` in prod~~ DONE
(values-prod); (4) a roadmap item for fleet services consuming vended per-tier creds instead of
root keys (the service-door identity plumbing exists, `security.py:78-160`). Constraint from
§5.5: `open_ingest_design.md §2` records a RULING against building human-vs-service tier guards
into the write doors — posture hardening is tuple-seeding and values policy, not new code guards.
Also §5.5: the fleet-key redesign and `open_gateway.md` Phase 2 (dapr-api-token guard fate) are
the same decision and must be made together.

---

### F8 (P2) — Spec-conformance corrections

**(a) `Unsupported` must map to 406, not 501.**
Evidence: `packages/service-kit/src/service_kit/lakehouse/ns_errors.py:25` (verified):
`ErrorCode.UNSUPPORTED: 501`, docstring calls it "a spec-correct 501".
The vendored spec disagrees twice: `spec.yaml` declares
`406: UnsupportedOperationErrorResponse` on routes (e.g. :117-118 (verified)), and
`ns_catalog/namespace/supported-catalogs/lance-rest.md:1002` (verified): "Error code `0`
(Unsupported) maps to HTTP `406 Not Acceptable`".
Impact is soft (spec clients dispatch on the numeric code) but generated clients special-casing
406-vs-501 will misclassify the 6 stub ops.
Fix: change the map entry to 406; update `_UNREDACTED_5XX` (`ns_errors.py:87`) — 406 is 4xx so the
redaction question disappears; update the docstring, `docs/COVERAGE.md`, and the skill ("answer a
spec-correct 501" → 406) in the same commit. `tests/unit/test_ns_errors_contract.py` pins the map —
update its expectation deliberately.
Caveat to record: the spec declares 406 on only a handful of routes; using it estate-wide for
declined ops is the spec's *intent* (code 0) even where the route table omits it — note this in the
map's comment. `[verify]` The contract test's pin is behavioral, not a map-value assertion
(`test_ns_errors_contract.py:48` asserts `status == 501` through `problem_detail`), and a
repo-wide grep for `406` across `packages/` + `services/catalog` returns zero code hits — the fix
introduces the status, it doesn't redirect an existing one. Adjacent per §5.5:
`open_python-audit.md` catalog-api-10 files a sibling misuse (501-vs-400 on the access surfaces)
— land both inside its E4 one-error-taxonomy consolidation, not as a lone patch.

**(b) Spec self-contradictions to pin in tests** (so an implementing agent doesn't "fix" rask to a
buggy prose line) — all (spec), from the `wf_88eb56e8` scan:
- `errors.md` lists codes 0-21; the ErrorResponse model defines 24 (22 `TableBranchNotFound`,
  23 `TableBranchAlreadyExists`). Implement against the model. (rask already does — the branch
  codes landed 2026-08-04.)
- `lance-dir.md:306,332` / `lance-rest.md:510,581` say "return error code 12
  (TableVersionAlreadyExists)" for CreateTableVersion conflicts — **code 12 is
  TableColumnNotFound**; there is no TableVersionAlreadyExists. Correct surfacing of a lost
  version-CAS: 14 ConcurrentModification → 409.
- DropNamespace mode=Fail prose demands HTTP 400 (and Skip → 204) against the error model's
  NamespaceNotFound=1 → 404. Follow the error model.
- `ListTablesResponse` description says recursive; both implementation specs return DIRECT children
  (recursive is `ListAllTables`). Follow the implementations.
- Two context→header conventions coexist (`x-lance-ctx-<key>` in rest/index.md vs the `header.<name>`
  literal mapping in the Context schema, `spec.yaml:2461-2480`). The `header.*` form lets a request
  BODY inject literal headers including `header.Authorization` — the gateway/catalog MUST allowlist
  forwarded context headers and canonicalize identity from headers only. Verify rask's handling
  (`core/` context plumbing) explicitly rejects/strips `header.Authorization` from bodies.
- CreateTable's REST binding contradicts itself between rest/index.md (location/properties in
  `x-lance-table-*` headers) and spec.yaml (JSON-encoded query params). rask follows spec.yaml;
  note that interop clients may send the other form.

**(c) The describe-path vend is unaudited.**
Evidence: bespoke `/credentials` audits every direct issuance (#41,
`credentials.py:102-110` (agent-read)); the spec path `describe?vend_credentials=true`
(`tables.py:295-301` (agent-read)) issues the same class of credentials with no audit event.
Fix: emit the same `vend_credentials` audit row (tier=read, path=describe) from the describe vend.
Acceptance: audit stream shows one row per issuance on both paths.

---

### F9 (P2) — No generic/opaque asset rung; the model registry squats on the `table` type

**Evidence `[verify — one detail corrected]`.** The Lance-only gate: `data.py:96-108` — right
call for the DATA plane. The model registry authorizes model objects as `table:models$<model>`
(`endpoints/models.py:5-13, 51-62`; `fga_deps.py:907` for can_promote); `list_artifacts` is the
one plain-path, non-Lance surface (`services/models.py:155-174` — "the tree is OUTSIDE any Lance
dataset by design"); promotion correctness leans on "last row of the registry dataset" +
"compaction preserves rows and their order" (`services/models.py:6-10, 69, 197`) — a convention,
not a key. CORRECTION: the "listable-but-ungovernable" case is a bare directory under
`models_root` that is not a readable Lance dataset (lists with null versions,
`endpoints/models.py:66-74`; promote 404s via `_open`). A bare ARTIFACT tree is not even that —
artifact trees live under the SEPARATE `model_artifacts_root` (`config.py:136-147`), which
`list_models` never enumerates, so it is invisible to the listing rather than listable. The
essence stands: half-plain-file assets governed under the `table` type, with the visible/
promotable boundary drawn by implementation accident rather than by an asset model.

**Comparator precedent.** This is EXACTLY Lakekeeper's Generic Table (name + format tag + location
+ properties + opaque blobs; identity/authz/vending/lifecycle; NO data ops — and their reuse
finding was that the catalog machinery needed almost nothing new) and Gravitino's fileset (named
pointer + vended creds + zero content parsing) / model catalog (sequential versions, movable
aliases, named multi-URI maps). Both prove the rung is cheap when the governance plane is already
format-agnostic — and rask's is (JSON registries + FGA + protection + trash carry no Lance
dependency; `projects.py`/`warehouses.py` import only `pyarrow.fs` (verified)).

**Fix specification (design item — needs owner ratification, mirrors the lance-ns-merge doc
process).** A first-class `asset` kind: control-root record {id, format (validated lowercase tag),
location, properties, doc}, its own FGA type (owner/writer/reader + manage_grants — NOT `table`),
create/describe/list/deregister(+drop with the same trash semantics), vending against its location
prefix, protection + trash reuse as-is. NO data ops, NO schema interpretation — Lakekeeper's
boundary, coexisting with the Lance-only table rung. First consumer: the model registry's artifact
trees (fixes the type-squat and gives model versions a keyed home instead of last-row-order).
Relevant for the archives estate beyond models: EAD files, IIIF sidecars.

---

### F10 (P2) — Smaller confirmed drift/hygiene items (one line each, all need re-verify at fix time)

1. Two project-id regexes: `CONTROL_ID_RE` (`catalog/core/identifiers.py:32-33`, lowercase 3-63,
   `\Z`-anchored) vs `PROJECT_PATTERN` (`service_kit/lakehouse/warehouse_registry.py:37`,
   `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`) (agent-read) — the exact single-constant lesson
   `identifiers.py` records, recurring across the package boundary. Fix: one constant exported from
   service-kit, both import it.
2. `fga.py:3-6` docstring says "nine types"; `model.fga` defines ten (`annotation_project`)
   (agent-read). One-line fix.
3. Binding cache is forever-positive with best-effort broadcast eviction over a ring-buffer
   transport with no dead-letter (`api/dapr.py:64-80`). `[verify — consequence CORRECTED]`: a
   dropped `warehouse_deleted` event does NOT leave a replica routing at a deleted bucket —
   `dependencies.py:75-97` reads warehouse STATUS live on every request and a missing record
   fails closed to 403. The real residual: persistent 403s on a since-re-bound namespace until
   restart, wrong-bucket routing only during the partial-delete window (record still present) or
   under warehouse-id reuse. Fix stands (TTL floor, minutes) but is P2-sized, matching the
   corrected consequence.
4. Trash-window privilege bleed: grants stay live on a freed id (correct, #75), so a same-id
   RE-CREATE during the grace window inherits the old table's tuples, and undrop seeds the
   UNDROPPER as owner (`tables.py:517-548` (agent-read)) — an implicit ownership transfer. Fix
   options: block same-id create while a live trash record exists (409 naming the trash deadline +
   undrop/purge as the outs — cheapest and most honest), or revoke-on-recreate.
5. Undrop ignores expiry: an expired-but-not-yet-purged record still undrops
   (`maintenance/core/config.py:133` notes it (agent-read)) — the `/tasks` deadline overstates
   finality. Decide: honor expiry at undrop (409 past deadline) or document the deadline as
   "purge-eligible", not "gone".
6. Purge control events reuse `table_dropped`/`namespace_dropped` with `extra.reason=trash_expired`
   (`purge.py:342-351` (agent-read)) — consumers attribute expiry purges to a user drop until the
   vocabulary regen adds `*_purged` actions (`make openapi` + `gen:types:catalog` +
   `test_openapi_contract`, per the skill's wire-contract rule).
7. `[verify — CORRECTED, exposure narrower]` The sweep's `asyncio.Lock` single-flight claims
   cluster-wide validity via `compactionReplicas=1` (`routes.py:41-49`) — but that values key
   **exists nowhere in the chart** (the comment is itself drift), and `maintenance.yaml:47`
   HARDCODES `replicas: 1`, so scaling is not reachable through values at all — only via kubectl
   scale or a template edit. Today the lock actually holds (this matches `open_dapr.md §3`'s
   reading). Fix: `open_batch_process.md B13` already PLANS the right shape — invariant tests
   binding each lock-as-cluster-lock to its replica count, landing with the transform build;
   coordinate there rather than adding a render guard for a key that doesn't exist. Also fix the
   routes.py comment.
8. `[verify — CORRECTED, fix half-exists]` ListUsers truncates silently at the server cap 1000
   (`fga.py:520-522, 583-588` — log.warning only). The proposed `truncated: true` flag ALREADY
   EXISTS on the admin door (`AccessListUsersResponse.truncated`, `schemas.py:218`, set at
   `access_admin.py:431`); the gap is only the per-object `/access/list` review
   (`AccessListResponse`/`RelationGrants`, `schemas.py:27-36` — no flag). Fix: add the same flag
   there; the admin door is the pattern to copy.
9. `revoke_object_tuples` reconstructs only the inverse `child` edge; any future model shape where
   a dropped object appears as USER on another object would survive drops
   (`fga.py:1267-1277` (agent-read)). Guard: a model-contract test asserting no such shapes exist,
   so adding one forces revisiting revoke.
10. `_collect_descendants` recurses unbounded (no depth cap) while the listing walk caps at
    `_MAX_NAMESPACE_DEPTH=8` (`tables.py:74` vs `namespaces.py` (agent-read)) — a pathological tree
    is bounded in one walker, a stack overflow in the other. Align the bound.
11. **Grant PROVENANCE is not recordable** — distinct from the lineage plane, which is healthy.
    Lineage (dataset derivation: OpenLineage events → AGE) answers "how did this data come to be";
    provenance (attribution/custody: who granted, who acted, under what authority) is scattered
    across #41 audit rows, `TupleOrigin`, and registry `created_by` — and its weakest link is that
    an OpenFGA tuple cannot carry its grantor: `can_revoke_grant` had to be made manage_grants-only
    for exactly this reason (`model.fga:191-204` (agent-read)), and `read_changes` cannot attribute
    actors (`fga.py:849-855` (agent-read)). Consequence: an access review can enumerate WHO HAS a
    grant but not WHO GAVE it, except by correlating the OpenFGA changelog against the estate's
    audit stream by timestamp. Fix options (design decision): a `granted_by`/`granted_at` sidecar
    record per grant on the control root (same shape as protection records), written by the
    /access/grant door in the same request; or accept and DOCUMENT the correlation procedure in the
    openfga skill so reviews have a sanctioned method. Comparator note: Lakekeeper has the same
    tuple limitation and leans on audit events as the history — nobody has solved this inside
    OpenFGA itself.

---

## §2 What rask should NOT change (deliberate divergences, confirmed against the comparators)

Recorded so a future audit doesn't "fix" them:

1. **No Postgres for the catalog.** See §0.3. The comparators' DB is load-bearing for Iceberg
   commit arbitration rask doesn't need; registry frequency is admin-scale; the spec's dir-V2
   sanctions storage-backed catalog state. The bill is F1/F4, payable with conditional PUTs.
2. **Format-awareness and the Lance-only gate** (`data.py:96-108`). Refusing a Delta create instead
   of accepting an opaque pointer it cannot govern is honesty; Gravitino's External/Unparsed
   passthrough shows the governance-theater alternative. The generic-asset need is real but it is a
   NEW rung (F9), not a loosening of the table rung.
3. **Trash keeps tuples** (#75). Independently identical to Lakekeeper's hard-drop-only tuple
   deletion. Keep; fix only the re-create bleed (F10.4).
4. **The 403-before-existence posture with per-door exceptions** (pre-authz 404 for projects, the
   no-existence-oracle collapse on destructive doors). More nuanced than Lakekeeper's uniform
   404-hides-existence; each choice is documented in-code. Keep, but the three postures deserve one
   paragraph in the skill so operators can reason about them.
5. **Vending returning `read_version`** — one round trip gives a client-direct writer scoped creds
   AND its optimistic-commit base. No comparator has this; it is the best part of the /commit
   design. Keep.
6. **The earned purge** (clean-report gate + five-rung per-record refusal ladder + revoke-before-
   bytes). Far stronger than Lakekeeper's straight recursive delete. Keep; F6(c) adds the missing
   unbind, F6(d) stops the sweep fighting it. The known cost — one drift finding blocks ALL
   reclamation estate-wide — is a deliberate posture; revisit only with owner sign-off.
7. **Web-identity vending** (caller's own OIDC token exchanged BY the store; boot-refusal guard
   against posting it to public AWS STS, `config.py:324-337` (agent-read)). Stronger chain than any
   comparator's catalog-held-key AssumeRole. Keep; make it the prod default per F7.

---

## §3 Comparator dossiers (condensed; full agent output in the session scratchpad)

### Lakekeeper (Generic Tables, v0.13.0, PR #1673)
Rust + Postgres-only. Generic table = a third `tabular_type` in the SHARED `tabular` table
(+ `generic_table` / `generic_table_properties` satellites, format regex `^[a-z][a-z0-9_-]{0,63}$`,
schema/statistics opaque JSONB ≤1MB, metadata_location constrained NULL) — soft-delete, undrop,
protection, rename, task-queue purge all inherited for free. OpenFGA type
`lakekeeper_generic_table`, ~16 actions; visibility gate `get_metadata` → cannot-see = 404
uniformly. Dual-write: DB tx commits first, FGA second, `GenericTableCreationGuard` deletes the
row on FGA failure; `lakekeeper openfga reconcile` repairs drift. Vending: same tiering as rask;
plus per-request S3 remote signing (no STS endpoint needed — the middle option between rask's
mode_b full proxying and full STS). Explicit non-goals: no commit coordination, no format libraries,
no schema enforcement. **Endpoints are Lakekeeper-proprietary** (`/lakekeeper/v1/...`), NOT Lance
Namespace — rask serves the actual Lance spec; a lance-ray/lancedb client can speak to rask
natively but needs bespoke client code for Lakekeeper generic tables.

### Unity Catalog
Two incarnations, keep them straight. **Databricks-hosted**: metastore>catalog>schema>securable;
managed vs external tables; VOLUMES (governed arbitrary files — the closest thing to F9);
registered models first-class; row filters + column masks (sub-table authz rask has no story for);
UNDROP 7-30d; per-object downscoped temp credentials gated by a DISTINCT privilege
(`EXTERNAL USE SCHEMA` — the "may hold raw creds" vs "may query" split rask lacks: both ride
`can_read_data`/`can_write_data`); commit-coordinating Iceberg REST for managed Iceberg (the same
architectural move as rask's /commit). **OSS**: thin single server, Hibernate over H2/PG/MySQL,
static per-bucket credential vending (isomorphic to rask's `StaticPrefixVendor`, same weaknesses),
hard deletes, no undrop. **Lance story: none native** — the lance-namespace-impls Unity adapter
registers Lance datasets as EXTERNAL tables with `data_source_format=TEXT` +
`properties['table_type']='lance'`, `managed_versioning=false`: an opaque pointer, strictly weaker
than what rask provides.

### Apache Gravitino
Metalake>catalog>schema>{table,fileset,topic,model}; JDBC entity store (H2 dev / MySQL / PG prod);
federated catalogs read-through ("what you see is what is there now" — proof that governance-store-
vs-substrate eventual coherence is a legitimate architecture, which is rask's posture too).
jCasbin RBAC with ALLOW/**DENY** (DENY beats ALLOW — an exception/lockout primitive OpenFGA and
therefore rask cannot express) + `X-Gravitino-Active-Roles` per-request narrowing + Ranger
pushdown. FILESETS + GVFS = the format-blind governed-directory rung (F9 precedent); MODEL catalog
= keyed sequential versions + movable aliases (the fix shape for rask's last-row convention).
Credential vending per-catalog provider SPI incl. IRSA; a GENERIC endpoint vends for ANY metadata
object. **Lance: first-class but metadata-only** — a `lakehouse-generic` catalog (Lance Java lib in
core, schema auto-detect, `lance.schema-refresh-mode` reconciling out-of-band writers) plus a
standalone Lance REST service implementing the same Namespace spec rask does; no alter, no
partitioning, no indexes, no data plane. rask is a working implementation where Gravitino is a
registry of one.

---

## §4 Spec contracts an implementer must hold in mind (from the lance_docs scan)

1. **Vending wire contract** (spec): `vend_credentials` on DescribeTable/DeclareTable; credentials
   INSIDE `storage_options` with bare Lance keys; `expires_at_millis` for temporary creds
   (`namespace.md:3477,3796,3822`). DescribeTable is the de-facto refresh path — keep it cheap and
   side-effect-free. rask conforms (read-tier only, deliberate; pinned in
   `tests/integration/test_vending_endpoint.py:75-101`).
2. **Consumers are STATIC** (spec/SDK): sync SDK and every lance-ray op snapshot `storage_options`
   into Ray workers; the only refresh hook (`AsyncTable.latest_storage_options`) is flagged
   internal. Vended TTLs must cover the longest expected job, or mid-job auth failures are
   accepted. Also: the merged creds dict is serialized into Ray task specs — vended secrets appear
   in Ray's object store; scope TTLs accordingly.
3. **lance-ray merges caller storage_options with namespace-returned ones with UNDOCUMENTED
   precedence** (`ray.md:298,397`) — never rely on vended options winning; enforce with the session
   policy on the credential itself (rask already does — keep it that way).
4. **base_paths/multi-base**: a manifest may reference bytes OUTSIDE the table root (tiering,
   shallow clones, branches, blob-v2 `base_store_params`). Prefix-scoped vending breaks those
   tables. rask's `_has_external_bases` → `mode=server_mediated` fallback
   (`credentials.py:76-81,123-133` (agent-read)) is the correct honest answer — preserve it in any
   vending change, and extend the same awareness to purge (F6 tail) and orphan scanning (already
   done — the scan refuses flags 16/64).
5. **managed_versioning** is the sanctioned switch that redirects clients from storage-direct
   commits to catalog version ops (External Manifest Store role, incl. reader-side crash repair,
   `file_format.md:5406-5435`). rask's `/commit` is a bespoke sibling. If "every write governed"
   ever becomes a requirement, this is the spec-shaped door — and it is a catalog-WIDE toggle in
   the dir reference, not per-table.
6. **Batch atomicity honesty**: `BatchCreateTableVersions` / `BatchCommitTables` demand multi-table
   atomicity at the metadata layer. Single-object CAS cannot provide it; rask's 501 (→406 after
   F8a) stubs are the honest answer. Do not fake these with sequential writes.
7. **`_transactions/*.txn` are load-bearing** (commit retry reconstruction + conflict detection,
   `file_format.md:4783-4790`); they accumulate by design and nothing prunes them (known,
   `orphans.py:16-19`). Any future pruning must tie retention to manifest retention, never age.
8. **V2 manifest naming is inverted** (`u64::MAX - version`, zero-padded): lexicographically FIRST
   = NEWEST. Any sweep/catalog logic assuming ascending V1 names computes "oldest" backwards on V2
   tables. Detect per table.

---

## §5 Recommended implementation sequence

Ordered by value-per-effort; each item is independently landable.

| # | Item | Findings | Size | Notes |
|---|---|---|---|---|
| 1 | ~~Conditional PUT on id-minting registry writes + skill correction~~ **LANDED 2026-08-14** (see F1 STATUS; residuals: bucket-claim record w/ #85, trash/protection w/ F6) | F1 | done | closes the CAT-CORE-05 mint half; the RMW half is row 4 |
| 2 | **WRAPPER HALF LANDED 2026-08-14** — `condition_context()` (one clock helper) + `context` accepted and forwarded by `check`/`batch_check`/`list_objects`/`list_users`, clock defaulted in so a caller that forgets it no longer sees a live grant as expired. **REMAINING: the catalog call-site half** (`_require` still passes no context) — deferred, `services/catalog` was being edited by another session. CORRECTION to this row's premise: the model tests were NOT missing — `model.fga.yaml` already asserted both sides of the window, the cascade, and migration-safety; a direct TABLE-rung conditional test was the one real gap and is now added (38/38, 208/208 checks). | F2 | S left | wrapper + tests done; call sites when catalog is quiet |
| 3 | Fix + run the isolation e2e; wire to CI (#84) | F5 | S | shape + key names + region; CREATE the missing make target; sabotage-run proof; pin lands in a collected testpath (§5.5.7) |
| 4 | ETag/version-conditioned read-modify-write on mutable records | F4 | M | status, protection, trash; bounded retry |
| 5 | Dual-write repair: create-side compensating cleanup + write-capable structural reconcile | F3 | M-L | Lakekeeper's guard + reconcile pattern; keep report AST gate on the report module |
| 6 | Trash-plane coherence: record-first ordering, declared-only unification, purge unbinds, sweep skips trashed | F6 | M | four small fixes, one theme |
| 7 | `Unsupported` → 406 + docs/skill/COVERAGE updates | F8a | S | update the pinned contract test deliberately |
| 8 | Describe-path vend audit row | F8c | S | |
| 9 | Posture decision: prod vending + fleet-on-vending roadmap | F7 | owner | grace-days and root-create lock are ALREADY DONE in the chart (`[verify]`); remaining: vending mode in prod + fleet static keys — decide together with `open_gateway.md` Phase 2 (§5.5) |
| 10 | Generic `asset` rung design (models first consumer) | F9 | L | needs owner ratification; Lakekeeper/Gravitino precedent in §3 |
| 11 | Hygiene batch | F10.1-10 | S each | independent one-liners |

---

## §5.5 Cross-references to the other `open_*` specs (verification pass, 2026-08-14)

The ten sibling working specs were scanned in full. (**`OPEN-WORK.md` — the durable register, a
different genre — was MISSED by this pass** because it doesn't match the lowercase glob; it was
scanned separately the same day and its results are §5.6. Read §5.6 too: it contains a pre-filed
twin of F3's fix and an owner ruling that collides with F9.) Three kinds of result; an implementer MUST
read this section before starting any F-item, because several fixes are already filed, planned,
or ruled on elsewhere.

### Already filed elsewhere — coordinate, do not re-file

- **F1 + F4 = `open_python-audit.md` CAT-CORE-05** ("Control-plane registry writes are plain
  overwrites with no compare-and-swap", `warehouses.py:78, :177` — filed med/E3, execution wave 2)
  plus SKG-08 (the four hand-rolled record registries as one family) and SKG-09 (records as
  unvalidated dicts). This file's contribution on top: the TOCTOU-on-TENANT-ISOLATION-GUARDS
  framing and failure scenarios, which CAT-CORE-05's lost-update framing doesn't carry. The
  audit's Appendix C ordering (wave 1 = E1+E2+E9 first) applies — slot the F1 fix into wave 2
  rather than forking the ordering. Machine-readable twin: `open_python-audit.findings.json`.
- **F3 partial**: SKG-01 (swallowed-rejection → stale grant + false SUCCESS audit) and SKG-02
  (partial-batch audit skip) file adjacent FGA write-path defects, and SKG-01's fix is
  PRESCRIPTIVE — use OpenFGA `ConflictOptions(on_duplicate_writes=IGNORE, ...)` so the SERVER
  decides idempotency, and never audit an unconfirmed write. F3 remediation must build on that,
  not invent a parallel classifier.
- **F10.7 = `open_dapr.md §3`** (phantom `compactionReplicas` key, adjudicated: lock holds today,
  the defect is drift) **+ `open_batch_process.md` B13** which already PLANS the invariant tests —
  land there.
- **F10 items also filed**: ListUsers truncation = SKG-04; binding-cache unbounded global =
  SKG-11 (and `tests/unit/test_binding_cache_eviction.py` already pins part of the eviction
  contract); unbounded cascade recursion = catalog-api-07; F8a's territory = catalog-api-10
  (sibling 501-vs-400 misuse) → ride E4's one-taxonomy consolidation.
- **Two FGA defects near this file's scope are already adjudicated OPEN and unfixed**
  (`open_dapr.md` HANDOFF 2026-08-10): a manage_grants-only principal can self-grant owner, and
  the inverse `child` edge is never backfilled on the pre-existing estate (upward visibility inert
  there). F10.9's guard-test proposal should reference, not re-discover, these.

### Contradictions to resolve — on THEIR side or ours, explicitly

- **`open_projects.md §4.5`** claims the id regex is "single-sourced (identifiers.py:31-33)" under
  its do-not-re-file list. F10.1 shows a second, divergent pattern at
  `warehouse_registry.py:37` (verified, both patterns quoted in F10.1). open_projects' claim is
  scoped to the catalog service and misses the service-kit copy — correct that entry when F10.1
  lands.
- **`open_dapr.md §4/§4.1`** asserts the purge leaves "a RECOVERABLE state" on every failure
  ("idempotent-retry-to-completion, not forward-then-undo"). F6(c) falsifies this for bindings:
  the cascade→expire→purge path leaves residue nothing retries AND nothing reports. §4.1's
  disposition ("reopen the day trashPurge is enabled") now has a second reason to reopen.
- **`open_batch_process.md §6`** (Rejected list) states "writes are server-side, FGA-gated,
  CAS-guarded" as achieved posture — overstated per F1; its B8 build ("`_transforms/` records
  under the control root … CAS'd") assumes a CAS seam that does not exist yet. B8 should either
  land after F1's conditional-write seam or build it.
- **`open_python-audit.md`'s maintenance section** treats the report-only reconciler as a pinned
  design property (AST-gate test praised, reconciler defects filed WITHIN the report-only
  contract). F3's repair-path proposal deliberately challenges that boundary — the resolution
  recorded here: keep the REPORT module report-only and its AST gate; the repair lives in a NEW
  module/endpoint the gate does not cover (F3 fix spec option 2 already says this).

### Binding rulings and constraints an implementer inherits

1. **FGA model edits are gated and the gate is unreachable in-sandbox** (`open_projects.md §6`):
   `fga model test` needs the `fga` CLI, which cannot be installed here (release binary 403s
   through the proxy). Affects F2's delete-the-condition alternative, F9's new type, F10.2's
   docstring-vs-model fix if resolved model-side. Model changes are owner-executed.
2. **"Never write FGA tuples — report the exact missing tuple and STOP"** is a standing agent
   constraint (`open_batch_process.md §7`). F3's write-capable structural reconcile and any
   child-edge backfill are therefore OWNER-EXECUTED migrations; an agent may build the tool, not
   run it against the estate.
3. **Root-create lock trap** (`open_projects.md §5`, quoting `model.fga:82-85`): with
   `fga_lock_root_create` on, a missing parent relation is an OpenFGA 400 → fail-closed 503 for
   EVERYONE, and `transaction` declares `parent: [namespace, warehouse]` so every new `can_create_*`
   must exist on BOTH types. Any F9 type addition or F7 posture change must audit the full
   parent-relation matrix first — this is the same 400→503 failure class F2 documents.
4. **F9's design space is pre-constrained by three recorded rulings**: (a) the model already
   documents WHY a dedicated type was rejected for `store` (`model.fga:100-105` — nothing would
   seed the tuples for code-defined defaults; `open_projects.md §4.4` argues this does NOT
   transfer to types needing one seeded tuple — engage that argument, don't repeat it); (b) a new
   type must define the `_GRANTABLE_BASE` rungs (owner/writer/reader/validator) or it is
   ungrantable through `/access/grant` (`open_projects.md §4.1`); (c) the annotator/assist plane
   resolves models via RAY SERVE discovery, bindingly ("there is no second model plane",
   `open_assist_discovery.md:7-9,102-107`) — an F9 asset rung must not assume the assist plane as
   a consumer, and must preserve `models$<name>` addressing + the version==Lance-commit
   crash-safety (`open_anno_active.md:266-269, 483-495`) that the train-loop work builds on.
5. **The existence-oracle ruling is settled** (`open_ingest_design.md §1d`, "recorded here so it
   is not re-litigated"): describe AND exists both 403 on an absent table; `_create_empty` is the
   only door that answers does-this-table-exist. F8c's audit fix must not alter describe/exists
   semantics — the ingest ensure-sequence and the annotator publish design depend on them.
6. **Where audit belongs** (`open_medallion_workflow.md §4.2`, ruling): durable decisions are
   recorded in LINEAGE facets, not retention-bounded stores. F8c's describe-vend audit should
   follow the existing #41 audit stream convention, and anything promoted to "durable decision
   record" status belongs in lineage.
7. **Test-wiring hazard** (`open_python-audit.md` X2/CAT-CORE-03): `services/catalog/tests` and
   `services/lineage/tests` are ABSENT from root `testpaths` — suites placed there are silently
   inert. Every acceptance test in this file must land in a collected path or extend `testpaths`
   in the same commit. (F5's pin → `tests/unit/test_vending.py`.)
8. **Lint blind spot** (`SKG-14`): the whole `lakehouse/**` scope sits under a blanket 21-rule
   ruff exemption — F6/F10 hygiene fixes there are unlinted until that exemption is lifted;
   don't mistake a clean `make lint` for coverage.
9. **In-flight collisions**: `open_batch_process.md` B8 adds `_transforms/` records under the same
   control root (coordinate with F1's seam); the bulk grid's act-first ontology PATCH
   (`open_bulk_active.md §5`, landed 2026-08-09) sits on the annotations table, which is
   catalog-governed — F1/F4/F6 semantic changes land under a live save wire whose OCC pattern
   (`base_version` → 409 → re-fetch) is ALSO the estate's precedent for F4's
   conditional-read-modify-write shape. Reuse it conceptually.
10. **Dapr-CAS caveat if any fix uses Dapr state instead of raw S3** (`open_dapr.md §2.7`,
    CLOSED): an ETag alone is IGNORED under Dapr's default concurrency — `concurrency: first-write`
    must be explicit. The registry CAS in F1 uses raw boto3 `If-None-Match` and does not inherit
    this, but F4 implementers considering the landed UserStateStore pattern do.
11. **Sandbox verification limits** (`open_anno_active.md §0`): full pytest hangs on Dapr actor
    suites in cloud sandboxes (use targeted `uv run pytest tests/unit/...`); no helm binary; no
    k3s/Dagger; some egress blocked. State scope-cuts explicitly when verifying F3/F6/F7 fixes
    here — don't let "couldn't run it" read as "verified".

### Scope confirmation

`open_anno_active.md`, `open_assist_discovery.md`, `open_bulk_active.md` file NOTHING in this
file's scope (annotator/assist/bulk planes; governance explicitly out of scope for them) and
contradict nothing — checked, including the near-miss on annotation-row provenance vs F8c
(different planes). The specs' own lifecycles: all are self-deleting working docs; references
into them will dangle as their backlogs drain — cite the finding ids (CAT-CORE-05, SKG-01, B8,
B13) alongside file names so the trail survives.

## §5.6 Cross-references to `OPEN-WORK.md` — the durable register (scanned 2026-08-14, full 3582 lines)

**Why this file exists and how the conventions relate** (its own words): `open_*.md` files are
WORKING plans under the fold-then-delete convention — "when the work lands, what is still live
moves here and the file goes" (`OPEN-WORK.md:1221-1223`); `OPEN-WORK.md` is "THE one register"
(header, `:1-9`), created because lance-ns tracked open items as session task-ids that died with
sessions; "not deletable; reconciled at P8, never dropped" (`:1209-1216`). **Consequence for this
file: when the F-items land, their live residue folds INTO OPEN-WORK — and must RECONCILE the
overlaps below, not duplicate them.** Staleness note: the header says "Status as of 2026-07-27"
but sections I/J are amended through 2026-08-11; the FOLDED trackers (RASK-INTEGRATION,
ASSESSMENT, BENCH, `:1350+`) are explicitly pre-merge records — historical rulings, not
current-tree facts.

### Already filed in the register — reconcile, don't duplicate

- **E3 #11 (`:767-768`) IS F3's fix option 2**, filed 2026-07-27 from the same Lakekeeper study:
  "Reconcile-from-catalog — additive FGA rebuild + opt-in drift deletion with dry-run; absent."
  F3's write-capable structural reconcile has been on the register all along — implement it AS
  #11, citing both.
- **I.4 #84 (`:1182-1183`) is F5's CI half** — "credential attack in CI (needs web-identity
  vending + a second tenant admin)" — and names web-identity vending as prerequisite, tying F5's
  completion to F7's row-9 decision.
- **I.6 #85 (`:1200`) — "collapse the four control-root JSON stores"** — the registries F1/F4
  harden are already slated for consolidation. The CAS seam and the collapse must be ONE design;
  building the seam per-store and then collapsing builds it twice.
- **mode_b vending is a WRITTEN OWNER DEFERRAL** — "Written deferrals (don't re-litigate,
  schedule): … mode_b vending" (`:2092-2093`), and E3 #14 (`:775-777`) carries the conditional
  follow-on (`/refresh-credentials`, only if STS/web-identity enabled). F7 row 9 is therefore
  "schedule an already-deferred decision", not a new thread. The fleet-on-vended-creds end-state
  is ALSO already the stated seam contract: "workload identity … vends short-TTL, table-scoped
  creds via `POST /v1/table/{id}/credentials` (web_identity flow). No durable secret on compute"
  (`:1986-1989`).
- **F1's primitive is pre-proven in the register**: "RustFS conditional-PUT … VERIFIED 2026-08-03
  — it IS enforced … second `If-None-Match: *` PUT rejected with PreconditionFailed" (`:525-533`).
- **F6's baseline contract** is the live-driven #96 closure (`:1167-1173`, cascade trash/undrop,
  tuples KEPT — #75's rule at subtree scale) + #75 (`:1137-1139`); F10.4/F10.5 are residuals of
  that rule and any fix must preserve it. **#79's ordering ruling** (`:1188-1197`): trash purge
  FIRST, gated on a clean drift report — constrains when F6(c)'s unbind fix can be exercised live.
- **F9's first consumer is deprioritized by ruling**: "Models registry MLflow parity (was #101) —
  Deprioritized until after the product pass" (`:629`).

### Contradictions with the register — resolve explicitly when landing

- **F9 vs the folded BENCH rulings — the sharpest collision in either direction.** BENCH records:
  generic-table registration "out of scope (Lance-only) … deliberate non-goal" (`:2155-2161`);
  "UC volumes as ungoverned file dirs — do NOT build — blob-v2 in-table is the deliberate
  alternative" (`:2199-2207`); "no in-scope catalog feature is missing" (`:2211-2219`). These are
  2026-07-22 rulings against approximately F9's precedents. Arguably distinguishable — BENCH
  retired generic tables as FOREIGN-CATALOG INTEROP, F9 proposes a rung for rask's OWN non-Lance
  assets — but F9's owner-ratification step MUST engage `:2201-2203` head-on or it will
  (correctly) be rejected as re-litigating a recorded scope decision.
- **F8a vs RASK-INTEGRATION decision 3** (`:3009-3014`): "the 7 genuinely backend-stubbed ops …
  stay 501 until the upstream Rust DirectoryNamespace implements them" — enshrines the wrong
  status per the spec's 406 mapping. When F8a lands, correct this register entry AND
  `docs/COVERAGE.md` in the same commit.
- **F10.3 vs #46's closure** (`:1178-1179`): "scale-into-staleness is now a render error, not a
  runtime surprise" is over-broad — the render guard closes only the controlEmit-off case; a
  dropped broadcast event still yields persistent 403s until restart (F10.3's verified residual).
- **F6(a)/(b) vs #96's unscoped phrasing**: #96 was driven on the CASCADE door only; the entry
  reads as trash-plane coherence achieved, but the single-table door retains the crash window
  (F6a) and destructively drops declared-only tables the cascade records (F6b). Scope the entry
  when F6 lands.

### Prerequisites and in-flight collisions from the register

- **E3 #12 — URL-encode FGA subjects (`:762-765`)**: "mandatory before prod OIDC … OIDC subjects
  here are emails. Smallest and sharpest of the set." Touches every tuple write F3 reworks — do
  #12 first or fold it into F3's door changes; do not let them race.
- **E3 #9 — versioned authz-model migration (`:765-767`)**: the missing machinery for F2's
  delete-the-condition alternative AND F9's new type. Without it, model changes are raw
  owner-executed edits (§5.5.1's CLI gate on top).
- **E3 #10 — split tuple helpers + golden tuple tests (`:769-770`)**: collides with F3's
  seed/revoke changes; land the golden tests with (or before) F3.
- **E3 #2 — credentials-vs-config response split (`:771-772`)**: touches the exact response shape
  F5's e2e parses — coordinate or the e2e is fixed against a shape about to change.
- **E3 #3 — request_id/actor propagation (`:772-774`)** is adjacent to F10.11's grant-provenance
  gap ("record it or build it").
- **Section J environment facts for F2** (`:3482-3582`): OpenFGA v1.18.3 with
  `weighted_graph_check` ON via `OPENFGA_EXPERIMENTALS` (and the `experimentals:` chart key
  BREAKS the whole render — touch with care); model audited weighted-graph compatible; the
  provision half of conditions was already fixed once ("fga.provision silently dropped
  model['conditions'] … every FGA-enabled service fail-closed 503", `:297-303`) and
  `test_fga_provision.py` pins it — the estate has ALREADY witnessed the 400→503 class F2
  describes, fleet-wide.
- **Registry-plane neighbors of F1/F4**: #48 warehouse-delete partial-failure honesty, #67
  ghost-projects migration, #43 separate-rustfs-instances (`:1198-1200`).
- **F6(d)'s code neighbor**: #62 couples to §H1 (`:1071-1097`) — per-request `lance.dataset()`
  opens + cache ceilings ~17× pod limits, "confirmed live" — the same open-path the sweep fix
  touches.

### Updated start-order consequence (supersedes the §5 note where they differ)

Nothing here blocks §5 rows 1–8 outright, but three couplings tighten: (1) F1's seam should be
co-designed with #85's four-store collapse; (2) F3's door changes should carry #12 (subject
encoding) and #10's golden tests; (3) F5's e2e fix should be aware of #2's pending response-shape
split. F9 now has TWO gates: the §5.5 rulings and the BENCH scope decision above.

## §6 Sources

- rask working tree, branch `claude/catalog-layer-lance-comparison-cdfikh` (all `file:line`).
- Vendored `lance_docs/`: `namespace.md`, `file_format.md`, `lance_sdk.md`, `ray.md`,
  `ns_catalog/spec.yaml`, `ns_catalog/catalog/{dir,rest}/index.md`,
  `ns_catalog/namespace/{index,object-relationship}.md`, `operations/{index,errors}.md`,
  `supported-catalogs/*.md`.
- Lakekeeper: docs.lakekeeper.io (generic-tables, concepts, authorization-openfga,
  generic-table-open-api.yaml), PR lakekeeper/lakekeeper#1673, migration
  `20260529000000_add_generic_table.sql`, `crates/lakekeeper/src/server/generic_tables/*.rs`.
- Unity: unitycatalog/unitycatalog (auth.md, configuration.md), docs.unitycatalog.io,
  docs.databricks.com (securable-objects, credential-vending, filters-and-masks, undrop,
  object-storage-lifecycle, iceberg), lance-format/lance-namespace-impls `docs/src/unity.md`.
- Gravitino: gravitino.apache.org docs (overview, filesets, model-catalog, access-control,
  credential-vending, lakehouse-generic-catalog, lance-rest-service,
  how-to-use-relational-backend-storage), apache/gravitino repo (core connector SPI,
  catalog-lakehouse-generic, `libs.lance` in core).
- Workflow runs `wf_ee7e7283` (11 agents) and `wf_88eb56e8` (4 agents), 2026-08-14; full structured
  agent output preserved in the session scratchpad (`rask_facets.md`, `cmp_{lakekeeper,unity,
  gravitino}.md`, `lance_docs_scan.md`).
- Published summary artifact: https://claude.ai/code/artifact/3cc1014b-c875-48c4-aa9c-fbf7a26eeb9b
