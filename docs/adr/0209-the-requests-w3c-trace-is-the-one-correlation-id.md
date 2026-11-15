# 0209. The request's W3C trace is the one correlation id (2026-11-15)

**Status:** accepted. Source: XC-048 (#210), counted in map #478 and placed after XC-199 (#787), whose CloudEvents
envelope this builds on ([ADR 0207](0207-the-lakehouse-runs-no-dapr-and-each-pod-asks-the-bus-for-a-session.md)).

## Context

Two correlation paths ran side by side. Every lakehouse service ran `RequestIDMiddleware`, which took an inbound
`X-Request-ID` as given or minted one, echoed it, printed it on every log line and stamped it on every compliance record
as `audit.request_id`, so text a client chose reached the audit trail unchecked. Beside it the log lines carried the W3C
trace id where `opentelemetry-instrument` ran, and a problem body named it as `instance`; with `observability.enabled`
off no tracer provider existed, so a request had no trace id at all.

No durable record carried either id. A control event, a staged outbox object (the event's JSON alone) and a commit's
`rask.*` properties named no trace, and a relay that delivered a staged event published it in the relay tick's own trace,
so an event delivered late could not be joined to the request that caused it (criteria 1 and 4).

The reference answers. W3C Trace Context Level 1 defines `traceparent` and `tracestate` as the one interoperable carrier
of a trace across HTTP. CloudEvents' distributed tracing extension carries the same two fields as attributes of an event's
envelope, which is what `lakehouse.bus` already writes on every publish since ADR 0207. OpenLineage defines no trace
facet, and a custom one would need a new schema under a new immutable tag of `spec/facets/rask/`.

## Decision

1. **The request's W3C trace is the one correlation id.** Its 32-hex trace id joins a request's log lines (`[trace_id]`),
   its audit records (`audit.trace_id`, audit format `2.0`), its problem body's `instance`, its commits and its events.
   A caller's well-formed `traceparent` is continued, so its own trace reaches every record its request causes; the id is
   correlation, never evidence, and nothing grants or attributes on it. `lakehouse.context` holds the reads
   (`current_trace_id`, `current_traceparent`) and `continuing(traceparent)`, which runs later work in a recorded trace.
2. **`RequestIDMiddleware`, `request_id_ctx` and `X-Request-ID` are deleted.**
3. **Every request runs in a trace, whatever the deployment.** The lance app factory instruments every app with
   OpenTelemetry's FastAPI instrumentation, outermost, and installs an SDK tracer provider with no exporter when none is
   set, so a process run without `opentelemetry-instrument` mints trace ids and exports nothing. Under
   `opentelemetry-instrument` the distro's provider and instrumentation stand, and the factory adds nothing.
4. **An event carries its trace on its envelope, never in its signed data.** The CloudEvents envelope's `traceparent` and
   `tracestate` (the distributed tracing extension) name the trace of the request that caused the event. The outbox
   stages the whole envelope, in the trace of the request that staged it, and a publish of an envelope that names a trace
   runs in it, so a relay's copy (the control relay, lineage's relay, the DLQ replay) continues the request's trace rather
   than starting one. The event's data, and its signature, are unchanged.
5. **A commit a catalog door makes records `rask.traceparent`**, beside its other `rask.*` properties. The reconcile that
   announces a commit whose announcement was lost announces it in that trace. A stock client's commit through
   `version/create` carries none; its announcement is made in that request's trace, and a reconcile announces it in the
   reconcile's own.

## Consequences

- An event delivered late, a lineage edge and a commit can be joined to the request that caused them by one id, in the
  logs, the audit trail, the bus and the version log alike.
- Lineage's feed keeps each event's data, not its envelope, so a feed row names no trace; joining a row to its request
  goes through the bus or the commit's `rask.traceparent`. A trace column on the feed is a schema change of a provenance
  table and is not made here.
- A staged outbox object is now an envelope, not a bare event: every reader takes the event from its `data`, and bytes
  that hold no envelope are poison.
- The audit format is `2.0`: a compliance query reading `audit.request_id` matches nothing and can tell it is reading a
  format it was not written for.
- Four facts. GitOps: nothing new to declare. Cloud native: W3C Trace Context and CloudEvents are the standards, and the
  OTel backend stays the platform's. Lance-idiomatic: a transaction property is Lance's own commit metadata. Zero trust:
  a caller's text reaches no record unchecked (the W3C grammar is held to), and a trace id authorizes nothing.
