# Lakekeeper deep-read: RESILIENCE + PRODUCTION READINESS, translated onto rask's stack

Date: 2026-09-25. Reference tree: `/home/gabriel/Desktop/lakekeeper-ref` at `a58e401`; chart at
`.../reaudit/lakekeeper-charts/charts/lakekeeper`. Rask read at HEAD + the staged pylance-12 diff (not
relevant to this domain). All paths below are relative to those roots unless absolute. `LK:` = Lakekeeper,
`R:` = rask (`/home/gabriel/Desktop/rask`).

Method: read in full the files the domain names (health, cache_ttl, cache_metrics, idempotency, serve,
bin/*, router, maintenance, the integration-test crate's `src/` + its test_utils, three authz test files,
the Lance e2e) and the relevant spans of the rest; then read the rask counterpart and, for one claim that
could not be settled by reading, MEASURED it (below, finding 1). Scratch only under
`.../reaudit/lakekeeper/`. Nothing in the repo or the cluster was written.

---------------------------------------------------------------------------------------------------------

## 0. Headline (ranked by what it costs rask today)

1. **NEW, HIGH, MEASURED — rask's SIGTERM drain hook stops uvicorn from ever shutting down.**
   `arm_drain_on_sigterm` replaces uvicorn's own SIGTERM handler instead of chaining to it. A process that
   arms it never exits on SIGTERM and never runs its lifespan teardown. The maintenance worker's memory
   recycle (the LH-183 mitigation) is implemented as SIGTERM-to-self, so a "retired" worker becomes a
   permanently NotReady zombie that `/livez` keeps alive. Lakekeeper's shape (one cancellation token →
   graceful server stop → bounded join of every background service → abort) is the reference.
2. **Idempotency records never expire, are never reaped and are not atomic with the mutation** (already
   filed as new in `findings_lance_lakekeeper.md:77`). Lakekeeper adds three things the filing does not
   name: a record *lifetime* advertised to clients, a grace period, and a success-only record written
   inside the mutation's own transaction.
3. **LH-194 has a Lakekeeper answer, and it is option (a).** The service that created an object
   compensates *in the same request* with a creation guard, through the authorizer rather than the
   user-facing door (`create_table.rs:44-107,146-167`).
4. **Startup validation: Lakekeeper refuses to serve against stored state the binary cannot honour** (DB
   migrated by a newer binary, missing authz-model version, every IdP down). Rask has fail-closed
   *secrets* and *auth construction* but no stored-state-versus-config guard. That guard is exactly what
   LH-150 and XC-011 lack.
5. **Health: rask's probe split is better than Lakekeeper's. Lakekeeper's cached, jittered, per-component
   background health is better than rask's.** Take the second without the first. Lakekeeper's chart
   points LIVENESS at a dependency-aware `/health`, which restart-loops the whole fleet on a shared
   dependency outage.
6. **Caches: jitter, single-flight, a version gate and metrics.** Rask's binding and registry caches have
   none of the four. Rask's cross-replica broadcast eviction is ahead of Lakekeeper, whose invalidation is
   in-process only.
7. **Events: do NOT copy.** Lakekeeper's CloudEvents are best-effort: a 50 ms channel send, warn-and-drop,
   and core NATS with no persistence. Rask's outbox lanes are ahead. CP-037 is the remaining rask gap.
8. **Integration tests: Lakekeeper's Rust suite proves service-layer authz WIRING against a test double
   and never exercises vending.** Its only negative credential-scope proofs are a docker-compose Python
   e2e, plus — the part worth copying — a *runtime* self-check at warehouse create that refuses
   over-permissive downscoped credentials.

---------------------------------------------------------------------------------------------------------

## 1. Graceful shutdown (NEW finding; LH-183, LH-148)

### What Lakekeeper does
- One `CancellationToken` for the process. The signal task listens for Ctrl-C, SIGTERM and SIGINT and
  cancels the token (`LK: crates/lakekeeper/src/api/mod.rs:26-79`). It is spawned into the same named
  `JoinSet` as every background service (`crates/lakekeeper/src/serve.rs:207-222`).
- The HTTP server stops through `axum::serve(..).with_graceful_shutdown(token.cancelled())`
  (`api/router.rs:418-431`). Health loops, the task-queue runner and the event publisher all `select!`
  on the same token (`service/health.rs:19-46`; `service/tasks/mod.rs:934-979`;
  `service/tasks/task_queues_runner.rs:132-154`).
- Shutdown is **bounded and reported**. After `serve_inner` returns it cancels, sends `Shutdown` to the
  stats and event channels, and waits at most 20 s. It logs every 5 s which named services are still
  running, then `abort_all()` (`serve.rs:245-322`). If any service exits early, not by the signal, that
  is treated as an error and brings the process down (`serve.rs:583-601`).
- In-flight task leases are released by construction. A worker that dies mid-task stops heartbeating,
  and its task becomes pickable again once `now - last_heartbeat_at > max_time_since_last_heartbeat`.
  The timed-out attempt is logged as `'Attempt timed out.'`
  (`crates/lakekeeper-storage-postgres/src/tasks.rs:328-419`, esp. 337-345, 347-391, 398).
- Chart: `maxUnavailable: 0` rollout (`templates/catalog/catalog-deployment.yaml:14-19`). **Defect worth
  not copying:** `catalog.terminationPeriod: 60` is documented (`values.yaml:198-200`, README:172) but
  rendered by no template (grep over `templates/` returns nothing), so pods get the Kubernetes default
  30 s.

### What rask does today
- A much richer drain *contract*: SIGTERM flips `shutting_down` (`R: packages/service-kit/src/service_kit/draining.py:210-257`).
  HTTP run doors answer 503 + `Retry-After` and sidecar routes answer RETRY, never DROP (`draining.py:87-111,159-194`).
  A preStop sleep, a grace period and Dapr `block-shutdown-duration` are derived from one `lifecycle`
  block (`chart/values.yaml:690-696`; `chart/templates/_helpers.tpl:215-228,965-975`).
- **The defect.** `arm_drain_on_sigterm` installs its handler with `loop.add_signal_handler(SIGTERM, _flip)`
  (`draining.py:245`), and `_flip` only calls `mark_draining` (`draining.py:238-242`). uvicorn 0.51.0
  (the locked version, `uv.lock:4287-4288`) installs its own `handle_exit` with `signal.signal` *before*
  the lifespan runs (`.venv/.../uvicorn/server.py:320-344`). `add_signal_handler` replaces that Python
  handler, so `should_exit` is never set.
- **Measured** (scratch `.../reaudit/lakekeeper/sigterm/drive.py`, `drive2.py`, run on the rask venv's
  uvicorn 0.51.0; reading rule fixed before the run: "armed app alive ≥10 s after SIGTERM while the
  control exits <2 s ⇒ handler displaced"):

  | app | SIGTERM result | lifespan `finally` |
  |---|---|---|
  | control, no hook | exited in 0.20 s | ran |
  | rask hook (`arm_drain_on_sigterm`) | **still alive 12 s later**; `/flag` answered `{"shutting_down":true}` | **never ran** |
  | rask retirement shape (`os.kill(getpid(), SIGTERM)`) | **still alive 12 s later** | **never ran** |
  | candidate fix: `_flip` then call the captured previous handler | exited in 0.20 s, flag flipped first | ran |

- Seven production callers: `services/lineage/src/lineage/main.py:130`, `services/medallion/src/medallion/producer.py:149`,
  `services/medallion/src/medallion/stage_runner.py:127`, `services/maintenance/src/maintenance/service.py:111`
  (both the planner and the worker run `maintenance.service:app`, `chart/templates/maintenance.yaml:104`,
  `maintenance-worker.yaml:61-62`), `services/gateway/src/gateway/__init__.py:418`,
  `services/notifications/src/notifications/lifespan.py:155` and `packages/service-kit/src/service_kit/media/lifespan.py:121`.
  The catalog does not arm it.
- **Consequences, derived and not all observed:**
  - (a) Every rollout of those pods ends in SIGKILL at `terminationGracePeriodSeconds` (40 s; 120 s for
    maintenance) instead of an orderly exit. Their lifespan teardown (client closes, and any flush in
    teardown) never runs.
  - (b) **The maintenance worker's recycle does not recycle.** `retire_this_worker` sends SIGTERM to self
    and documents "the container then exits for Kubernetes to restart"
    (`services/maintenance/src/maintenance/services/rewrite_slot.py:147-170`). With the handler displaced
    the process stays up and drained. `/readyz` answers 503, but `/livez` is dependency-free and keeps
    answering 200 (`service_kit/probes.py:46-50`; `_helpers.tpl:1090-1095`), so the kubelet never
    restarts it. Its sidecar keeps delivering, the app answers RETRY, and units exhaust the 4×120 s
    policy and park on the DLQ (`draining.py:92-107`; `dapr-resiliency.yaml:49-51`). Two workers
    (`MAINTENANCE_RECYCLE_AFTER_PASSES=150`, `..._AT_MEMORY_FRACTION=0.7` on the live deployment) would
    halt the maintenance lane once both retire.
  - Not observed live: the current worker pods are 4 h old with 0 restarts and no `maintenance_worker_retiring`
    line in 5 h of logs (read-only `kubectl logs`). **Worth checking, not concluded:** whether the 7
    `dlq.maintenance.work` messages LH-148 counts were parked by a retired worker.
- **Why the tests miss it:** `packages/service-kit/tests/test_drain_arms_at_sigterm.py:35-47` asserts only
  that the flag flips, with no uvicorn in the loop. `services/maintenance/tests/test_a_worker_retires_before_it_is_killed.py:74-92`
  monkeypatches `os.kill`. Both are doubles that cannot see the displaced handler (MEMORY: "a double must
  carry the whole signature").
- uvicorn's own bound is also unset: `timeout_graceful_shutdown` defaults to `None`
  (`.venv/.../uvicorn/config.py:230`, `server.py:284-296`), and no chart arg passes
  `--timeout-graceful-shutdown` (`chart/templates/services.yaml:60-63,557-560`; `medallion.yaml:53-56`).
  After the fix, one slow in-flight request can still hold the process until SIGKILL.

### What rask should do (on its own stack)
1. RED first: a test that spawns the real `uvicorn` on a minimal app whose lifespan calls
   `arm_drain_on_sigterm`, sends SIGTERM, and asserts exit within N s **and** that the lifespan `finally`
   ran. It fails today (measured above). Add a second case for SIGTERM-to-self.
2. Fix at the seam: `_flip` must flip the flag and then **hand the signal on** to the handler captured in
   `previous` (uvicorn's `handle_exit`), exactly as the measured candidate does. This keeps the SIGTERM-time
   flag the owner ruled for (2026-08-25) and restores exit. `_restore` already re-installs `previous`
   (`draining.py:250-255`).
3. Bound uvicorn: `--timeout-graceful-shutdown = grace − preStop − teardown budget` (e.g. 40−5−10 = 25),
   rendered from the same `lifecycle` block so the numbers cannot drift. This is Lakekeeper's 20 s bound,
   in uvicorn's vocabulary. Log the in-flight count on expiry; uvicorn already does
   (`server.py:289-295`).
4. Fix the chart-level twin of Lakekeeper's dead value while there: add a render test that every
   `lifecycle.*` value reaches a pod spec (Lakekeeper's `terminationPeriod` shows how a documented value
   can reach nothing).

---------------------------------------------------------------------------------------------------------

## 2. Health and probes (XC-006, XC-039, CTL-023; new)

### What Lakekeeper does
- `HealthExt` providers are refreshed by a **background loop, not by the probe**. The refresh is bounded
  by `timeout(refresh_interval)`, followed by `refresh_interval + 0..500 ms` jitter, and cancels promptly.
  A hanging provider is covered by a test (`service/health.rs:10-48,203-256`). The default interval is
  10 s (`config.rs:471,1055`).
- `/health` serves the cached `Vec<Health{name,lastCheck,status}>` per provider (catalog pools, secrets,
  authz) plus `maintenance_mode` (`health.rs:159-201`). Any non-`Healthy` answers 503
  (`api/router.rs:225-232`). It is unauthenticated because it is mounted after the auth layer
  (`router.rs:165-173`).
- Initial state is `Unknown`, which answers 503, so a pod is not ready until one real check has run
  (`crates/lakekeeper-storage-postgres/src/lib.rs:165-176`).
- Checks are synthetic calls on the real path: Postgres `SELECT 1` on both pools (`lib.rs:178-195`); an
  OpenFGA `check()` against a random project on the server object, which exercises store id + model id +
  authn (`crates/authz-openfga/src/health.rs:10-36`); Vault `sys/health` (`crates/lakekeeper-secrets-kv2/src/lib.rs:225-251`).
- A CLI `healthcheck` subcommand for container HEALTHCHECK (`crates/lakekeeper-bin/src/healthcheck.rs:9-44,61-85`).
- **Two weaknesses not to copy.** (i) On a check *timeout* the loop only warns and the stored status stays
  at its last value (`health.rs:28-37`), so a black-holed dependency keeps reporting `ok` with an ageing
  `lastCheck`, and `collect_health` never looks at age (`health.rs:159-188`). (ii) The chart points
  **liveness and readiness at the same `/health`** (`templates/catalog/catalog-deployment.yaml:106-125`),
  so an OpenFGA outage fails liveness 5×5 s later and restart-loops every pod.

### What rask does today
- Correct split: `/livez` is dependency-free and async, `/readyz` is lifecycle-gated plus an optional
  `ready_check` (`service_kit/probes.py:42-69`; `service_kit/health.py:1-33`). The factory mounts them for
  every app and sets the lifecycle flags (`service_kit/app.py:206-231,258-272`). There is a startupProbe
  with a 300 s budget (`chart/templates/_helpers.tpl:1076-1096`). `findings_lance_lakekeeper.md:39`
  already records this as rask being ahead.
- The dependency view is thin. The catalog's `ready_check` reports only the namespace id and never
  OpenFGA, the object store or the sidecar (`services/catalog/src/catalog/main.py:294-303`). Lineage does
  a **live** AGE round-trip inside the probe, 2 s budget, per probe (`services/lineage/src/lineage/main.py:149-167`).
  `Readiness.components` exists but is populated ad hoc (`service_kit/schemas/health.py:27-32`).
- There is no per-component "last checked" and no sealed-OpenBao or sidecar-down signal on any probe
  (XC-006 names the sealed-store hang; CTL-023 names sidecar transport failures surfacing as 500s).

### What rask should do
- Add a `service_kit` health registry: per-component async checks run by a background task started in
  the factory lifespan and cancelled on drain. Refresh with a timeout, interval + jitter; on timeout set
  `unknown`. Treat a result older than 2× the interval as `unknown`, which fixes Lakekeeper's weakness
  (i). Serve the cache in `/readyz` `components` with `lastCheck`.
- Components that map onto rask's stack:
  - OpenFGA: a `check` on a sentinel object, not a TCP ping — Lakekeeper's pattern.
  - Object store: `HEAD` on the control root.
  - The Dapr sidecar: `GET /v1.0/healthz/outbound`.
  - The Dapr secret store: sealed-OpenBao shows up here — XC-006.
  - AGE: `SELECT 1` + `RETURN 1`, moved out of the probe path into the loop.
- **Keep readiness failing only on per-pod faults** (own pool, own sidecar, draining). A shared
  dependency (OpenFGA, the store) is reported `degraded` in the body and the doors answer 503 +
  `Retry-After`, which the spec defines (`lance_docs/ns_catalog/spec.yaml:6685-6688`). Pulling every
  replica for a shared outage only turns 503s into connection refusals; that is Lakekeeper's weakness
  (ii) in readiness form.
- Surface the catalog's read-only maintenance flag in the body (see §7).

---------------------------------------------------------------------------------------------------------

## 3. Startup and config validation (LH-150, XC-011, P1.2 in `findings_reconciliation.md:21-22`, XC-006)

### What Lakekeeper does
- **Config is validated once, loudly, at first access.** Invalid config panics: `.expect("Valid
  Configuration")` (`config.rs:68-70`). Explicit asserts cover:
  - the IdP id grammar and reserved names (`config.rs:151-179`);
  - trusted-engine identities (`config.rs:93-119`);
  - `base_uri` joinability (`config.rs:126-130`);
  - a **cross-setting invariant**, user-assignment TTL ≤ role TTL, stated where the two caches meet
    (`config.rs:132-146`; rationale `service/cache_ttl.rs:14-20`).
- **Serve refuses a world it cannot honour:**
  - authenticator IdP ids validated and propagated to the authorizer (`serve.rs:198-202,644-668`);
  - a required OIDC provider that fails discovery is fatal (`service/authn.rs:323-334`);
  - if every configured OIDC provider failed, it "Refus[es] to start with authentication disabled"
    (`authn.rs:236-246`);
  - Kubernetes authn construction is fatal (`authn.rs:161-178`);
  - ServerInfo is validated: terms accepted or open-for-bootstrap (`serve.rs:369-371,604-625`);
  - the OpenFGA authorizer resolves the model id **for the version compiled into the binary** and fails
    with `ActiveAuthModelNotFound` if the store lacks it (`crates/authz-openfga/src/client.rs:55-72,101-127`;
    `migration.rs:100-127`).
  - Minor internal inconsistency: `migration.rs:113` logs the configured version but `:116` fetches
    `ACTIVE_MODEL_VERSION`.
- **Stored-schema-versus-binary guard.** `wait-for-db -m` reads migration state from the PRIMARY. If the
  DB was migrated by a NEWER binary it fails fast ("retrying never resolves this"); "behind" retries
  (`crates/lakekeeper-bin/src/wait_for_db.rs:16-35,37-86`, esp. 40-46, 52-66). The chart runs it as an
  init container, `wait-for-db -dm -r 100 -b 2` (`templates/_pods.tpl:19-44`). Migrations are one Job,
  a helm post-install/upgrade hook at weight -100 (`templates/db-migration.yaml:1-25`), never a serving
  pod. `migrate_before_serve` is debug-only and "not recommend[ed] in production"
  (`crates/lakekeeper-bin/src/config.rs:9-17`).
- Operator-only recovery verbs, confirmation-gated and lock-guarded: `reopen-bootstrap --yes`
  (`lakekeeper-bin/src/main.rs:120-137,292-320`); `openfga reconcile --dry-run`, add-only by default,
  under a Postgres advisory lock (`main.rs:140-163,322-410`; `service/maintenance.rs:1-27`).
- Unsafe defaults are warned about: the default secret-encryption key (`lakekeeper-bin/src/serve.rs:132-139`).

### What rask does today
- Per-field pydantic validation plus a few `model_validator`s (e.g. `services/catalog/src/catalog/core/config.py:616-680`).
  Fail-closed secret fetch with jittered retry, then raise (`service_kit/governed/secrets.py:64-131`).
  A `fatal` auth-construction posture for catalog, lineage and medallion (`service_kit/governed/auth_lifespan.py:1-45`).
  The app-token assertion (`services/maintenance/src/maintenance/service.py:121-123`).
- **No stored-state guard.** The delimiter is "BOOTSTRAP-ONLY" in prose (`catalog/core/config.py:400-402`),
  yet nothing refuses a boot whose delimiter disagrees with stored FGA ids (LH-150 evidence). No
  bootstrap record exists (XC-011).
- FGA model: `provision` refuses narrowing (`service_kit/governed/fga.py:514-604`). `resolve` returns the
  store's **newest** model (`fga.py:607-647`). `audit_pinned_model` "REPORTS, NEVER REFUSES"
  (`fga.py:670-735`). So what a non-provisioning pod enforces depends on boot order unless the per-cluster
  ids are pinned. P1.2 already proposes resolving the model whose canonical body equals the image's
  `model.json`.

### What rask should do
- **One bootstrap record, validated at every boot.** `_control/bootstrap.json` (XC-011), written
  conditionally once by the bootstrap Job — the way Lakekeeper's ServerInfo is written by bootstrap —
  carries `estate_id`, `delimiter`, and the FGA model *content hash* the estate was bootstrapped with.
  Every lakehouse service reads it in its lifespan and **refuses to serve** on a delimiter mismatch.
  That is LH-150's "refuse" option with a stored record to compare against instead of sampling tuples;
  it stays a no-op on an empty estate.
- **Model versioning by content, resolved per image** (P1.2), fail-closed like
  `ActiveAuthModelNotFound`. Keep publication to ONE Job, as Lakekeeper keeps migrations to one Job,
  never to a serving pod. P1.2's body-hash choice is better than Lakekeeper's integer version tuples
  because it cannot be bumped wrongly.
- **Refuse "ahead" at boot for the AGE graph schema too**, if lineage versions its constraints. Not
  checked: whether it records a schema version at all.
- Cross-setting invariants belong in one `model_validator` beside the settings they relate. The
  lifecycle arithmetic (grace > preStop + sidecar + uvicorn bound) is the rask example, and today it
  lives only in values comments (`chart/values.yaml:690-696`). Pin it with a render test.

---------------------------------------------------------------------------------------------------------

## 4. Idempotency (new filing `findings_lance_lakekeeper.md:77`; LH-194)

### What Lakekeeper does
- The header is an optional UUID. A duplicate header → 400, empty → absent, non-UUID → 400
  (`service/idempotency.rs:5-63`). **Only committed successes are recorded, with no in-progress state
  and no stored bodies**: a replay *re-derives* the response (`idempotency.rs:66-90`). For create and
  register that is a fresh `load_table` with fresh authz and fresh credentials
  (`server/tables.rs:101-170,285-304`; `server/tables/create_table.rs:120-140`).
- **Atomicity.** The key is inserted with `INSERT … ON CONFLICT DO NOTHING` **inside the mutation's own
  transaction**, immediately before commit. A conflict rolls back and answers `request_in_progress`
  (`create_table.rs:368-391`; `server/tables.rs:1691-1703`; `crates/lakekeeper-storage-postgres/src/idempotency.rs:145-170`).
  Where a mutation cannot put the key in one transaction, the key is **refused rather than faked**:
  recursive namespace drop (`server/namespace.rs:486-494`).
- **Lifetime.** `lifetime` PT30M + `grace_period` PT5M. The lifetime is advertised to clients as
  `idempotency-key-lifetime` in `getConfig` (`config.rs:737-781,758-767`; `server/config.rs:129-132`).
- **Reaping.** 1% of checks spawn a cleanup that deletes ≤1000 rows older than lifetime+grace. A
  process-local claim with a take-over timeout stops overlap (`storage-postgres/src/idempotency.rs:12-45,73-118`),
  and a `created_at` index serves it (`migrations/20260318120000_idempotency_record.sql:35-36`).
- A `CHECK` constraint restricts records to mutation endpoints (`...idempotency_record.sql:17-32`).
- The check runs in parallel with authz for commit (`server/tables.rs:1358-1383`). There is a small
  inconsistency: the trait doc says "uses the write pool to avoid replica lag"
  (`service/catalog_store/idempotency.rs:15-18`) while the impl uses `read_pool` and argues that is safe
  (`storage-postgres/src/idempotency.rs:53-57`).
- Tests prove only the sequential replay (`crates/lakekeeper-integration-tests/tests/server_generic_tables.rs:402-445`).

### What rask does today
- Optional `Idempotency-Key` (the Lance Namespace spec defines none — `services/catalog/src/catalog/api/idempotency.py:1-20`).
  The record is claimed by a store-arbitrated conditional put, with an `in_flight` lease (300 s),
  takeover, and a stored `{status, body}` (`packages/service-kit/src/service_kit/lakehouse/idempotency.py:32-49,85-145`).
  The door records only after success (`services/catalog/src/catalog/api/v1/endpoints/data.py:143-167`).
  The version CAS door deliberately does without it (`endpoints/versions.py:401-417`). The seam's unit
  tests are *stronger* than Lakekeeper's: concurrency, lease, takeover and key reuse
  (`packages/service-kit/tests/test_a_replayed_write_converges_on_its_key.py:41-113`).
- **Gaps:**
  - A `done` record replays **forever**: `claim` returns `Replay` with no age check (`idempotency.py:120-121`).
  - Nothing deletes under `_idempotency/`; the docstring says "these expire" (`idempotency.py:32-35`), and
    grep finds no other reader of `PREFIX`.
  - The claim is not atomic with the Lance write. A crash between the write and `record_outcome` leaves
    `in_flight`, and after the lease a replay re-executes; for `mode=Create` that is the exact
    AlreadyExists ambiguity the seam exists to remove.
  - The stored body replays without re-checking authz. It carries no secret (`CreateTableResponse`
    carries location, version and properties only — `services/catalog/src/catalog/services/dataplane.py:402-405,494`),
    so the exposure is metadata to a subject whose grant was revoked inside the window.

### What rask should do
- Add a **lifetime**: `done` records older than `lifetime` are ignored and treated as a fresh key, with a
  grace window for clock skew. Advertise it: rask has no `getConfig`, so use a response header on every
  keyed write, e.g. `Idempotency-Key-Lifetime: PT30M`, and state it in the `rask-lance-catalog` skill.
- A **reaper** in `services/maintenance` over `_idempotency/`, deleting records older than lifetime+grace
  (report-only first, as the existing filing says). Use one registry of control prefixes so the purge
  list cannot miss it (same filing).
- **Atomicity, Lance-idiomatically.** Make the marker ride the same atomic write as the mutation.
  Lakekeeper puts it in the mutation's transaction; rask's atomic unit is the store's conditional put or
  the Lance commit. For create and register: stamp the key into the object the create itself writes, then
  on takeover-after-lease, before re-executing, read the existing table and converge if it carries this
  key. **Unverified which carrier is atomic on pylance 12.0.0** (Lance transaction properties vs the
  namespace registration record). Measure before choosing; do not guess.
- Where atomicity is impossible, **refuse the key**, as Lakekeeper does for recursive drop, rather than
  offer a convergence that can re-execute.
- On replay, re-authorize (cheap; the subject hash already scopes the key) or re-derive the body from a
  describe, the way Lakekeeper re-derives from `load_table`.

**LH-194 (failed seed leaves a registration only a human can remove).** Lakekeeper's `TableCreationGuard`
records what the request created (the authorizer tuple via `mark_authorizer_created`, the metadata file via
`mark_metadata_written`). On any error it deletes them *through the authorizer and IO directly*, not
through a permission-checked user door; `success()` disarms it (`create_table.rs:44-107,146-167,356-366`).
That is option (a) of LH-194. It works because creation and compensation are **one request**. Rask's
producer registers and seeds in two requests, so the translation is: move the seed into the catalog's
create-from-Arrow door, which already exists (`data.py:103-167`), and let the catalog's own guard unwind.
The catalog already does exactly this for its own seed failure — `_undo_register`, per LH-194's evidence
(`tables.py:802-806`). The alternative, a creation-scoped unwind capability returned by the register
call, is a new credential shape and weaker.

---------------------------------------------------------------------------------------------------------

## 5. Caches: TTL jitter, single-flight, version gate, metrics (new; P4.6 in `findings_reconciliation.md:88-89`)

### What Lakekeeper does
- **Downward-only TTL jitter**, 10% by default. Each entry lives `base·f` for `f ∈ (1−0.1, 1]`, so the
  configured TTL stays an upper bound. The purpose is to break the fleet-wide stampede that "per-replica
  single-flight cannot see". It re-jitters on update, and one cache is deliberately excluded to keep a
  cross-cache TTL invariant (`service/cache_ttl.rs:1-93`).
- **Single-flight read-through** (a per-key compute lock) with **no negative caching**, a **version-gated
  insert** so a stale load cannot overwrite a newer entry, and **removal through the same compute lock**
  so a delete racing an in-flight load cannot be re-put (`service/catalog_store/warehouse_cache.rs:109-158,160-248`).
- **Metrics:** `lakekeeper_cache_size`, `_hits_total` and `_misses_total` labelled by `cache_type`
  across 12 caches, plus a fan-out histogram for invalidations (`service/cache_metrics.rs:1-34`;
  `warehouse_cache.rs:250-259`).
- Invalidation listeners run on the **in-process** event dispatcher (`serve.rs:414-443`), so other
  replicas converge only by TTL.

### What rask does today
- The warehouse binding cache is positives only, with a TTL floor (300 s, `catalog/core/config.py:602`)
  plus **cross-replica broadcast eviction** over pub/sub (`services/catalog/src/catalog/api/dependencies.py:64-110,133-161`;
  `api/dapr.py:72`). The broadcast is ahead of Lakekeeper.
- The registry-root cache is a bounded TTL map, 5 s default, with no negative caching
  (`packages/service-kit/src/service_kit/lakehouse/warehouse_registry.py:62-127`).
- **Missing on both:**
  - no jitter;
  - no single-flight (a miss burst issues N S3 GETs from each replica);
  - no version gate (a resolve that started before an eviction and finishes after it re-inserts the
    pre-eviction binding for up to the TTL — `dependencies.py:75-90` writes after the `await`);
  - no hit/miss/size metric (grep finds none).

### What rask should do
- One small `service_kit` TTL-cache primitive used by both caches (and by P4.6's secret caches):
  downward-only jitter, per-key `asyncio` single-flight, and an insert guarded by a monotonic
  *eviction epoch* per key. Bindings carry no version, so an epoch bumped by the broadcast eviction is the
  on-stack stand-in for Lakekeeper's version gate. Add OTel counters `cache_hits`, `cache_misses` and a
  `cache_size` gauge labelled `cache_type`.
- Keep the broadcast eviction. It is the part rask does better.

---------------------------------------------------------------------------------------------------------

## 6. Retries, backoff and jitter (CTL-004, LH-195, CP-045/CP-038 for phase 2)

### What Lakekeeper does
- Commit conflicts: up to 2 retries with jittered exponential backoff (50 ms·2^n + rand(base/2),
  capped), with warn logs carrying the attempt count (`server/tables.rs:99,1248-1308`; the same for views,
  `server/views/commit.rs:136-173`).
- Object store: AWS SDK *adaptive* retry (`crates/io/src/s3.rs:65,95`), plus `tryhard` exponential backoff,
  3 retries, 100 ms → 10 s, on classified-retryable errors only (`crates/io/src/s3/s3_storage.rs:331-347`).
  The identity cache is disabled per client to avoid **unbounded partition growth** (smithy-rs#4340;
  `s3.rs:75-91`).
- Task queues: poll with `poll_interval + 0..500 ms` jitter; a 5 s back-off after a pick error; recording
  failure or success retries 5× at 1 s (`service/tasks/mod.rs:934-979,1055-1135`).
- Crash-safety through **heartbeat leases**: `FOR UPDATE SKIP LOCKED`, a stale heartbeat makes the task
  re-pickable, `attempt+1`, and `max_retries` moves it to the log as failed
  (`storage-postgres/src/tasks.rs:328-419,568-640`). A supervisor restarts any exited or panicked worker
  unless cancelled (`service/tasks/task_queues_runner.rs:36-156`).

### What rask does today
- Pub/sub delivery retry is `constant` 120 s × 4 **with no jitter, by design**: the Resiliency CRD cannot
  express `randomizationFactor`, and the window is load-bearing against ackWait
  (`chart/templates/dapr-resiliency.yaml:8-29,49-51`). Invocation retries are constant 2 s × 3, with a
  narrower matcher for write doors (`dapr-resiliency.yaml:166-212`) — CTL-004's policy.
- Client retries in service-kit already use tenacity `wait_exponential_jitter`
  (`service_kit/governed/fga.py:279`; `governed/secrets.py:80-99`).
- Rask replaces Lakekeeper's in-process task loops with Dapr cron bindings + JetStream consumers, which
  already give redelivery, ackWait and a DLQ — a stack translation, not a gap.

### What rask should do
- Leave the sidecar policy constant; it is measured and pinned. **Add jitter where the app owns the
  timing:**
  - the §5 caches;
  - the cron-driven ticks: the maintenance sweep's 120 s tick (LH-195), the outbox and control relays,
    and the lineage reconcile. Each handler can sleep `rand(0, k·interval)` before heavy work, or
    randomise its first dataset offset, so N replicas firing on one cron second do not hit the catalog
    and OpenFGA in lockstep.
- For phase 2 (compute), Lakekeeper's heartbeat lease is the pattern CP-045 and CP-038 lack. A running
  unit carries `last_heartbeat_at` in its durable record, a watcher that stops heartbeating makes the
  unit re-claimable, and a bounded attempt count parks it. On rask's stack that means a Dapr Workflow
  timer or JetStream `ackWait` plus `InProgress` acks rather than a Postgres row.

---------------------------------------------------------------------------------------------------------

## 7. Read-only maintenance mode (new; XC-031)

### What Lakekeeper does
- `MAINTENANCE_MODE=read-only` is read at startup. Mutating methods on `/catalog` and `/management` get
  503 + `Retry-After: 60` + a typed error `MaintenanceModeError`, while `/health` stays unaffected
  (`api/maintenance.rs:1-80`; `api/router.rs:146-153`). The mode is used for zero-downtime upgrades: an
  operator rolls pods into read-only, migrates, then rolls them out of it (`config.rs:547-583`).
- **Built-in task workers are not started** in read-only mode (`serve.rs:451-471`). Side-effecting reads
  are suppressed (`server/config.rs:41`).
- The mode is **echoed in `/health`** so an operator can confirm every pod picked it up
  (`service/health.rs:186,195-200`).

### What rask does today
- `LANCE_MAINTENANCE_READ_ONLY` on the catalog is more Lance-idiomatic than Lakekeeper's version. It
  classifies by the spec's action suffix, fail-closed by allowlist, because Lance reads arrive as POST
  (`services/catalog/src/catalog/api/maintenance_mode.py:28-68,91-100`; `core/config.py:230`).
- **But:**
  - it is not rendered by the chart (grep over `chart/` finds nothing), so it is unreachable in a
    deployment;
  - it is not reported on `/readyz`;
  - it gates only the catalog, while the maintenance worker, the stage runners and client-direct writers
    holding vended credentials keep committing to storage.
- A partial read-only window that the writers ignore is not a maintenance window.

### What rask should do
- Decide what the window is *for*. Rask has no relational migrations; candidates are an FGA model change,
  a bucket move, or an OpenBao unseal. If it is kept:
  - render it (`catalog.maintenanceReadOnly`);
  - echo it in `/readyz` `components`;
  - have the same flag stop the write-side consumers — maintenance units and stage triggers answer RETRY
    using the existing drain gate, `draining.py:186-194`;
  - have the catalog refuse to **vend write-tier credentials** during the window. That last point is the
    Lance-specific half Lakekeeper does not need, because Lance commits are CAS in the object store and
    not in the catalog.
- If there is no use, delete it (no-shims rule).

---------------------------------------------------------------------------------------------------------

## 8. Events under failure (CP-037, LH-144, LH-148) — do NOT copy

Lakekeeper publishes CloudEvents with `send_timeout(50 ms)` onto a bounded mpsc of 1000; a full channel is a
warn and a drop (`service/events/publisher.rs:544-584`; channel `serve.rs:228-229`). The background task
warns per failed sink and continues (`publisher.rs:616-697`). The NATS backend is core `client.publish`
with no JetStream and no ack (`crates/lakekeeper-events-nats/src/lib.rs:78-85`). That fits Lakekeeper,
whose events are notifications. For rask the lineage write event is the **cascade head**, so this model is
wrong for it. Rask's staged outbox plus relay is ahead (CLAUDE.md; CP-037 row's evidence
`service_kit/lakehouse/outbox.py:333-380`). The remaining rask gap is the compute plane's fire-and-forget
emit (CP-037), not anything Lakekeeper solves.

---------------------------------------------------------------------------------------------------------

## 9. Memory growth (LH-183)

- Lakekeeper runs **jemalloc as the global allocator** "to avoid glibc malloc fragmentation which causes
  monotonic growth of container_memory_working_set_bytes" (`crates/lakekeeper-bin/src/main.rs:10-14`).
  It is the same symptom class as LH-183. Rask already bounds glibc arenas and routes Arrow through
  system malloc (`chart/templates/_helpers.tpl:1570-1590`; `chart/values.yaml:3250-3264`), and the row
  shows the growth continuing.
- **Hypothesis, unmeasured, for the D12 profiling session** (`findings_reconciliation.md:171`). Lakekeeper
  disables the AWS SDK identity cache because it grows one partition per distinct credential set
  (`crates/io/src/s3.rs:75-91`). LH-183 measures ~1.06 KiB per *dataset-operation*, and the sweep vends
  per dataset (`vend_denied` is a refusal reason, LH-195). **If** pylance's object-store layer caches a
  client per distinct `storage_options` (a new session token per vend), that would be a per-operation
  native ratchet the working set does not bound. I did not verify it: `lance.Session` on 12.0.0 exposes
  only `size_bytes`, `index_cache_size_bytes` and `is_same_as`, and nothing in `lance_docs/` names an
  object-store registry.
- A test design: open the same S3 dataset 10k times through the shared session, with constant vs
  per-open-distinct credentials, and compare RSS slopes. Also try `LD_PRELOAD` jemalloc with
  `MALLOC_CONF=background_thread:true` as an A/B on one worker. Note that §1 matters here too: the
  recycle mechanism LH-183 relies on does not currently exit the process.

---------------------------------------------------------------------------------------------------------

## 10. What the Lakekeeper integration tests actually prove (XC-033, LH-177, CP-016; new)

**The Rust crate** `crates/lakekeeper-integration-tests`: 35 test files, about 20.9k lines, 265 test
functions.
- Every test calls `CatalogServer::*` or `ApiServer::*` **in-process** (`src/internal_helper.rs:54-147`;
  `src/lib.rs:85-150`). **0 files build a router or send HTTP**, so the authn middleware, the
  maintenance, body-limit and timeout layers, and request-metadata extraction are never exercised.
- Callers are `RequestMetadata::new_unauthenticated()` or a `test_user`
  (`crates/lakekeeper-storage-postgres/src/test_utils.rs:173-177,291-294`).
- **AuthZ** is `AllowAllAuthorizer` in most files and the `HidingAuthorizer` **test double** in 9
  (`crates/lakekeeper/src/service/authz/mod.rs:2502-2616`); real OpenFGA appears in exactly one file
  (`tests/openfga_role_membership.rs`). What this proves is *wiring*, not the model:
  - which action is checked for which object;
  - hidden objects are denied and blocked actions refused;
  - batch results keep order, and generic tables are not auto-allowed in the batch path
    (`tests/service_authz_table.rs:143,255,409,575,636,758`);
  - the check endpoint's semantics, including `TooManyChecks` (`tests/management_check.rs:297-356,870-908`).
- The real-OpenFGA file proves role-membership reads, including 403 for an unprivileged caller
  (`openfga_role_membership.rs:323-356`). **It is excluded by the default nextest filter**
  (`openfga_role_membership.rs:15-22`; `.config/nextest.toml:9-24`) and runs only under `--profile ci`
  with a live OpenFGA service (`.config/nextest.toml:26-30`; `.github/workflows/unittests.yml:298-307,340-362`).
- The model itself is proven by `fga model test` in `authz.yml:28-46`.
- **Vending is never exercised in this crate.** Every warehouse uses the in-memory profile, and
  `validate_access` skips vended validation for `Memory` (`service/storage/mod.rs:542-543`).
  `s3_compatible_profile` is defined and re-exported (`test_utils.rs:296-329`; `lib.rs:186-190`) but
  called by no test in the crate.
- Idempotency: the sequential replay only (`tests/server_generic_tables.rs:402-445`).
- **Flake masking in CI:** the test step runs under a retry action with `max_attempts: 2`
  (`unittests.yml:340-345`). Do not copy.

**Where the credential-scope proofs actually live:**
- (a) **At runtime, not in a test.** Warehouse create, storage update and credential update run
  `validate_access` (`api/management/v1/warehouse/mod.rs:463-473,1191-1197,1317-1322`). It checks
  direct read/write/delete; then it vends downscoped credentials and verifies **read/write in a
  sub-location AND no write to the parent**. A successful parent write fails creation with "Downscoped
  credentials allow write access to parent location" (`service/storage/mod.rs:501-596,598-706,759-813`).
  The check runs in parallel with a storage-overlap check (`warehouse/mod.rs:463-473`).
- (b) Env-gated MinIO and AWS inline tests (`service/storage/s3.rs:1673-1837`).
- (c) The docker-compose Python e2e — the only negative proofs:
  - a sibling write is denied (`tests/python/tests/test_special_char_locations.py:310-316`);
  - a cross-table read through byte-aliased paths is denied, with a positive own-path control
    (`test_special_char_locations.py:423-527`).
- (d) The **Lance** e2e (`tests/integration-tests/lance/test_lance.py`) runs with `AUTHZ_BACKEND: allowall`
  (`tests/integration-tests/lance/docker-compose.yaml:63`). It checks that `s3.session-token` is
  *present* and that a positive read works (`test_lance.py:173-211,266-299`). It **overwrites the vended
  endpoint client-side** because the warehouse endpoint is the in-cluster `http://minio:9000`
  (`test_lance.py:21,75,201,291`). That is LH-177's exact problem, papered over in Lakekeeper's own test.

**What rask has:**
- `test_credential_isolation_e2e.py:197-317` is *stronger* than all of Lakekeeper's: cross-tenant,
  cross-table, a read tier that cannot write, a sibling-plant check, and an own-table positive control
  (`findings_lance_lakekeeper.md:37` agrees).
- `fga model test` in CI (`.github/workflows/ci.yml:326-349`).
- **But** the e2e runs only by hand (XC-033), and there is **no runtime self-check** of vended scope at
  warehouse bind or catalog boot. Grep over `services/catalog/src/catalog/services/warehouses.py` finds
  provisioning and claims, but no probe.

**What rask should do:**
- **Port (a), not the tests.** When a warehouse is created or re-credentialed, the catalog vends a
  write-tier credential for a scratch table prefix. It asserts PUT, GET and DELETE inside the prefix,
  and that a PUT one prefix up is **denied**, then refuses the warehouse on either failure. This is
  zero-trust verification at the moment config changes, and it catches a MinIO policy regression that no
  test run would see until someone runs e2e.
- **XC-033:** follow Lakekeeper's CI shape and run live-dependency suites against **ephemeral services in
  CI** (its option (c) — `e2e-stack`/Dagger already approximates it), instead of needing a runner with a
  path to the deployed k3s.
- **LH-177:** the Lakekeeper test is evidence that "vending in-cluster only" is the de-facto reference
  behaviour. The honest door answers with a configurable external endpoint or says it cannot serve this
  caller; a test should not have to override the endpoint.

---------------------------------------------------------------------------------------------------------

## 11. Request limits (CTL-006; minor)

- Lakekeeper sets a 2 MB body limit, a 30 s `TimeoutLayer` → 408, a `CatchPanicLayer`, and a UUIDv7
  `x-request-id` minted and propagated (`api/router.rs:153,187-216`; `config.rs:537-545,1072-1073`).
- Rask has a body cap and write-concurrency shedding (`packages/service-kit/src/service_kit/middleware.py:105-130`;
  `services/catalog/src/catalog/main.py:336-342`) and no server-side time budget.
- **Do not add a blanket timeout to write doors.** Threadpool Lance work cannot be cancelled, so a 408
  would return while the commit may still land. That is worse than slow, and it defeats idempotency. A
  read-door budget, e.g. `/query`, is reasonable.

---------------------------------------------------------------------------------------------------------

## 12. Row mapping

| Topic | Rows | Verdict |
|---|---|---|
| SIGTERM hook displaces uvicorn handler; recycle never exits; no uvicorn shutdown bound | **NEW** (HIGH); LH-183 (recycle), LH-148 (`dlq.maintenance.work` source — to check) | Fix at `service_kit.draining` + `--timeout-graceful-shutdown`; RED test with real uvicorn |
| Cached, jittered, per-component health; readiness vs shared deps | NEW; XC-006, CTL-023, XC-039 (tangential) | Adopt the loop; keep rask's probe split |
| Stored-state guards at boot | LH-150, XC-011, P1.2, XC-006 | Bootstrap record + refuse on mismatch; model by content hash |
| Idempotency lifetime, reaper, atomicity | NEW (`findings_lance_lakekeeper.md:77`) | Lifetime + reaper + atomic marker, or refuse the key |
| Failed-seed registration unwind | LH-194 | Option (a): in-request creation guard (Lakekeeper `TableCreationGuard`) |
| Cache jitter, single-flight, gate, metrics | NEW; P4.6 | One `service_kit` primitive |
| Retry and backoff jitter where the app owns timing | LH-195, CTL-004; phase 2: CP-045, CP-038 | Jitter the cron ticks; heartbeat lease for compute |
| Read-only maintenance window | NEW; XC-031 | Wire it end-to-end or delete it |
| Events best-effort | CP-037, LH-144, LH-148 | Rask ahead; do not copy |
| Allocator and per-credential cache growth | LH-183 | Profile; test the hypothesis |
| Vending proofs: runtime self-check; CI ephemeral stack; endpoint | NEW; XC-033, LH-177, CP-016 | Port `validate_access`; run e2e in ephemeral CI |
| Per-service images make per-service readiness and drain meaningful | LH-197 | Context only |

---------------------------------------------------------------------------------------------------------

## 13. Not covered / not verified

- **Finding 1 was measured locally only** on uvicorn 0.51.0 (the locked version), not in a pod. The
  cluster is read-only for this audit, so I did not delete a pod to time a rollout, trigger a worker
  retirement, or read the DLQ messages (that needs NATS access via exec, which is outside "get/logs").
  `opentelemetry-instrument` wrapping uvicorn was not reproduced; it does not change `Server.serve`'s
  `capture_signals`, but I did not run it.
- Not read in full:
  - `lakekeeper/src/server/tables.rs` (spans 95-175, 280-310, 1225-1420, 1680-1720 only);
  - `service/tasks/mod.rs` (structure plus 185-270, 645-690, 876-1135);
  - `lakekeeper-storage-postgres/src/tasks.rs` (300-420, 565-640);
  - `service/storage/mod.rs` (500-830);
  - `warehouse_cache.rs` (105-260);
  - `authn.rs` (159-360);
  - the integration test files other than `lib.rs`, `internal_helper.rs`, `service_authz_table.rs`
    (first 400 lines plus the assert lines), `management_check.rs` and `openfga_role_membership.rs`
    (harness plus asserts), `server_load_table.rs` and `server_generic_tables.rs` (spans) — the rest were
    surveyed by grep for authorizer, storage profile and transport;
  - `config.rs` lines 1900-2532 (tests) were assert-skimmed only;
  - the Python e2e suite beyond `test_special_char_locations.py:300-527` and `test_lance.py`.
- Not checked on the rask side:
  - whether lineage versions its AGE constraints (§3);
  - which carrier makes an idempotency marker atomic with a Lance create on pylance 12 (§4);
  - whether the notifications and media lifespans rely on teardown flushes that the §1 defect drops;
  - `service_kit/lakehouse/outbox.py` itself (cited via CP-037's evidence only).
- Endpoint statistics, contract verification, the OPA bridge, generic-table specifics and Iceberg-only
  concerns (snapshot filters, ETags) are out of domain and were not assessed.
