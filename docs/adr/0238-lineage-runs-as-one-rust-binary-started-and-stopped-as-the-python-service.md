# 0238. Lineage runs as one Rust binary, started, judged ready and stopped as the Python service is (2026-11-17)

**Status:** accepted; a feature of the port (ADR 0217), and the pilot's assembly: every piece of lineage in Rust existed
(ADRs 0219 to 0237) and nothing started them together. `lineage serve` now starts them from the environment the chart
gives the lineage Deployment, serves the ten operations, runs the ingest lane and the relay under one supervisor, and
drains on SIGTERM. No image is built and nothing on the estate runs the binary. Source: the owner, 2026-11-17 ("try to
port the code to rust following these principles, keep ADR of features").

## Context

The Python service is `lineage.main`: `build_lance_service_app` with a lifespan that builds, in order, the secret-store
session, the object-store session, the provenance tables, the names, the authenticator and the decider
(`attach_auth`), the bus, the published keys, the ingest lane, the forwarder's client, the health watch and the relay,
every background loop kept on a `lakehouse.supervision.Supervisor`; `/readyz` answers from `lakehouse.lifecycle` and
`lakehouse.health`, and `lakehouse.draining` flips it at SIGTERM. uvicorn serves it with the chart's flags
(`--host`, `--port`, `--timeout-graceful-shutdown`, the proxy headers) under `opentelemetry-instrument`. The Rust pieces
were each proven alone; three seams between them were left by slices built in parallel: two adapters of the store to
the gates' `Feed`, two paths deciding who may write an event (the doors' and the lanes' `Gate`), and two futures that
were not `Send` (the derived walk's and the relay's), worked around with `spawn_blocking` + `block_on` and a boxing
helper, or never spawned at all.

## Decision

1. **One binary, from the chart's environment and flags.** `lineage serve [--host=] [--port=]
   [--timeout-graceful-shutdown=]` (`lineage::service`); `lineage operations` still prints the ledger's operations. It
   reads exactly the variables `chart/templates/services.yaml` renders for the lineage container (`lance.otelEnv`,
   `lance.busEnv`, `LINEAGE_*`, `lance.secretStoreEnv`, `lance.objectStoreSessionEnv`, `lance.governedOidcEnv`,
   `lance.governedFgaEnv` with the catalog's decision door, `RASK_SA_*`, `LANCE_AUDIT_ENABLED`, `LINEAGE_SIGNERS` and
   `LINEAGE_DELEGATORS`) and **no new variable**: the port, the address and the grace come as the flags the chart
   already passes uvicorn, and `--proxy-headers` and `--forwarded-allow-ips` are taken and change nothing (no door
   reads the peer). A setting that cannot be taken ends the start with exit 3, uvicorn's code for a start that failed,
   and a line naming the variable.

2. **What starts, in what order** (`service::serve`, `service::start`), each step the Python's:

   | Step | Rust | Python it answers for | Fails the start when |
   | --- | --- | --- | --- |
   | 1 | `Telemetry::from_env`, `install` (the log subscriber and the process's tracer) | `opentelemetry-instrument`, `configure_app_logging` | an exporter, protocol or token file the ADR 0237 rules refuse |
   | 2 | `Settings`, `AuthSettings` (with the waiver), the decider's settings, `SecretStoreSettings`, `BusSettings`; the audit trail installed | `get_settings`, `assert_authentication_configured`, `configure_audit` | any value cannot be taken |
   | 3 | bind the port; `/livez` 200 and `/readyz` 503 `starting` from here | uvicorn binds after the lifespan | the port is taken |
   | 4 | `Session::from_settings` (secret store), kept logged in | `secret_store_session` | the store never admits the login |
   | 5 | `StoreSession::exchange_at_boot`, kept renewed (`store-session`) | `start_store_session` | no session is issued |
   | 6 | `Store::new` and `ensure`, asked again every 2 s for 120 s | `ProvenanceStore`, `_ensure_tables` | the catalog does not create the tables in time |
   | 7 | `CatalogNames` | `CatalogNames.from_settings` | |
   | 8 | `issuer::authenticator`, the keys fetched and kept fresh (`oidc-keys`) | `attach_auth` | the providers' settings are refused |
   | 9 | the decider: the catalog's decision door (`RASK_FGA_DECIDER_URL`), else OpenFGA at the pinned store and model | `attach_auth(fatal=True)`, `CatalogDecider` | the decider's token or pins are missing |
   | 10 | `Bus::start(..).instrumented(BusMetrics)`, connecting behind the start | `bus_session` | |
   | 11 | `PublishedKeys` over the store's session, and the one `Gate` | `store_published_keys` | |
   | 12 | the ingest lane (`lineage-ingest`), where a bus is named | `_take_deliveries` | |
   | 13 | `HealthWatch` over the dependencies, a loop each (`health`) | `HealthWatch`, `_dependencies` | |
   | 14 | the relay's timer (`timer-lineage-relay`), where git gives it a period | `start_relay` | |
   | 15 | the doors behind `Shell::instrumented_router`; `/readyz` answers from the health watch | `startup_complete = True` | |

3. **Readiness.** `/readyz` is 503 `starting` until step 15, then the health watch's verdict: 503 `unhealthy` while the
   catalog's feed does not answer (the one required dependency), 200 `degraded` while only OpenFGA (asked directly),
   the bus, the object store holding the outbox or the secret store does not, else 200 `ready`, each named in
   `components` as the Python names them; 503 `shutting_down` from SIGTERM on.

4. **Drain, in the Python's order** (`Shell::serve`, then `service::serve`). SIGTERM turns `/readyz` to `shutting_down`
   at once; the listener takes connections for 100 ms more (what arrives as the signal lands is answered, as uvicorn
   answers what arrives before its loop next looks at its exit flag), then stops; the requests open then have the
   grace (`--timeout-graceful-shutdown`, else the request deadline) to be answered. Then every background piece is
   told: the ingest lane begins no pull and finishes and settles each delivery it holds (`keep_until_stopped`); the
   relay, the health loops and the sessions' loops are cancelled, which their idempotent steps allow; the bus is
   flushed and closed; the telemetry is shut down, which sends what is batched. The process exits 0.

5. **Supervision** (`lakehouse_core::service::supervision`, the port of `lakehouse.supervision`). Every background
   piece is kept on a `Supervisor`, by name. One that ends while the process serves, by returning or by a panic, ends
   the process: `background_work_ended` with its name, the telemetry given 3 s at most, then exit 70 (`EX_SOFTWARE`), for
   Kubernetes to start it again; a pod that is Ready and does nothing has no other repair.

6. **One gate.** `gate::Gate` is the one path that decides who may write an event: `authorize` (the bus and the outbox:
   the signature over the bytes that arrived, then what the stamped author may record), `ingest` (the HTTP door: the
   caller as itself), and `replay` (an operator's replay of a staged event: both). The lane, the relay and the four
   doors that record hold the one `Arc<Gate>`, so they cannot disagree: the model's Judge is one step. The store is the
   gates' `Feed` itself (`impl Feed for Store`), which removes `state::StoreFeed` and `gate::Recorded`; the doors'
   `State` holds the gate in place of its own copies of the signing, keys, decider, catalog, root and store, and the
   replay doors' `admission` went with them. `intake::Counted` is `lakehouse_core`'s `LineageOutcome`, and
   `intake::refusal_outcome` the one map from a refusal to its outcome, which the HTTP door, the lane and the relay share.

7. **Every future is `Send`, at its cause.** The derived walk is `store::walk::Walk`, a walk the store drives (it names
   the keys it reads next and takes their edges), so `Store::derived` holds no async closure; the doors' `spawn_blocking`
   + `block_on` and `doors::sendable` are deleted. The relay's future held async closures too (`Timer::run`,
   `Timer::turn` and `forward::forward` took them): they take a future, or a closure that makes one, so the relay runs
   on a task of its own.

8. **Count and tell** (`signals::Signals`, ADR 0237's instruments). The lanes' `Tally` is replaced by the metrics the
   Python records, at the moments it records them: `lineage.events.processed` at the subscriber (each delivery's
   outcome), the HTTP door (a refusal by `refusal_outcome`, a foreign run, an ingest), the dead letter (each park, and
   on a parked replay each delivery's outcome and each recovered one) and the outbox (each refusal the drain kept);
   `lineage.ingest.duration` around each record that landed; `lineage.signature.verified` and `.refused` in the gate;
   `lineage.forward.*` each forward; the outbox's backlog every tick, its poison drops, and its drained, stranded and
   refused counts; the bus's publish and process durations, deliveries by outcome, pulls failed and oversize refusals
   (`Bus::instrumented`); and the timer's `timer.turns`, `timer.last_claimed` and `timer.period`
   (`Timer::instrumented`), which the alert on a stopped timer reads and which the lanes slice had not ported. The
   governance trail is `telemetry::audit::Audit`, installed as the process's (`Audit::install`, `audit::emit`): `authn`
   at every door (`Authenticator::caller_of`), `authz` `deny` per object a gate refuses, with its relation, and
   `dlq_replay` and `dlq_parked_replay` with their outcomes. The spans are `publish <topic>` (PRODUCER, in the trace the
   envelope was staged under, and the envelope goes out naming it), `process <subject>` (CONSUMER, in the trace the
   envelope names, its status an error where the delivery was parked) and `lineage.record` (each group commit, with
   `lance.lineage.events`), on the tracer `Telemetry::install` makes the process's; the shell's deadline runs a door in
   its request's span, so a span a door starts is that span's child.

## How the binary was proven

`scripts/port_lineage_check.sh` builds the binary and starts real collaborators, never a container: the real Python
catalog on real pylance, the pinned OpenFGA with the estate's model and a tuple set on the catalog's own table uuids,
an identity provider the tokens are signed by, the pinned RustFS with its STS, the stand-in ServiceAccount issuer and
the chart's lineage policy, and the pinned nats-server with JetStream and a token.
`scripts/port_lineage_fixture.py` adds a proxy in front of the catalog (to fail or slow the feed's writes, or take the
catalog away), a stand-in secret store speaking the store's API and holding the signers' published keys, an OTLP/HTTP
sink that decodes every export, and a graph backend; it starts the binary and the Python service (`opentelemetry-instrument
uvicorn lineage.main:app`) from one environment shaped like the chart's. Two departures from the chart's shape, each
because the fixture is not the estate: authorization is OpenFGA's own API at the pinned store and model, since the
catalog fixture runs without authorization and so serves no decision door (the doors' corpus is put to both with the
catalog deciding by `DECIDER=catalog scripts/port_doors_check.sh`, ADR 0236); and no ServiceAccount verifier is
configured. Every check script takes free ports of its own unless one is named (`scripts/free_port.sh`), so two runs
on one machine do not share a store. Result on 2026-11-17, every check against the binary:

| Check | Count | Result |
| --- | --- | --- |
| (a) the doors' corpus (ADR 0236) and the signed cases, Python and binary side by side, signing enforced, the bus present | 406 requests, 203 to each; 4 signed staged events replayed on each side (listed signer, a signer nobody lists, a tampered signature, none) and a signed ingest | 0 differences; 6 kept on purpose: ADR 0236's five and the unlisted signer's refusal message (ADR 0223) |
| (a) a signed replay republished on the bus by each | 2 replays | each republished |
| (a) both stop on SIGTERM | 2 | the binary exits 0; the Python ends by the signal (below) |
| (b) the lanes' nine cases, the model's five ways among them, OpenFGA deciding, the relay on a 2 s timer and the forwarder on | 9, with the start | each passed: one feed row, whole (its run's binding and its edge), nothing left staged, a refusal kept; two replicas ran 4 ticks in 8 s at a 2 s period; the forwarder sent the event after a restart once and nothing before it again |
| (c) a bad setting refuses the start, naming its variable | 10 settings, each to both | the binary refused all 10 with exit 3 and the variable named; the Python refused all 10 |
| (c) `/readyz` | 4 | 503 `starting` while the catalog is away at the start, then 200; 503 `unhealthy` with the catalog away, 200 once it answers; 503 `shutting_down` on a connection held across SIGTERM |
| (d) SIGTERM during an ingest held 3 s at the catalog | 1 | answered 201, recorded once, exit 0 after 5.9 s |
| (d) SIGTERM during a delivery held 3 s at the catalog | 1 | settled before the exit, recorded once, exit 0 after 4.6 s |
| (e) a handling that panics | 1 | `background_work_ended loop_name='lineage-ingest'`, exit 70 |
| (g) two replicas on one relay timer: A reports a backlog it cannot record (the catalog failing every feed write) and is stopped; B drains it once the catalog is let through; A resumes early in a window B claimed | 1, run three times | A reported no backlog from 0.5 s after it resumed until it next ran a window, 10 or more exports at 250 ms each time; with the hook planted out, every one of them reported the event B drained (two runs) |
| (f) the same traffic through each alone: 9 requests to the doors, 2 per reader, a replay, 3 bus events, 2 staged events, the relay and the forwarder | every export decoded | every export of both presented the pod's token; the 29 metric names, 11 span names and 4 audit action:outcome pairs the binary exported are the Python's; the 7 door:outcome pairs `lineage.events.processed` counted are the same; what only the Python exported is kept on purpose (below) |

30 checks, all passed.

The (b) cases are the nine of `lanes_live.rs` and the model's five ways (`specs/lineage/EventPath.tla`), now through the
binary and with OpenFGA deciding where the slice's test had a decider that denied everything: an event refused is one
signed by a signer that is no delegator, so its writer's grants are asked rather than attested.

**Planted defects**, one at a time, each restored after; each was reported:

| Defect | Reported by |
| --- | --- |
| ready before the start completes (`mark_started` before step 4) | (c) `/readyz` 200 `ready` while starting; and while the catalog was away |
| a lane's death swallowed (the supervisor's end ignored) | (e) the process did not exit |
| SIGTERM that drops a delivery in hand (the lane cancelled, not told) | (d) the delivery unsettled at the exit |
| the doors and the lane judging through different paths (the replay door asking the caller's gate only) | (a) 3 differences: the replays of the corpus's unsigned staged run, of an unsigned and of a forged event, admitted where the Python refuses them |
| a metric recorded at the wrong outcome (the lane's `refused` counted `unrepairable`) | (f) the door:outcome pairs differ |
| an audit record missing on a refusal (the gates' `authz` `deny` not emitted) | (f) the audit sets differ |
| a setting read under the wrong name (`LINEAGE_DELIVERY_ATTEMPT`) | (c) `LINEAGE_DELIVERY_ATTEMPTS=0` did not refuse the start |
| the drain order reversed (the listener stops before `/readyz` turns) | (c) `/readyz` read 200 `ready` after SIGTERM |
| the relay's `elsewhere` hook forgetting nothing | (g) the replica that lost the window reported the drained event's backlog |

## The pilot measured (ADR 0217 point 8)

`scripts/port_lineage_bench.py`, release build of the binary against the Python service as the chart runs it, on this
machine, the same collaborators and one environment, both exporting telemetry. Three runs each, alternating which
service goes first; the median, with the spread (min to max) in brackets.

Measured 2026-11-17, 17:00 to 17:48 UTC, `--runs 3 --requests 100 --events 1000 --feed 3000`: each run of each service on
a namespace of its own holding a seeded feed of 3,000 rows and an empty stream. The machine's one-minute load average
was 14.2 to 15.6 on its four cores before every run (other agents' builds and services beside this one).

| Measure | Python (uvicorn, as the chart runs it) | Rust (release, `lineage serve`) |
| --- | --- | --- |
| Start to `/readyz` 200 | 5.96 s (5.49 to 6.35) | 1.08 s (0.88 to 1.26) |
| Resident memory, idle after the start | 223.9 MiB (223.9 to 224.6) | 14.4 MiB (14.3 to 14.7) |
| Resident memory, after the load | 252.0 MiB (249.6 to 256.5) | 32.3 MiB (25.8 to 34.5) |
| `GET /events?limit=100`, c1: p50 / p95 / p99 | 333 / 431 / 656 ms | 318 / 429 / 517 ms |
| `GET /events`, c1 throughput | 2.93 req/s (2.83 to 3.58) | 3.06 req/s (2.25 to 3.17) |
| `GET /events`, c8: p50 / p95 / p99 | 789 / 1,057 / 1,191 ms | 891 / 1,264 / 1,398 ms |
| `GET /events`, c8 throughput | 9.67 req/s (8.02 to 10.23) | 8.51 req/s (7.48 to 8.93) |
| `GET /events`, c32: p50 / p95 / p99 | 3,282 / 3,829 / 4,054 ms | 3,168 / 3,981 / 4,101 ms |
| `GET /events`, c32 throughput | 10.00 req/s (8.13 to 10.37) | 9.51 req/s (7.87 to 9.76) |
| `POST /api/v1/lineage`, c1: p50 / p95 / p99 | 778 / 1,105 / 1,502 ms | 1,044 / 1,642 / 2,371 ms |
| `POST /api/v1/lineage`, c1 throughput | 1.24 req/s (0.93 to 1.38) | 0.92 req/s (0.54 to 1.18) |
| `POST /api/v1/lineage`, c8: p50 / p95 / p99 | 2,048 / 2,467 / 2,471 ms | 2,365 / 2,819 / 2,823 ms |
| `POST /api/v1/lineage`, c8 throughput | 3.70 req/s (3.62 to 4.86) | 3.23 req/s (2.36 to 4.00) |
| `POST /api/v1/lineage`, c32: p50 / p95 / p99 | 5,158 / 5,228 / 5,238 ms | 4,975 / 5,096 / 5,141 ms |
| `POST /api/v1/lineage`, c32 throughput | 5.99 req/s (4.78 to 6.47) | 6.34 req/s (5.04 to 6.37) |
| Ingest lane, 1,000 signed events from the bus | 2.63 events/s (2.35 to 2.79) | 2.25 events/s (1.87 to 2.40) |
| Image-relevant size | 550 MB venv (lineage alone; 553 MB with the image's three packages), plus the interpreter | 17.4 MB stripped binary (23.6 MB unstripped) |

Every request answered 2xx in every run. The latency percentiles of one run come from 100 requests, so a p99 is close to
that run's slowest request; the spreads are wide because the machine was.

What these numbers show: the binary starts in a sixth of the time and holds a fifteenth of the memory, idle, and an
eighth after the load. What they do NOT show:

- **The collaborators dominate the doors.** Every door reads or writes through the Python catalog fixture, one uvicorn
  process on real pylance over a local directory, and asks OpenFGA on its in-memory store; both services wait on them
  alike. A latency is the service's own work plus the same collaborators', and the difference is the part each service
  adds. A production catalog on S3 and OpenFGA on Postgres shift every figure. The doors' latencies are within each
  other's spread, which says the catalog fixture, not either service, sets them; it does not say the two services cost
  the same.
- **The Rust reads every table through the catalog's doors, the Python reads them itself.** The Python store reads a
  table's rows with pylance from the store, on credentials the catalog vends; the Rust crate links no Lance, so each
  read is the catalog's query door (ADR 0222). Against a catalog that is one process, every Rust read waits in its queue
  with every write: the lane, which reads the run's binding before each record, is where this shows (2.25 against 2.63
  events a second), and a quick run on 200 events and a 200-row feed measured the two lanes equal (7.3 and 7.2). How the
  catalog's query door scales is the measurement that decides whether the read path stays as it is.
- **The machine is shared**: four cores, other builds and agents running beside the measurement (the load averages
  are in the table). Spreads, not single figures, are the evidence.
- **Memory is the process's resident set**, the Python's with its interpreter and every library the venv's import
  graph loads; neither figure is a container's limit, and neither service ran a production-sized feed.
- **Size**: the stripped binary against the venv the image's builder installs, measured by `uv sync --no-dev
  --no-editable` into an empty directory: lineage alone 550 MB, the image's three packages 553 MB. The Python image
  also carries the interpreter; the Rust image would carry its base. No image was built.

## What differs from the Python, on purpose

- **The port is bound before the start**, so `/livez` answers 200 and `/readyz` 503 `starting` while the start runs;
  uvicorn binds only after the lifespan, so a probe meets a refused connection. A door asked before the start completes
  is answered 404 by the shell.
- **The process exits 0 after its drain.** uvicorn raises the SIGTERM it caught again once it has drained, so the
  Python ends by the signal (`-15`).
- **Authorization by OpenFGA directly needs the pinned store and model.** The Python resolves or provisions the store
  by name when they are not pinned; the chart gives lineage the catalog's decision door, which needs neither.
- **OpenFGA's readiness is one check** of `can_read_event_feed` on the root, not a read of the store: the decider the
  port holds has no other question.
- **No thread pool, so no `threadpool.*` metrics**, and the client instrumentations' metrics and spans (httpx,
  botocore, the OpenFGA SDK's `fga-client.request.duration`) and the ASGI layer's inner `http send` and `http receive`
  spans are not made (ADR 0237).
- **`lineage.record` carries no links** to the spans of the events it lands; it carries how many it lands.
- **A delivery held at SIGTERM is finished and settled**, never cut: the Python cancels its handling and asks for the
  delivery again at once. Both record it once.
- **A refusal's message quotes nothing from the event** (ADR 0223): a replay refused for a signer nobody lists answers
  "the signer is not an identity that may sign", where the Python quotes the claimed identity.
- **The debug build takes the CloudEvent type `rask.check.handling-panics` as a handling that panics**, which the
  check of the supervision (e) needs in place of a native library's panic; a release build has no such type.

## The outbox's order, and the backlog a replica did not measure

- **Staged events are drained oldest first by the store's own clock, in both.** The Python listed the outbox through
  pyarrow's S3 filesystem, which reads `LastModified` in whole seconds (measured on RustFS 1.0.3, 2026-11-17: four
  objects staged 30 ms apart read as one second there, `.657`, `.689`, `.717` and `.750` from the store's own listing),
  so it drained one second's events in key order and its `outbox.oldest_age` was up to a second short. It now lists
  through the storage seam (`lakehouse.storage.iter_listed`, boto3's ListObjectsV2), which keeps the store's precision,
  as the Rust's listing does; events written in one instant keep the listing's order, by key. Shown by
  `test_a_tick_drains_events_staged_within_one_second_in_the_order_they_were_staged` on the pinned RustFS
  (`scripts/port_outbox_check.sh`; the suite's S3 stand-in answers whole seconds itself) and by the outbox vectors' new
  listing. No finer order is stamped: the key is the envelope's id, which the drop and the replay address, and no
  consumer's safety rests on the order, since the model's relay picks any staged event (`specs/lineage/EventPath.tla`,
  Pick). The order is the bounded drain's fairness, the age's accuracy and the replay's choice of a run's newest
  staged event, each to the store's precision: milliseconds on RustFS, whole seconds on AWS S3.
- **A replica that did not run the relay's window forgets the backlog it measured** (`Timer::on_elsewhere`, the
  Python's `elsewhere` hook): another replica drained the outbox, and the last reading says nothing of it. Shown by the
  timer's turn vectors, which record each time the hook runs, and by check (g).

## The four facts

- **GitOps:** the binary starts from what the chart renders and from nothing else: the same variables, and the flags
  the chart passes the server today. No variable was added; the port, address and grace stay the chart's flags.
- **Cloud native:** every platform service is named by address (the catalog, the decider, the bus, the secret store,
  the object store, the OTLP gateway); the binary bundles none, and one that is away degrades readiness or retries,
  never ends the process. A loop that dies ends the process for Kubernetes to restart. A store that answers busy is
  asked again within three attempts, as the Python's client asks (ADR 0234), so a burst is absorbed rather than counted
  as a failed tick; a gauge reports only what this replica measured, so a backend summing replicas reads the outbox as
  it is.
- **Lance-namespace/table-idiomatic:** every table is reached through the catalog's doors; the crate links no Lance.
- **Zero trust:** no secret through the environment, only file paths: the bus, catalog, decider, secret-store,
  object-store and OTLP tokens are files read at each use; every request is authenticated by the service itself before
  its body is read; the one gate decides every write. The outbox's listing asks for nothing beyond the relay's own
  prefix, which its policy already grants (`s3:ListBucket` under `_lineage_outbox/`).

## Not done

- **Nothing on the estate runs the binary.** Its image (`.docker/lineage-rust.dockerfile`) and the chart value that
  selects it (`services.lineage.runtime: rust`, its `args` the flags above) are ADR 0239's; the image has not been built,
  and the live read-back (`docs/architecture/deployment.md` § The Rust lineage) has not run.
- The binary was run against the catalog's decision door for the doors' corpus only (ADR 0236), by a second catalog
  process with authorization on over the first one's tables; checks (b) to (g) still decide at OpenFGA. It was not run
  with the ServiceAccount verifier, nor with the docs pages (`LINEAGE_DOCS`), which are not ported.
- Lance's own metrics belong to a crate that links Lance (ADR 0237).

## Consequences

- `lineage` stands at 4,832 of its 5,242 budgeted lines (`docs/rust-port.json`), 406 more than before this slice:
  `service.rs` 340, `signals.rs` 27, `main.rs` 19, and the counting and spans in the store (+15), the doors' ingest
  (+13), the tick (+19) and the walk (+19, a stepper in place of the closure). The unification paid back 61: `state.rs`
  33 (the second `Feed` adapter and the doors' own copies of the gate's collaborators), `intake.rs` 12 (`Counted`),
  `relay.rs` 4, `doors/mod.rs` and `doors/tables.rs` 4 (`sendable`, the blocking walk), the dlq doors' `admission` and
  the lane's `Tally` folded into what replaced them.
- `lakehouse-core` grows by 170 lines for what every service runs on (the supervisor 64, the timer's metrics 73 across
  `timers.rs` and `instruments.rs`, the process's audit trail 9, the remote parent and the deadline's span 28, the
  shell's grace 14), and `lakehouse-bus` by 96 (the bus's metrics and spans).
- The follow-ups (a name the catalog refuses answered as the Python answers it, ADR 0226; the timer's `elsewhere` hook;
  the store asked again when busy, ADR 0234) bring `lineage` to 4,882 lines (+50) and `lakehouse-core` to 6,379 (+39).

- `scripts/port_lineage_check.sh` replaces the doors check's example server: `scripts/port_doors_check.sh` runs the
  binary, and `rust/crates/lineage/examples/doors_serve.rs` is deleted.
