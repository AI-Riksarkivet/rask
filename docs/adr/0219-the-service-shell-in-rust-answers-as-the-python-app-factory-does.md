# 0219. The service shell in Rust answers as the Python app factory does (2026-11-17)

**Status:** accepted; the second feature of the port (ADR 0217). No service is served in it yet. Source: the owner,
2026-11-17 ("try to port the code to rust following these principles, keep ADR of features").

## Context

Every Python service is built by one factory (`lakehouse.lance_app.build_lance_service_app`), which gives it the same
probes, the same error bodies and the same limits on every request whatever door it reaches. A Rust service needs the
same shell before it can serve its first door, and a client must not be able to tell the two apart by an error.

## Decision

1. **`lakehouse_core::service` is the shell**, five modules and no Lance crate:

   | Rust | Holds | Python it answers for |
   | --- | --- | --- |
   | `problem` | the spec's 24 error codes with their statuses and names, the problem body, a door's refusal (`NsError`) | `lakehouse.problem`, `lakehouse.ns_errors` |
   | `lifecycle` | starting, serving, draining | `lakehouse.lifecycle` |
   | `health` | the background watch of what a service depends on, and the readiness it gives | `lakehouse.health`, `lakehouse.schemas.health` |
   | `trace` | the trace a request belongs to, which every error body names as its `instance` | `lakehouse.context` |
   | `shell` | the probes, the router's layers, and serving until SIGTERM | `lance_app`, `probes`, `body_limit`, `deadline`, `panic_catcher`, `no_store`, `draining` |

2. **A request passes the same layers in the same order:** its trace, the no-store headers, the panic answer, the
   deadline, the body cap, then the answer to a path or method no door serves.
3. **A request past its deadline is answered and runs on**, as in Python. The door runs as a task of its own; when the
   deadline passes its caller gets the 503 and the task is left to finish, because stopping a write halfway leaves
   less known than letting it end. A caller that hangs up before the deadline stops its request.
4. **A refusal is a value.** A door returns `NsError` (a code, a name, a message); what went wrong inside the service
   is answered as "Internal Server Error" for every status of 500 and above, as in Python.
5. **Parity is shown by recorded exchanges** (`rust/vectors/shell_http.json`): `scripts/gen_port_vectors.py` builds an
   app with the Python factory, puts 42 requests to it and records each answer. The Rust test puts the same requests
   to the Rust shell and compares the status, the `content-type`, `cache-control`, `vary` and `allow` headers, the
   body, and whether the error names the trace that was sent. The exchanges cover both probes in every state of the
   lifecycle and of a required and an optional dependency, a missing path and method, the body cap declared and
   undeclared, the deadline, a panic, and a refusal with each of the 24 codes and under a name of its own.
6. **The drain is tested in a real process:** a served shell is sent SIGTERM with a request open; the request is
   answered, the probe state is draining, and the listener takes no new connection.

## What differs from the Python, on purpose

- **A GET door answers HEAD**, and `Allow` lists it. The Python doors refuse HEAD with 405. HEAD runs the same door
  with the same checks and sends no body.
- **A panic's body says the request panicked.** Python's says it failed inside a native library, which is what a panic
  is there. The status, code and type are the same.
- **Only a well-formed version 00 `traceparent` continues a trace** (the rule `lakehouse.context.is_traceparent`
  states). Python's instrumentation also continued a header of a later version.

## Not ported yet

The body of a request that fails validation (the `errors` list), logging and telemetry export, the audit trail, the
docs pages, the store-outage answer (`lakehouse.outage`), and a problem's extra members. Each arrives with the first
door that needs it.

## Consequences

- `lakehouse-core` is 586 of its 11,234 budgeted lines.
- A service is served by building its doors as a router and handing it to `Shell::router` and `Shell::serve`.
- A change to the Python factory's answers changes a recorded exchange, and `make rust-check` fails until the Rust
  shell agrees.
