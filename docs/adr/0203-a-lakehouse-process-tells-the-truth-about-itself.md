# 0203. A lakehouse process tells the truth about itself: supervised loops, readiness from memory, the caller judged before the body (2026-11-12)

**Status:** accepted. Source: the owner, 2026-11-12, through the question tool, on row #794 (XC-201, with CTL-006 #224
worked in the same change): "Gate on required", "Pass failed, go on" and "Answer, let it finish", each the recommended
option.

## Context

Kubernetes can act only on what a pod reports. Read from the code on main `a827797a3` and measured on 2026-11-12, unless
another date is given:

- **A loop could die in a Ready pod.** Maintenance's task handler and its loop supervisor caught `Exception`. pyo3
  raises a Rust panic as `pyo3_runtime.PanicException`, a `BaseException`, so a pylance panic in a pass ended the loop
  that takes tasks with no log line while `/readyz` stayed 200, and the task the bus delivered again ended the next
  replica's loop the same way. The catalog cancelled two background tasks at shutdown without awaiting them, and every
  lifespan acquired resources before its `try`.
- **Readiness waited for a request thread.** The catalog's `/readyz` resolved the namespace through `run_in_threadpool`,
  anyio's default limiter of 40 threads that every sync door shares, under the kubelet's default one-second probe
  timeout (the chart's readiness probe sets none). A burst of slow doors that held the 40 would fail every replica's
  probe together while nothing had failed. Lineage's feed read through the catalog took 1.03-1.25 s (measured live
  2026-10-19), longer than the probe allows.
- **Lineage's readiness read failed on a race, not an outage**: 8 `readyz_catalog_unavailable` lines in about 6.7 h
  (6 `RemoteDisconnected`, 2 `ConnectionResetError`). Its 5 s feed read reused a keep-alive connection the catalog closes
  after 5 s idle, and urllib3 does not retry a POST whose connection the server closed (`allowed_methods` leaves POST
  out), though the feed read changes nothing.
- **Bodies were read before the token.** FastAPI 0.140.13 reads and parses a route's body before it solves any of its
  dependencies (`routing.get_request_handler`), so lineage's intake parsed a JSON body of any size from a caller with no
  token, and an Arrow upload with no token sat in one of the catalog's two production write seats for as long as it
  trickled in. Lineage and maintenance installed no body cap at all (CTL-006).
- **FastAPI turns any Exception raised while it reads a body into its own 400** ("There was an error parsing the body"),
  so the body cap's streaming refusal, an `Exception`, answered 400 rather than 413.
- **FastAPI 0.140.13 keeps an included router as one opaque `_IncludedRouter` entry** in `app.router.routes`, and an
  included route builds its request handler afresh on each request (`APIRoute.handle` calls `get_route_handler()`), so it
  never runs `route.app`.
- **No request had a deadline.** The longest server spans in the estate's traces, 2026-10-29 to 2026-11-12: the
  catalog 50.9 s (a warehouse cascade delete) and 46 s (an insert waiting for its commit turn), lineage 58.1 s (the relay
  tick), maintenance 34.2 s; the catalog's p99.9 was 13 s.
- **The OIDC key set expired with the IdP down.** PyJWT 2.15.1's `PyJWKClient` caches a key set only on a successful
  fetch, so a cache whose lifespan ran out during an identity-provider outage left every token unverifiable.
- **A non-JSON grant body answered 500**: `_authorize_grant` and `_authorize_batch` read `request.json()` unguarded.
- **References**, as reported to the owner with the questions. Lakekeeper (main `08b67d7`, chart `040875a`): a 32 MB
  body limit on the whole router, a 30 s request timeout that cancels the handler and answers 408 (safe there because
  the handler's database transaction rolls back), a health check every 10 s with `/health` answering 503 while any
  provider is unhealthy, used for both liveness and readiness, the server ending on the first of its services to exit,
  and a task runner that logs a panicked worker and restarts it in-process. Polaris gates readiness on its database
  (`/q/health/ready`); Gravitino gates on nothing (`/`). The fastapi health-checks reference answers 503 when a required
  component is down.

## Decision

1. **Every background loop is supervised** (`lakehouse.supervision.Supervisor`). A loop that ends while its process
   serves, raised or returned, ends the process: one CRITICAL `background_work_ended` line naming the loop, the
   process's logs, metrics and traces given 3 s at most to leave (the Collector drops a lakehouse pod's stdout copy, so a
   record still in the exporter's batch is otherwise lost), then `os._exit(70)`. The flush runs on a thread of its own,
   waited for at most that long: the OpenTelemetry SDK's batch processors ignore the timeout a flush is handed, and one
   asked for 1 s against a Collector that took the connection and never answered took 16 s (measured 2026-11-12 on
   opentelemetry-sdk 1.45.2). A maintenance worker that ends itself while it holds a task flushes only within what its
   lease leaves, less 0.5 s, and not at all once its claim is gone, so its pass is gone before JetStream hands the task
   on or another pass takes the table. A one-shot task ends the process only when it fails; a cancelled piece and every
   end after the process began to stop are stops. Each loop waits out its own dependency's failures inside its pass, so
   only a fault no loop handles ends one, and a worker whose claims bucket the bus no longer holds ends its task loop on
   the first claim that finds it gone.
2. **A panic inside one maintenance pass is that task's failure** ("Pass failed, go on"): delivered again after its
   back-off, ended and counted (`maintenance.tasks.ended`) on its last delivery, logged with its traceback, while the
   worker serves on. Any other `BaseException` that is no request to stop propagates and ends the process. A panic
   inside a request is answered 500 (code 18) with the spec's problem body by `PanicCatcherMiddleware`, and the process
   serves the next request.
3. **Readiness answers from memory** (`lakehouse.health.HealthWatch`, "Gate on required"). Each dependency is read on a
   loop of its own every 5 s within 5 s, one cadence for every service (`lakehouse.health`), and `/readyz` answers the
   stored verdicts, each named in `components`. The
   catalog's object store (a listing of its control root under its own session) and OpenFGA, and lineage's feed read
   through the catalog, are REQUIRED: while one does not answer, `/readyz` answers 503 `unhealthy` and the replica
   leaves the Service. A reported dependency that does not answer keeps the replica in rotation, `/readyz` answering 200
   `degraded` with the dependency named, for a dashboard or an alert to act on. The bus, the secret store and, where
   lineage asks it directly, OpenFGA are reported and never gate: the outbox
   stands in for the bus, a service rides through the store on the keys it holds, and lineage's doors answer their own
   503 and its deliveries retry while OpenFGA is down (the option chosen gates lineage on the catalog alone, "as today").
   Maintenance gates on nothing it reads: its web and worker processes report the object store (its control root's
   listing), the bus and the secret store alike. Lineage reports the object store too where the outbox its relay drains
   is kept there. A
   verdict no longer renewed turns once its last answer is older than two rounds. A shared outage of the store or
   OpenFGA therefore takes every catalog replica out: the Gateway answers its own 503 and recovery lags the dependency
   by up to about 20 s.
4. **The app factory installs every request layer, once** (`build_lance_service_app`, CTL-006): `max_body_bytes` and
   `request_deadline_seconds` are required keywords, and the order, outermost first, is the request id, the panic
   catcher, the deadline, the service's outer layers (the catalog's write shed and path reading), the body cap, then the
   service's inner layers. The caps: the catalog 256 MiB (`catalog.maxBodyBytes`), lineage 8 MiB, maintenance 4 MiB, each
   a chart value. The body cap and the write shed answer a refusal inside the read that decides it and drop whatever the
   app sends after (`lakehouse.read_refusal`): FastAPI turns an Exception raised while it reads a declared body into its
   own 400, and a function middleware turns a raised BaseException into its own RuntimeError, so no exception type alone
   reaches the layer that refused. #224's gateway, producer and stage runner no longer exist (rask runs no edge
   process, ADR 0165, and the clients left with XC-192), so its 429 and access-line clauses are moot.
5. **The caller is judged from its headers before its body is read** (`lakehouse.admission.judge_before_the_body`). Every
   route that depends on the service's authentication gate, walked as it is served (`iter_route_contexts`, an included
   router's routes with the dependencies its inclusion adds), runs the service's judge first, at the route's own
   `handle`, which both dispatch paths call. The catalog and lineage build their judges from one
   (`lakehouse.admission.caller_judge`), which resolves the gate and its settings as FastAPI resolves them for the route,
   `dependency_overrides` included. A caller refused is answered before a byte of its body is read; the caller admitted
   is kept on the request for the gate (`admitted_caller`). A request of a method the route does not serve is still
   answered 405. The catalog's write seat is taken when a write first reads its body, so a request refused from its
   headers holds none.
6. **A request with no answer begun after its deadline is answered 503 (code 17), and its door runs on**
   (`RequestDeadlineMiddleware`, "Answer, let it finish"). 300 s per service, a chart value each, five times the longest
   door measured. The door is never cancelled: it undoes half-done work in its own `except` blocks (a location claim
   handed back, a parent edge moved back), and a cancellation, which is no `Exception`, would skip them. Its late answer
   is dropped and its end logged; what it holds stays held until it ends. A response already begun runs on unbounded.
7. **Every lifespan releases what it acquired through one `AsyncExitStack`**, in reverse order, the supervisor's loops
   stopped and awaited first. The maintenance worker stops them once it is asked to stop, before its drain, so the health
   watch never reads the queue the drain closes and no loop's end cuts a pass the drain lets finish.
8. **A read through the catalog whose kept-alive connection the catalog closed is sent again** (`catalog_read_client`):
   lineage's names and feed reads go through a pool that retries any method on a read error, since each read changes
   nothing.
9. **Every issuer's keys are refreshed in the background and served whatever their age**; a token naming a key the held
   set lacks fetches once, paced. An identity-provider outage serves the last keys read.
10. **A body a gate reads to decide and cannot parse is the caller's malformed request**: 400 (code 13).

## Consequences

- A dead loop restarts its container instead of leaving a Ready pod that does nothing; a native panic costs one task's
  delivery or one request, never the replica.
- A store or OpenFGA outage shows as NotReady catalog pods and the Gateway's own 503, not as problem bodies; the doors'
  own 503s remain for a per-replica fault the probe has not yet seen, and for lineage, which stays Ready through an
  OpenFGA outage.
- A door past its deadline holds its thread and memory until it ends, as it did without a deadline; its caller is told
  the outcome is unknown and reads before retrying.
- The admission wrap and the refusal answered in the read rest on FastAPI 0.140.13's dispatch and body read, both
  measured above. A FastAPI upgrade that moves either is caught by the tests that drive them over a socket and through
  the apps: `services/lineage/tests/test_a_post_to_the_intake_is_refused_before_its_body_is_read.py`,
  `tests/unit/test_one_lance_service_assembly.py`, `tests/unit/test_load_shed.py` and
  `packages/lakehouse/tests/test_shared_body_cap.py`.
- The logs operators read change: `readyz_catalog_unavailable` is now `dependency_unavailable` naming the dependency,
  and a loop's end is `background_work_ended` (exit 70); `docs/runbooks/RUNBOOK-oncall.md` says what each means.
