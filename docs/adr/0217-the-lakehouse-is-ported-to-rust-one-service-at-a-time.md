# 0217. The lakehouse is ported to Rust one service at a time, behind the doors it already serves (2026-11-17)

**Status:** accepted as the goal and the method; lineage is the pilot and nothing is swapped on the estate under it yet.
Source: the owner, 2026-11-17: "The goal is to have a SOTA lakehouse, performance, and security, understandable for
humans, lean and clean code. Proven and bugfree", then "setup a goal and try to port the code to rust following these
principles, keep ADR of features".

## Context

Measured 2026-11-17 at `ed1f446`: the three services and their library are 85,503 lines of Python, of which 42,040
execute (the rest is prose), under 66,673 executing lines of tests. A duplicate-code scan found two repeated blocks, so
the size is not copy-paste. It is plumbing that every handler repeats (the caller's token is declared on 157 routes
and the settings on 152), 171 single-rule guard functions, 525 thread hops around pylance, and long multi-step
operations whose ordering is argued in comments.

Lance's core is Rust, and the pinned docs list SDKs for Python, Rust and Java only (`lance_docs/lance/sdk_docs.md`), so
Rust is the one language that reaches Lance with no binding to own. Every contract a client sees is already language
neutral: the doors (`docs/*-openapi.json`), the events (`docs/COMMIT-EVENTS.md`), the authorization model
(`model.fga`) and the chart. `docs/operations.json` lists the 189 operations with the gate in front of each.

## Decision

1. **The goal.** A lakehouse that is fast, secure, small enough for one person to read, and checked: the Rust
   services serve the same doors and events as the Python ones, and replace them when every operation in the ledger
   passes on Rust.
2. **A plane of its own.** The Rust code is a Cargo workspace under `rust/`, beside the Python planes and
   `frontend/`. `rust/crates/lakehouse-core` holds what every service runs on and depends on no Lance crate;
   a service is a crate of its own. Only a crate that reads or writes a table depends on `lance`.
3. **One service at a time, smallest first:** lineage (the pilot), then maintenance, then the catalog. A Rust
   service ships as its own image and is chosen by a chart value, so a swap is one commit and the estate may run
   mixed. The lance crate and pylance move to the same major version before both write one table.
4. **Decisions are pure functions; effects are a thin shell.** What a door decides (parse, judge, plan the rows)
   takes values and returns values. Reading a table, calling OpenFGA and publishing are the shell around it. This is
   what makes the code short to read and the decisions checkable without a store.
5. **Parity is shown, in three ways.**
   - *Vectors*: a Python script runs the Python functions over a corpus and writes inputs with their results
     (`rust/vectors/`); the Rust tests assert the same results. A deliberate difference is listed in the feature's
     ADR, never left for the vectors to hide.
   - *Black-box tests* speak HTTP, NATS and S3 to a running service, so one suite judges either implementation.
   - *Models*: the lifecycle and the event path are specified in TLA+ and checked with TLC; a model's traces are
     replayed against the running service.
6. **A line budget per crate, enforced.** `rust/budget.json` holds the most lines of production Rust a crate may
   have: twice the executing Python it replaces (lineage: 5,242 for 2,621). Test code and vectors are not counted.
   Rationale and measurements go to an ADR; a comment says why, in a sentence. The rule was first "at or below the
   Python". Measured 2026-11-17 with about half of lineage ported, like for like: the store is 1,146 lines of Rust
   for 618 of Python, authorization 830 for 446, the bus 1,047 for 642 after a pass that removed its duplicates. The
   Rust writes out what Pydantic, FastAPI, nats-py and rows held as dicts give the Python, so the first rule could
   be met only by dropping behaviour or by code that is harder to read. Owner, 2026-11-17: "Cap at 2x Python".
7. **Features are tracked by generation and by record.** `docs/rust-port.json` is generated from
   `docs/operations.json` and from what the Rust plane declares it serves, so the ledger cannot claim a door the
   code does not hold. Each ported feature area gets an ADR naming what moved, how parity was shown and what
   differs.
8. **The pilot decides the rest.** Lineage in Rust is judged on: every lineage operation and delivery passes the
   black-box suite, the crate is within its budget, and its memory and latency are measured against the Python
   service's. The decision to port maintenance and the catalog is taken on those numbers.

## The four facts

- **GitOps:** an implementation is chosen by a chart value in git; no step outside the chart swaps a service.
- **Cloud native:** each Rust service is one static binary in one image, a declarative workload like the one it replaces.
- **Lance-idiomatic:** the Rust services use the `lance` and `lance-namespace` crates and the same namespace doors;
  a claim about Lance is settled from `lance_docs/` or measured on the pinned crate.
- **Zero trust:** token verification, vending and signing are ported behind black-box tests written first, and a
  credential still reaches a service only as a projected token or a secret read at run time.

## Consequences

- Two implementations exist until the last swap, and a door change lands in both in one commit.
- The Python tests that import service internals do not judge the Rust services; the black-box suite is new work
  and the larger part of the port's cost.
- What the comments record today (measurements, rulings) is carried into ADRs as each module is ported, or it is lost.
- This work is outside the Phase 1 map. The models in point 5 serve Phase 1's "events correct" and "resilient"
  criteria; the port itself does not, and it is paused whenever a `blocks-prod` row needs the time.
