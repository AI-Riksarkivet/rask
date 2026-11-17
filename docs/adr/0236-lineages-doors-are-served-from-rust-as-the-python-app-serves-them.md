# 0236. Lineage's doors are served from Rust as the Python app serves them (2026-11-17)

**Status:** accepted; a feature of the port (ADR 0217), and the first that serves a door. Lineage's ten operations are
served by `rust/crates/lineage`'s router behind the shell of ADR 0219; nothing on the estate runs it yet, because the
service's settings and its `main` wiring arrive with the lanes slice. Source: the owner, 2026-11-17 ("try to port the
code to rust following these principles, keep ADR of features").

## Context

Lineage publishes eight doors beside the two probes (`docs/lineage-openapi.json`, `docs/operations.json`): the commit
feed and its projection, what was derived from a table, the HTTP ingest, and the four admin doors over the outbox and
the parked deliveries. In Python they are `lineage.api.v1.endpoints` (`events`, `tables`, `ingest`, `dlq`) over the
gates of `lineage.api.fga_deps`, the caller of `lineage.api.security`, and the app `lineage.main` assembles with
`build_lance_service_app` and the judge before the body (`lakehouse.admission`). Everything a door needs was ported
already: the shell (ADR 0219), the caller (ADR 0220), the gates (ADR 0226), the store (ADR 0228), the signature
(ADR 0223), the intake (ADR 0224) and the outbox (ADR 0234). What remained was the doors themselves: their order of
checks, their validation and their answers, member for member.

## Decision

1. **Three new places in the crate**: `state` (what a door is served with, each collaborator a value), `doors` (one
   module per group of doors, and what they share in `doors/mod.rs`) and `app` (`router(state) -> axum::Router`, which a
   service hands to `Shell::router`). `SERVED` names the ten operations, the probes being the shell's.

   | Method | Path | Gate | Rust handler | Python it answers for |
   | --- | --- | --- | --- | --- |
   | GET | `/events` | every table a row names, `can_get_metadata` (`DatasetFilter::governed`) | `doors::events::events` | `events.get_events` |
   | GET | `/events/projection` | `can_read_event_feed` on the root (`require_event_feed_reader`) | `doors::events::projection` | `events.get_events_projection` |
   | GET | `/tables/{name}/derived` | `can_get_metadata` on the table (`require_metadata_access`), each derived table filtered | `doors::tables::derived` | `tables.get_derived` |
   | GET | `/admin/dlq` | an authenticated caller; each staged event filtered as `scope_unrecorded` names it | `doors::dlq::list` | `dlq.list_dlq` |
   | POST | `/admin/dlq/{run_id}/replay` | visibility as listed, then `bus_authz` over the staged bytes and `ingest_authz` over the operator | `doors::dlq::replay` | `dlq.replay_dlq` |
   | GET | `/admin/dlq/parked` | `can_read_parked_events` on the root (`require_parks_reader`) | `doors::dlq::parked` | `dlq.list_parked` |
   | POST | `/admin/dlq/parked/{run_id}/replay` | `can_replay_parked_events` on the root, then `bus_authz` per delivery | `doors::dlq::replay_parked` | `dlq.replay_parked` |
   | POST | `/api/v1/lineage` | the caller bound as author, names resolved as the caller, `ingest_authz` | `doors::ingest::ingest` | `ingest.ingest_event` |
   | GET | `/livez`, `/readyz` | open | the shell's probes (ADR 0219) | `lakehouse.probes` |

2. **The state takes constructed values**, so a test builds it on real collaborators or stand-ins:

   ```rust
   pub struct State {
       pub authenticator: Arc<Authenticator>,
       pub fga_enabled: bool,
       pub decider: Option<Arc<dyn AccessDecider>>,
       pub catalog: String,                 // the dataset namespace the catalog names its tables under
       pub root: String,                    // the object every estate privilege is checked on
       pub store: Store,
       pub names: Arc<dyn Resolver>,        // `Names` plus a namespace's uuid; `CatalogNames` implements it
       pub outbox: Option<Arc<Outbox>>,
       pub publisher: Option<Publisher>,    // the bus, the topic and the publish deadline a replay announces on
       pub keys: Arc<dyn PublicKeySource + Send + Sync>,
       pub signing: Signing,
   }
   pub fn router(state: State) -> axum::Router
   ```

3. **A request meets the checks in the Python app's order.** The caller is judged from the headers before anything
   else is read, the body included (`doors::caller`, the judge of `lakehouse.admission`); a gate the Python declares as
   a route dependency (`/tables`, `/admin/dlq/parked*`) runs before the query is validated, as FastAPI solves
   dependencies first; then the query (422), then the handler's own refusals in its order. A path segment that decodes
   to a `/` is no segment (Starlette routes on the decoded path).
4. **The query is validated as pydantic and Starlette validate it** (`doors::Query`): the last value of a name wins, `+`
   is a space, an integer is trimmed, signed, may hold single underscores between digits and a fraction of zeros, and
   is refused past 4,300 digits; a boolean is one of pydantic's twelve words in any case; each failing field is listed
   in the order the door declares it, with pydantic's message and type. An integer past 64 bits is held at the 64-bit
   bound, which reads the same rows. The ingest body is read as FastAPI reads a `Body()` parameter: JSON only under
   `application/json` or `application/*+json`, a UTF-8 byte-order mark dropped, CPython's error position (a trailing
   comma's at the comma), `null` as missing and any other value as no dictionary.
5. **A refusal is answered by the shell's problem model**, and a failed validation by its envelope with FastAPI's
   `errors` list (`Refusal::Invalid`), which ADR 0219 left to the first door that needed it.
6. **The audit records are one function** (`doors::audit`) at the call sites the Python audits: each replay's success or
   failure. It records on the process's trail (`telemetry::audit::emit`, ADR 0238).
7. **The feed's governed page reads as the Python one does**: at most three reads of up to 2,000 rows, sized by
   `paging::next_chunk`, each read's rows kept by the scope and then by the filter, the cursor by
   `paging::forward_cursor`. The Python reads the last chunk ahead beside a long judging; the Rust reads it after. The
   rows read and returned are the same.
8. **The derived walk is driven by the store** (`store::walk::Walk`, ADR 0238): the walk names the keys it reads next and
   takes their edges, and `Store::derived` reads them, so the handler's future is an ordinary `Send` one.

## How parity was shown

- **Vectors through the real Python app** (`scripts/_port_doors_vectors.py` → `rust/vectors/doors.json`, 121
  exchanges; the generator writes the same file on every run). The app is assembled as `lineage.main` assembles it, with
  authentication and authorization on; only its collaborators are stand-ins: a recorded HS256 key for the identity
  provider, a decider answering from recorded grants, the catalog's names from a recorded table, no outbox or bus, and a
  provenance store whose catalog does not answer. The corpus: each of the eight doors with no bearer, an unsigned one,
  another scheme, an empty bearer and an expired one (40); 22 queries to `/events` (every refusal pydantic gives, the
  declared order, the last value winning, `+`, underscores, fractions, 4,301 digits, a name with no value); the
  projection's, the derived door's and the dlq door's refusals and gates; 20 ingest bodies (missing, empty, three JSON
  errors, a list, `null`, a string, five content types, a byte-order mark, at and over the cap declared and not, with
  and without a bearer); six ingests the gates refuse or admit; 404, 405 and a trailing slash.
  `cargo test -p lineage --test doors_vectors` replays every exchange through the Rust router behind the shell and
  compares the status, the `content-type`, `cache-control`, `vary`, `allow` and `location` headers, whether an
  instance is named, and the body as text, so the members' order counts.
- **Side by side on real collaborators** (`scripts/port_doors_check.sh`, the Python half `scripts/port_doors_fixture.py`,
  the Rust half first the `doors_serve` example, then the binary, `lineage serve`, ADR 0238). It starts the real Python catalog, the pinned RustFS with its STS and the
  ServiceAccount issuer it trusts (`scripts/port_outbox_check.sh`), the pinned OpenFGA with the estate's model and a
  tuple set written on the catalog's own table uuids, and an identity provider the tokens are signed by; then the
  Python lineage app under uvicorn and the Rust doors on one environment, each logging in to the store as lineage's
  own ServiceAccount. It stages outbox events as the catalog's identity, parks deliveries in the refusals table, and
  puts 193 requests to each: GET `/events` 58, POST `/api/v1/lineage` 45, GET `/events/projection` 26, GET
  `/tables/{name}/derived` 16, GET `/admin/dlq` 14, POST `/admin/dlq/parked/{run_id}/replay` 11, POST
  `/admin/dlq/{run_id}/replay` 10, GET `/admin/dlq/parked` 9, and 4 to no door. Every door is asked as the admin, a
  reader with some grants, a subject with none and the feed's reader; 28 writes are put to both, each implementation
  going first in turn, so each reads what the other wrote; the feed is paged to its end older and forward on each
  side's own cursors; a replay that consumes its object is put to each with its own staged event or parked run and
  compared with the run id read as the same. Result on 2026-11-17: **0 differences**; 6 cases differ on purpose,
  under the first four entries below (a seventh, the trailing slash, ended when the Python stopped redirecting it).
- **Nine planted defects**, each restored after, each failed a test:

  | Defect | Vectors (121) | Side by side |
  | --- | --- | --- |
  | the projection's gate skipped | 2 exchanges | 2 cases |
  | the parked list asking the replay rung | 1 | 3 |
  | `oldest_seq` renamed | — (no page reaches a store) | 58 |
  | a page one row past its limit | — | 12 |
  | a hidden staged event answered 403 | — (no outbox) | 3 |
  | the derived gate's 403 answered 404 | 2 | 4 |
  | an ingest recorded before it is authorized | 4 | 0: both share one store, so the refused event lands for both and each answers the same 403 |
  | an answer marked cacheable | 2 | 138 |
  | no body cap (the shell's layer removed) | 3 | 1 |

## What differs from the Python, on purpose

- **A GET door answers HEAD**, as ADR 0219 keeps.
- **The `errors` list of an event the model refuses is the port's own**: one entry naming `body` with the model's
  message, where pydantic lists each field (ADR 0218 keeps the messages). The status, type and code are the same.
- **A body nested deeper than 512 levels is refused** with the framework's 400 ("There was an error parsing the
  body"), before any value is built (ADR 0223's reader bound). CPython reads far deeper.
- **A governed name the catalog refuses to describe is an outage** (503, ADR 0226). The Python answers the caller
  400 with code 13 and a detail naming the caller's own input: the catalog's refusal is a verdict on the name, which
  no retry cures. The Rust is to follow it; until it does, this case differs.
- **A record the store fails for any reason but another author's run is a 503**: `Recording::Failed` carries no class
  (ADR 0228). The Python answers the catalog's class. Outside the corpus.
- **An outbox object that is no UTF-8 is listed as poison.** The Python decodes it strictly and answers the whole
  listing 500: a defect, found by reading `lakehouse.outbox.list_events`, outside the corpus.
- **The body is read as UTF-8 only.** CPython's `json.loads` of bytes also reads UTF-16 and UTF-32. Outside the corpus.

## The four facts

- **GitOps:** nothing here is configured outside the state the service builds from its settings, which arrive from the
  chart; the check scripts start every collaborator from pinned binaries and the repository's own model.
- **Cloud native:** the doors are one router behind the shell's probes and limits, in the one lineage binary.
- **Lance-namespace/table-idiomatic:** every table is reached through the catalog's doors (`lakehouse-tables`); the
  crate links no Lance.
- **Zero trust:** every request is authenticated by the service from its bearer before its body is read, and every
  door asks its gate before it reads a row; no header but the bearer is believed; a refusal discloses nothing the
  caller could not list (the 404-versus-403 choices are the Python's, and planted defects of both directions fail); the
  body is bounded in bytes by the shell and in depth before a value is built.

## Not ported yet

- The service's settings and its `main` wiring: ported in ADR 0238, which serves the router from the binary.
- The outcome counter and ingest duration the doors count, and the audit trail's export: ported in ADR 0238.
- The read ahead of a governed page's last chunk, which changes when a read is made, not what is answered.
- The docs pages (`LINEAGE_DOCS`). The readiness dependencies are ported in ADR 0238.

## Consequences

- `lineage` serves 10 of its 10 operations (`docs/rust-port.json`) and stands at 3,595 of its 5,242 budgeted lines,
  827 of them this feature's (`doors/dlq.rs` 237, `doors/events.rs` 184, `doors/mod.rs` 183, `doors/ingest.rs` 98,
  `state.rs` 65, `doors/tables.rs` 30, `app.rs` 16, `lib.rs` 14, `authz/names.rs` 0: its `describe` is now crate-visible).
- A change to a door's order, validation or answer in the Python changes `rust/vectors/doors.json`, and `make
  rust-check` fails until the Rust agrees; the side-by-side check needs the fixtures and is not in `make rust-check`.
