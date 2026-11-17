# 0222. A table the catalog holds is reached from Rust only through its doors, as the caller's own identity (2026-11-17)

**Status:** accepted; the fourth feature of the port (ADR 0217). It is the client lineage's feed and edge list will be
read and written with; the store that uses it (`ProvenanceStore`, its group commit and `FeedWindow`) is the next slice,
so no door is served through it yet. Source: the owner, 2026-11-17 ("try to port the code to rust following these
principles, keep ADR of features").

## Context

Lineage keeps no database and holds no storage credential (ADR 0158): every row it keeps is written through the
catalog's `merge_insert` door and read back through its `query` door, as lineage's own principal, so the catalog
authorizes, judges and audits each call as any other writer's. In Python that client is `lakehouse.lancekit`
(`catalog_client`, `reader`, `writer`, `filters`) over the generated `lance_namespace_urllib3_client`, with the Arrow
encoding in `lakehouse.arrow_ipc`. `ProvenanceStore` makes eight kinds of call on it: create a table if absent, read
rows (columns, a filter, a limit and offset, row ids, at a version), count rows, the current version, an insert-only
merge, an upsert merge with an optional matched-when condition, a delete, and a describe for the table's uuid.

## Decision

1. **`rust/crates/lakehouse-tables` is the crate that reads and writes a table**, and the one that depends on Arrow
   (`arrow-array`, `arrow-ipc`, `arrow-schema` 58, the major `lance` 13 builds on), so `lakehouse-core` stays light. It
   reaches the catalog over `reqwest` (rustls) and depends on no Lance crate: it speaks only the spec's doors.

   | Rust | Decides or does | Python it answers for |
   | --- | --- | --- |
   | `requests` | each door's request as a value: `POST /v1/table/{id}/<action>`, the id joined by `$` and escaped, its query and body, and whether it may be sent again | the generated client's serialization, called from `reader`, `writer` and `ProvenanceStore._ensure` / `_feed_incarnation` |
   | `errors` | what a catalog's answer is refused as: a spec code, the message, and the HTTP status kept beside it | `reader.translate_catalog_errors` |
   | `filters` | `sql_literal` and `in_lists` | `lancekit.filters` |
   | `arrow` | the IPC stream a write sends, and the IPC answer of `/query` (file or stream) validated array by array | `arrow_ipc.encode_arrow_stream`, `decode_arrow_response` |
   | `bearer` | the bearer a request carries, asked of a closure on every request; `token_file` reads a projected token there | `catalog_client.request_headers`, `machine_identity.read_identity_token` |
   | `client` | the shell: one connection pool per catalog, `Table::{create, describe, version, count_rows, read, merge_insert_only, merge_upsert, delete}`, and how a read is paged | `CatalogTableReader`, `RestCatalogTransport`, `RestCatalogWriteTransport` |

2. **The doors are the spec's and nothing else** (`lance_docs/ns_catalog/spec.yaml`): `CreateTable` with
   `mode=exist_ok`, `DescribeTable` (with `load_detailed_metadata=true` for the version, `vend_credentials: false`
   always), `CountTableRows`, `QueryTable` (an empty vector and a bounded `k`, since the door has no plain-scan verb),
   `MergeInsertIntoTable` (an Arrow IPC stream body, its flags in the query) and `DeleteFromTable`.
3. **A read is paged as Python pages it:** pinned to the version current when it starts, 64 rows first, then 4 MiB of
   rows at the width the last page of the same projection measured, and a page shorter than its `k` is the last.
4. **Who a request is sent as is decided per request.** The pool carries no identity; the bearer closure is asked
   before each call, so a rotated projected token is read on the next one, and a token file that cannot be read or is
   empty stops the call unsent (`ServiceUnavailable`). The header is marked sensitive, so no debug rendering shows it.
5. **A read is sent again until answered, a write only when it never left** (three times at most), as the Python read
   and write clients do: a read meeting a connection the catalog closed for idleness does not fail, and a write the
   catalog may have applied is never sent twice.
6. **Parity is shown two ways.**
   - *Against the real catalog.* `scripts/port_catalog_fixture.py` serves `catalog.main:app` under uvicorn on real
     pylance over a `dir` namespace, with authentication off, as the Python integration tests do. The ten tests of
     `rust/crates/lakehouse-tables/tests/catalog.rs` drive every call against it and assert what is read back: a create
     of a table that exists keeps its rows and its uuid; an insert-only merge lands a key once; an upsert updates only
     where its condition holds; a delete removes what an `in_lists` predicate names, a quote included; a read pinned to
     an older version reads that version; row ids are stable and filter a read and a count; a read of 300 rows pages;
     the two filters `in_lists` makes of 551 values read exactly those keys; a missing table, version, malformed
     filter and missing column keep their meaning; a catalog that does not answer is unavailable. They are skipped
     with a message when `RASK_PORT_CATALOG_URL` names no catalog. Run:
     `uv run scripts/port_catalog_fixture.py --port 8765`, then
     `cd rust && RASK_PORT_CATALOG_URL=http://127.0.0.1:8765 cargo test -p lakehouse-tables --test catalog`.
   - *Against the real Python client* (`scripts/_port_tables_vectors.py` → `rust/vectors/tables.json`, 55 cases):
     9 literals and 8 sets of values through `filters`, 16 catalog answers through `translate_catalog_errors`, and the
     eleven calls `ProvenanceStore` makes, each with a bearer and without, sent through the real generated client with
     only its socket replaced by a recorder. The Rust client makes each call over HTTP against a local server that
     replays the recorded answers, and sends the same method, the same path and query byte for byte, the same
     `accept`, `content-type` and `authorization`, an equal JSON body or the same rows, and reads each answer to the
     result Python read.

   Eight planted defects (a describe body missing `check_declared`, unsorted `IN` lists, a 422 not taken as a refusal,
   an insert-only merge that updates, every page read at the first offset, an untrimmed token, a pinned version off by
   one, row ids asked for inverted) each failed at least one of the two suites.

## What differs from the Python, on purpose

Measured on both implementations 2026-11-17:

- **The uuid describe names its table in the body**, as the version describe does. Python's sends no `id` there; the
  path names the table either way.
- **A refusal keeps the catalog's own code** when its problem body names one whose status is the status answered: a
  404 naming `TableVersionNotFound` or `TableColumnNotFound` stays that, and a 409 naming `TableAlreadyExists` stays
  that. Python kept only the class (not found, conflict). The status and the message are Python's.
- **A catalog that does not answer is `ServiceUnavailable`.** Python let urllib3's connection error escape
  `translate_catalog_errors`, a 500 at the door.
- **Redirects are not followed**, so a request and its bearer go only where they were addressed; a redirect is an
  answer outside the spec's classes, so the catalog is unavailable. urllib3 followed them.
- **A compressed IPC answer is refused**: `arrow-ipc` is built without LZ4 and zstd. The catalog's `/query` answers
  uncompressed, which every read against it above decoded.
- **A page's width is measured by Arrow's own buffer sizes**, not pyarrow's `nbytes`, so how many rows a later page
  asks for may differ by a little; the rows read are the same.

## Not ported yet

- `CatalogTableReader.versions` (`/version/list`): `ProvenanceStore` does not call it.
- `arrow_ipc.decode_arrow_body`, the catalog's decoder of a caller's body, held to the door's body cap: it belongs to
  the catalog's doors.
- `ProvenanceStore` itself: the feed, the edge list, the run bindings and refusals, the group commit and `FeedWindow`.

## Consequences

- `lakehouse-tables` is 471 lines of production Rust against a budget of 480: the executable lines of
  `catalog_client.py`, `reader.py`, `writer.py`, `filters.py` and `arrow_ipc.py` (467), with `uuid_of` (4) and
  `read_identity_token` (9). Of `arrow_ipc.py` only 63 lines are ported, so the Python it replaces line for line is 346;
  the rest of what it replaces is the generated client's serialization, which is not counted.
- A change to what the Python client sends or reads changes `rust/vectors/tables.json`, and `make rust-check` fails
  until the Rust agrees. The black-box suite needs the fixture running and is not in `make rust-check`.
