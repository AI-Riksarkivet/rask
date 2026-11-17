# 0237. A service tells the platform about itself in Rust as in Python: its telemetry, metrics, audit trail and log lines (2026-11-17)

**Status:** accepted; a feature of the port (ADR 0217), and its fifth zero-trust code after ADR 0220, ADR 0223, ADR 0230
and ADR 0234. It exports a Rust service's traces, metrics and logs to the platform's gateway as the pod's own identity
(ADR 0215), records the metrics the alert rules and dashboards read by name, emits the `lance.audit` trail, and writes
the service's log lines. No Rust service starts its telemetry yet. Source: the owner, 2026-11-17 ("try to port the code
to rust following these principles, keep ADR of features").

## Context

A Python service is launched under `opentelemetry-instrument` when the chart's `observability.enabled` is on
(`lance.otelEnv`): the SDK's OTLP/HTTP exporters send to `observability.export.endpoint`, each request carrying the
pod's projected ServiceAccount token for `observability.identity.audience` through the credential entry point
`rask_service_account` (`lakehouse.otlp_identity`). The services record their own instruments
(`lineage.core.metrics`, `lakehouse.bus_metrics`, `lakehouse.outbox_metrics`, `audit.records`), the FastAPI
instrumentation records the HTTP server's, `lakehouse.governed.audit` writes the governance trail on the logger
`lance.audit` (which the SDK's logging handler exports as that instrumentation scope), and `lakehouse.app.setup_logging`
writes every record to stdout, stamped with the request's trace (ADR 0209). The platform reads all of it by name:
`deploy/alerting/rules.yml` selects 19 series names of these instruments (measured 2026-11-17), the development gateway routes the trail by its
scope and admits it by the token's subject, and its file-log pipeline drops a lakehouse pod's stdout line that begins
with a date, because the OTLP copy of that line already arrived.

## Decision

1. **`lakehouse_core::telemetry` is the port**, six modules and the shell's layer:

   | Rust | Holds | Python it answers for |
   | --- | --- | --- |
   | `settings` | what the chart's environment names: the endpoint of each signal, the exporters, the token file, the timeout, the metric interval, the resource | the SDK's reading of `OTEL_*` under `opentelemetry-instrument` and the distro, `otlp_identity.session`'s refusal |
   | `identity` | the bearer a token file's contents give, and the one HTTP client every exporter sends with, reading the token for each export | `otlp_identity` (`_ProjectedToken`, `session`), `machine_identity.read_identity_token` |
   | `instruments` | `LineageMetrics`, `BusMetrics`, `OutboxMetrics`, their closed attribute sets and their series created at 0 | `lineage.core.metrics`, `lakehouse.bus_metrics`, `lakehouse.outbox_metrics` |
   | `audit` | `Audit`, `AuditAction` (the 42 actions), `AuditOutcome`, `audit.records` | `lakehouse.governed.audit` |
   | `logging` | the `tracing` subscriber, the line, `RASK_LOG_LEVEL`, the OTLP log record under the writer's module | `setup_logging`, `CorrelationFilter`, `DiagnosticFormatter`, the SDK's `LoggingHandler` |
   | `http` | each request as a SERVER span and in the HTTP server metrics, the probes excluded | `opentelemetry-instrumentation-fastapi` under `OTEL_SEMCONV_STABILITY_OPT_IN=http` |
   | `mod` | `Telemetry`: the three providers, `flush`, `shutdown`, the count of failed exports | `opentelemetry-instrument`'s configuration |

   `Shell::instrumented_router` puts the `http` layer just inside the request's trace, so a 503 past the deadline and a
   panic's 500 are measured as the caller saw them.

2. **The metrics keep the Python's names, units, kinds and attribute keys**, because an alert rule selects by them:

   | Scope | Metric | Kind | Unit | Attributes |
   | --- | --- | --- | --- | --- |
   | `lance.lineage` | `lineage.events.processed` | counter | `{event}` | `lance.lineage.outcome`, `lance.lineage.door` |
   | `lance.lineage` | `lineage.ingest.duration` | histogram, 0.005 to 10 s | `s` | none |
   | `lance.lineage` | `lineage.forward.events` | counter | `{event}` | `lance.lineage.outcome` |
   | `lance.lineage` | `lineage.forward.stalled` | counter | `{tick}` | none |
   | `lance.lineage` | `lineage.signature.verified` | counter | `{event}` | `lance.lineage.identity` |
   | `lance.lineage` | `lineage.signature.refused` | counter | `{event}` | `lance.lineage.reason` |
   | `lance.bus` | `bus.publish.refused` | counter | `{event}` | `lance.bus.topic`, `lance.bus.reason` |
   | `lance.bus` | `messaging.client.operation.duration` | histogram, messaging buckets | `s` | `messaging.system`, `messaging.operation.name`, `messaging.destination.name`, `error.type` |
   | `lance.bus` | `bus.deliveries` | counter | `{message}` | `lance.bus.consumer`, `lance.bus.outcome` |
   | `lance.bus` | `bus.pulls.failed` | counter | `{pull}` | `lance.bus.consumer` |
   | `lance.bus` | `messaging.process.duration` | histogram, messaging buckets | `s` | `messaging.system`, `messaging.operation.name`, `messaging.destination.name`, `messaging.consumer.group.name` |
   | `lance.outbox` | `outbox.events.staged`, `.published`, `.drained`, `.stranded`, `.refused`, `.poison_dropped`, `outbox.publish.failed`, `outbox.stage.failed`, `outbox.drop.failed` | counter | `{event}` | `lance.outbox.lane` |
   | `lance.outbox` | `outbox.depth` | gauge, registered at the first measurement | `{event}` | `lance.outbox.lane` |
   | `lance.outbox` | `outbox.oldest_age` | gauge, registered at the first measurement | `s` | `lance.outbox.lane` |
   | `lance.audit` | `audit.records` | counter | `{record}` | `action`, `outcome` |
   | `lance.http.server` | `http.server.request.duration` | histogram, 0.005 to 10 s | `s` | `http.request.method`, `url.scheme`, `network.protocol.version`, `http.route`, `http.response.status_code`, `error.type` |
   | `lance.http.server` | `http.server.active_requests` | up-down counter | `{request}` | `http.request.method`, `url.scheme` |
   | `lance.http.server` | `http.server.request.body.size`, `http.server.response.body.size` | histogram, the SDK's buckets | `By` | as the duration |

3. **Every export presents the projected token, read for that export.** The OTLP exporters are the SDK's; the HTTP
   client they send with is lakehouse-core's (`ProjectedTokenClient`, the SDK's `HttpClient` seam), which reads the
   token file, trims it, and sets `Authorization: Bearer <token>` marked sensitive. An unreadable, empty or
   unheaderable token sends nothing; no export goes without the token. The client follows no redirect and uses no
   proxy, and no error or line quotes the token or its file. The SDK exports from threads of its own; the client sends
   on the service's runtime, so an export never runs on a request's task.
4. **An export that fails costs a count and a line, and nothing else.** A refusal, a transport failure or a token not
   presented is counted (`Telemetry::export_failures`) and said on stdout (`telemetry_export_failed`, never itself
   exported). The SDK retries within the timeout, its queues are bounded, and `flush` and `shutdown` answer nothing,
   so no request waits on the gateway and no failure reaches a caller.
5. **The trail is `Audit`**, a handle the service takes from `Telemetry::audit(enabled)` with the chart's
   `auth.audit`. A record is an OTLP log record under the scope `lance.audit`, body `audit`, severity INFO, with
   `audit.format` `2.0`, `audit.action`, `audit.outcome`, `audit.trace_id` (`-` outside a trace), `audit.subject`,
   `audit.resource` and each field under `audit.`, and the same stdout line Python writes. It is emitted whatever
   `RASK_LOG_LEVEL` says, as Python's dedicated logger is, and counted in `audit.records`. An action is an
   `AuditAction(&'static str)` constant, so a client names its own vocabulary the same way and no request text becomes
   an action.
6. **The line is Python's**: `%Y-%m-%d %H:%M:%S,mmm LEVEL   module.path [trace id] — message key=repr(value)`, in UTC,
   with `::` in a module path written as `.`. Its leading date is what the platform's file-log pipeline drops a line by
   when its OTLP copy exists. The same event is an OTLP log record whose scope is the module (as Python's is its logger's
   name), with the request's trace id.
7. **Off unless the chart turns it on.** No `OTEL_EXPORTER_OTLP_ENDPOINT` is export off: every handle works and only
   stdout lines leave the process. With it, an exporter other than `otlp` or `none`, a protocol other than
   `http/protobuf`, or no `RASK_OTLP_TOKEN_FILE` refuses the start.

## The dependency: the SDK, measured

Measured 2026-11-17 on this machine (4 cores shared with four other builds, `-j 2`, `CARGO_INCREMENTAL=0`):

| | `opentelemetry` 0.33 + `opentelemetry_sdk` + `opentelemetry-otlp` (`http-proto`, no client feature) + `opentelemetry-http` | OTLP/JSON by hand |
| --- | --- | --- |
| Crates added to `Cargo.lock` | 16 (311 to 327): the four above, `opentelemetry-proto`, `prost`, `prost-derive`, `async-trait`, `futures-executor`, `futures-macro`, `rand` 0.9 with `rand_chacha` and `rand_core`, `itertools`, `either`, `anyhow`. No `tonic`, no second HTTP client, no second TLS stack: the exports go over the workspace's `reqwest` 0.13 | 0 |
| Clean build of `lakehouse-core` (library, debug) | 140 s before, 215 s after, this feature's own code included; both on a machine the other builds also loaded, so the 75 s is approximate | about the same as before |
| Production lines to write | the SDK glue: `mod` 124, `identity` 80 | an estimated 700 to 900 more: the protobuf or JSON encoding of three signals, cumulative aggregation by attribute set, explicit-bucket histograms, asynchronous gauges, two batch queues and a periodic reader, span ids, retry |
| A bearer read for every export | yes, through the SDK's `HttpClient` trait, with no fork | yes |

The SDK fails none of the criteria, so it is used. `opentelemetry-proto` is also what the tests decode the exports with.

## How parity was shown

- **Vectors through the real Python** (`scripts/_port_telemetry_vectors.py` → `rust/vectors/telemetry.json`, 101
  lines, the same on every run). The real instruments, read through an in-memory reader on the process's meter
  provider, driven through 36 recordings with 5 snapshots of 22 metrics; a real app from the lance app factory under the
  FastAPI instrumentation as the chart configures it, put 8 requests (a route, a query with a `traceparent`, a 500, a
  POST with a body, a 405, an unmatched path and both probes), giving 6 server spans and 4 HTTP metrics; the real
  `audit` and `audit_read` through the SDK's logging handler, 7 cases giving 6 records and their lines, the 42 actions,
  4 outcomes and the `audit.records` count; the real `otlp_identity.session` through a capturing adapter, 8 cases of 9
  exports (a token, kubelet whitespace, a rotation, empty, whitespace only, missing, recovered, no file named); the real
  exporters, metric reader, distro defaults and resource over 16 environments; the real `setup_logging` over 12
  `RASK_LOG_LEVEL` values; the real handler's line for 6 records. The Rust tests (`tests/telemetry_vectors.rs`, 8)
  replay each through a real export to an OTLP/HTTP sink that decodes the protobuf, and assert every metric's scope,
  name, unit, kind, buckets and points at every snapshot, the HTTP metrics' attributes and counts, each span's name,
  kind, status, trace and attribute keys, every audit record and line, each export's header, and the settings, levels
  and lines.
- **The names cross-check**: the set of metric names the Rust exports after driving every handle equals the set the
  Python recorded, so an alert's metric cannot vanish in the port without a test failing.
- **Black-box against a sink** (`tests/telemetry.rs`, 7): every signal arrives presenting the token and the trace id
  joins the span, the record and the stdout line; a rotated token is presented on the next export; the trail arrives
  under `lance.audit` and a disabled trail sends nothing; a gateway that is not listening, or that answers 401 or 503,
  leaves every request answered in under a second and the service serving, with each failed export counted and said;
  an unreadable token sends nothing and the next readable one is presented; export off sends nothing and still logs.
- **Against a real collector** (`tests/telemetry_collector.rs`, ignored unless `.localbin/otelcol-contrib` is fetched):
  otelcol-contrib 0.157.0, the development gateway's version, pinned in `scripts/toolchain.sha256` (linux x86_64), run
  with the development gateway's receiver, `oidc` token check against a JWKS file, `attributes/sender` and the two audit
  filters, with file exporters in place of the store and no `k8sattributes`. A token signed by a P-256 key generated at
  test time: of another audience, refused (each export counted, nothing stored); of the telemetry audience for an
  account outside the lakehouse, admitted with its trail dropped; for `rask-sa-lineage`, admitted with its trail on the
  audit path, stamped with that subject, and never on the general log path.
- **Planted defects**, each restored after; each failed the tests named:

  | Defect | Failed |
  | --- | --- |
  | `lineage.events.processed` renamed | the instruments' vectors, the names cross-check |
  | `bus.pulls.failed`'s unit changed | the instruments' vectors |
  | `messaging.consumer.group.name` dropped from a delivery's duration | the instruments' vectors |
  | the token read once and kept across exports | the rotation and unreadable-token black-box tests, the identity vectors |
  | the audit scope named `lance.audits` | the audit black-box test, the audit vectors |
  | an export's failure propagated out of `flush` | the gateway-down, gateway-refuses and unreadable-token black-box tests |
  | the token sent untrimmed | the every-signal black-box test, the identity vectors |

## What differs from the Python, on purpose

- **No `OTEL_EXPORTER_OTLP_ENDPOINT` is export off.** Python exports to `localhost:4318` wherever it is launched under
  `opentelemetry-instrument`; the chart sets the endpoint exactly when it adds the launcher.
- **Only `http/protobuf` is spoken.** Python's distro defaults to gRPC where the protocol is unset; the chart always
  sets `http/protobuf`. An unset protocol is taken as `http/protobuf`; `grpc` refuses the start.
- **A token file that is not named, or an exporter other than `otlp` or `none`, refuses the start.** Python's launcher
  logs the failed configuration and runs with no export.
- **`OTEL_EXPORTER_OTLP_COMPRESSION`, `_HEADERS`, the per-signal `_TIMEOUT` and certificates are not read**; the chart
  sets none. A timeout that is not a positive number is the default 10 s.
- **The server span has no `client.address` or `client.port`**: the shell's listener does not hand the peer's address
  to the layer. Every other key of the Python span is there. The ASGI instrumentation's INTERNAL `http send` spans are
  not made, and the HTTP metrics' scope is `lance.http.server`, not the FastAPI instrumentation's name.
- **The probes are excluded by exact path** (`/livez`, `/readyz`, `/healthz`, `/api/health`, `/metrics`); Python's
  `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` patterns match anywhere in a path.
- **No HTTP client spans or metrics.** Python's `httpx`, `requests`, `urllib3`, `aiohttp-client` and `botocore`
  instrumentations made a CLIENT span and `http.client.*` metrics for every outbound call (the catalog, the object
  store, the secret store, OpenFGA); no rule or dashboard reads them. Nor are the Python SDK's own exporter metrics
  (`otel.sdk.exporter.*`) made.
- **A log record carries no `code.*` attributes and no span id**: the trace id joins it to its request.
- **Lines are UTC**, as a pod's clock is; Python writes local time. A float diagnostic is Python's `repr` for finite
  values; a string's control characters are escaped, other characters written as they are (Python escapes the few
  non-printable ones above U+00A0 as well).
- **`CRITICAL` keeps no line**: nothing in the lakehouse logs above ERROR, and `tracing` has no higher level.
- **`service.instance.id` is a new UUID each start**, as Python's SDK makes one; `telemetry.sdk.*` name Rust.

## The four facts

- **GitOps:** every endpoint, the audience's token file, the interval and the resource come from `lance.otelEnv` in
  the chart; nothing is set by hand.
- **Cloud native:** the gateway is the platform's, named by address; the product chart renders no collector (ADR 0215).
- **Lance-namespace/table-idiomatic:** not applicable; no table is read or written.
- **Zero trust:** no telemetry secret exists. The identity is the projected token, scoped to the telemetry audience,
  short-lived, read for each export, never logged, never sent elsewhere, and never replaced by an anonymous export.

## Not ported yet

- Lineage starting it, its lanes, relay and doors recording through the handles, and the spans of the bus and the store
  (`publish <topic>`, `process <subject>`, `lineage.record`): ported in ADR 0238, the spans on the tracer `install` makes
  the process's.
- **Lance's own metrics** (`lakehouse.lance_metrics`): they belong to a crate that links Lance.

## Consequences

- `lakehouse-core` holds 1,119 counted lines of this feature (instruments 382, logging 223, http 141, mod 124, audit
  96, identity 80, settings 73), and the shell, trace and signing modules 35 more, within its budget.
- A change to an instrument's name, unit, kind or attributes, to the audit record or to the line changes a vector, and
  `make rust-check` fails until the Rust agrees.
