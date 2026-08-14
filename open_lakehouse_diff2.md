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

**Failure scenario.** Estate admin writes `reader` on `table:acme$bronze$pages` for `user:eve` with
`grant_time=now, grant_duration=86400` via `POST /v1/access/tuples`. The simulator says ALLOW. Every
real route — describe, query, credentials — 403s for the entire window, because `_require`'s check
evaluates the conditional tuple with no `current_time`. The feature is write-only: it can be
granted, validated, simulated, audited — and never authorizes anything.

**Fix specification.** Thread `context={"current_time": <RFC3339 now>}` through every enforcement
check: `_require`, `_require_any`, `batch_check` call sites, and the `list_objects`/`list_users`
listing filters (OpenFGA accepts context on all of these). Centralize in `fga_deps` (one
`_now_context()` helper) so no call site can forget it — the same "inside the library so no write
site can skip it" pattern `_audit_tuples` already uses (`fga.py:1042-1055` (agent-read)).
Alternative (if the feature is judged unwanted): delete the condition from the model + the
tuple-editor validation and document per-object grants as permanent — a dead apparatus that LOOKS
like it works is the worst of the three states.

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

**Comparator precedent.** Lakekeeper, same dual-write, two mitigations rask lacks: (a) a
`GenericTableCreationGuard` — DB row committed first, and if the subsequent OpenFGA write fails the
guard DELETES the just-committed row (compensating cleanup, logged if even that fails); (b) a
write-capable `lakekeeper openfga reconcile` CLI (`add-missing` / `add-and-delete-drift`,
`--dry-run`) that rebuilds structural parent/child edges from Postgres while preserving grants.

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

**Fix specification.** In the e2e: `creds = body["credentials"]["storage_options"]` and switch to
the bare key names (mirror `_client` kwargs accordingly). Then actually run it once against a
deployed stack (`make e2e-isolation`) and record the result here; then wire it to CI (#84).

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
record (undrop of it = re-declare). Acceptance: drop+undrop a declared-only table through BOTH
doors with grace on; no destructive native call observed on either.

**(c) The purge leaks warehouse bindings.**
Evidence: recoverable cascade deliberately KEEPS the top-level binding so undrop can route
(`namespaces.py:311` gates unbind on `not recoverable` (agent-read)); the purge deletes bytes →
tuples → records but never touches `_warehouses/bindings/` (`purge.py:354-431` walk
(agent-read)). A cascaded-then-expired namespace therefore leaves a dangling binding — the exact
class the reconciler's `dangling_bindings` category reports, re-created by rask's own lifecycle.
Fix: purge of a namespace-kind trash record whose id is a bound `top_ns` unbinds after the last
record of that subtree is reclaimed (and emits `namespace_dropped` so #46 evicts caches).
Acceptance: cascade-drop a bound top-level namespace with grace on → expire → purge → reconciler
reports zero `dangling_bindings`.

**(d) The sweep keeps maintaining trashed datasets.**
Evidence: a trashed table is deregistered but its bytes keep their `_versions/` marker in a swept
bucket; `discover_dataset_uris` is marker-based (`optimize.py:52-101` (agent-read)), so
`compact_one` keeps compacting AND `cleanup_old_versions` keeps deleting version history of data
the owner believes frozen pending undrop.
Fix: sweep loads the trash index for each bucket's control root and SKIPS any dataset URI matching
a live trash record (report them as their own summarize() line, like refusals — never silently).
Acceptance: sweep tick over a bucket containing a trashed dataset → dataset untouched, counted in
the new line.
Related (smaller): a trashed multi-base table's purge deletes only the recorded primary root; F9's
asset-record work should record base URIs so purge can at least NAME what it did not delete.

---

### F7 (P1) — The shipped default posture exercises almost none of the safety apparatus, and the fleet bypasses vending entirely

**Evidence** (all agent-read against config/chart):
- `LANCE_TRASH_GRACE_DAYS` default 0 → drops destroy immediately (`config.py:81`).
- `vending.mode` default `mode_b` (`chart/values.yaml:692-693`) → NO scoped credentials exist;
  the entire #74 isolation apparatus is dormant unless web_identity/sts is opted into.
- `MAINTENANCE_TRASH_PURGE_ENABLED` default off; orphan scan default off.
- `warehouses_enabled` default off (`config.py:71`) → `require_warehouse_scoped` is a no-op.
- `fga_lock_root_create` off → top-level namespace/table create is OPEN self-serve: any
  authenticated caller, no FGA decision at all (`fga_deps.py:269-271` (agent-read)).
- The fleet holds static root S3 keys: `MEDALLION_S3_*`
  (`services/medallion/src/medallion/core/config.py:210-236`), viewer/maintenance/lineage
  similarly — credential-level tenant isolation exists only for EXTERNAL clients, on an opt-in
  path. Databricks ships vending as THE access mechanism and soft-delete ON (7 days); Gravitino's
  model has every consumer (incl. GVFS) on vended creds.

**This is a decision list, not a bug list.** Each default is individually reasoned (grace changes
drop semantics for every caller; mode_b is the RustFS-without-STS reality). But jointly, a default
deploy has Lakekeeper-grade machinery with OSS-Unity-grade enforcement. The ask: make the posture
an explicit, owner-ratified matrix (dev vs prod values files), specifically deciding: (1) prod
grace days > 0? (2) prod vending = web_identity (RustFS-native, chart already has the wiring,
`chart/templates/services.yaml:104-111`)? (3) `fga_lock_root_create=true` in prod? (4) a roadmap
item for fleet services consuming vended per-tier creds instead of root keys (they can call the
same `/credentials` door with their service identity — the service-door identity plumbing exists,
`security.py:78-160` (agent-read)).

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
map's comment.

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

**Evidence.** The Lance-only gate: `data.py:96-108` (agent-read) — right call for the DATA plane.
But the model registry authorizes ONNX artifact trees as `table:models$<model>`
(`endpoints/models.py:8-13,51-63` (agent-read)); `list_artifacts` is the one plain-path,
non-Lance surface (`services/models.py:155-175` (agent-read)); a bare artifact dir with no
per-model Lance registry dataset is listable-but-ungovernable (null versions, unpromotable); and
promotion correctness leans on "last row of the registry dataset" + "compaction preserves row
order" (`services/models.py:1-16` (agent-read)) — a convention, not a key.

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
   transport with no dead-letter (`api/dapr.py:73-80` (agent-read)); a dropped `warehouse_deleted`
   event leaves a replica routing a tenant at a deleted (possibly purged) bucket until restart.
   Fix: TTL floor (minutes) on the cache — bounds the damage without giving up the immutability
   premise.
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
7. Scaling maintenance replicas silently defeats the sweep's in-process `asyncio.Lock`
   single-flight (`routes.py:41-49` notes `compactionReplicas=1` (agent-read)); no chart guard
   exists, unlike the catalog's replicas>1-requires-controlEmit render failure. Fix: the same
   render-failure pattern for `maintenance.replicas > 1`.
8. ListUsers truncates silently at ~1000 subjects (`fga.py:520-559`, `access.py:20-22`
   (agent-read)) — an access-review surface that can under-report with only a log line. Fix:
   surface truncation in the response (`truncated: true`) so reviews cannot silently lie.
9. `revoke_object_tuples` reconstructs only the inverse `child` edge; any future model shape where
   a dropped object appears as USER on another object would survive drops
   (`fga.py:1267-1277` (agent-read)). Guard: a model-contract test asserting no such shapes exist,
   so adding one forces revisiting revoke.
10. `_collect_descendants` recurses unbounded (no depth cap) while the listing walk caps at
    `_MAX_NAMESPACE_DEPTH=8` (`tables.py:74` vs `namespaces.py` (agent-read)) — a pathological tree
    is bounded in one walker, a stack overflow in the other. Align the bound.

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
| 1 | Conditional PUT on id-minting registry writes + skill correction | F1 | S-M | boto3 `IfNoneMatch="*"`; 412 → code 14/AlreadyExists; new `cas`-marker e2e |
| 2 | `context` threaded through every FGA check | F2 | S | one helper in `fga_deps`; integration test with real window |
| 3 | Fix + run the isolation e2e; wire to CI (#84) | F5 | S | shape + key names; sabotage-run proof |
| 4 | ETag/version-conditioned read-modify-write on mutable records | F4 | M | status, protection, trash; bounded retry |
| 5 | Dual-write repair: create-side compensating cleanup + write-capable structural reconcile | F3 | M-L | Lakekeeper's guard + reconcile pattern; keep report AST gate on the report module |
| 6 | Trash-plane coherence: record-first ordering, declared-only unification, purge unbinds, sweep skips trashed | F6 | M | four small fixes, one theme |
| 7 | `Unsupported` → 406 + docs/skill/COVERAGE updates | F8a | S | update the pinned contract test deliberately |
| 8 | Describe-path vend audit row | F8c | S | |
| 9 | Posture matrix decision (grace, vending default, root-create lock, fleet-on-vending roadmap) | F7 | owner | decision doc, then values files |
| 10 | Generic `asset` rung design (models first consumer) | F9 | L | needs owner ratification; Lakekeeper/Gravitino precedent in §3 |
| 11 | Hygiene batch | F10.1-10 | S each | independent one-liners |

---

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
