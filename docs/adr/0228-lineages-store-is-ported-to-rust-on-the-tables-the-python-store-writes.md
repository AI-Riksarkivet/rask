# 0228. Lineage's store is ported to Rust, writing the same tables the Python store writes (2026-11-17)

**Status:** accepted; a feature of the port (ADR 0217), written to the event path's model (ADR 0221) on the table
client of ADR 0222. No door is served through it yet: the bus intake, the relay and the HTTP doors that call it are the
next slices. Source: the owner, 2026-11-17 ("try to port the code to rust following these principles, keep ADR of
features").

## Context

`lineage.services.provenance.ProvenanceStore` keeps lineage's five tables in the catalog's provenance namespace: the
commit feed (`events`), the edge list (`edges`), the run bindings (`runs`), the outbox refusals and parked deliveries
(`refusals`) and the forwarder's cursors (`cursors`). It writes every row by an insert-only merge on the table's
unenforced primary key, through the catalog's doors as lineage's own principal (ADR 0158). Its module docstring states
the measured facts the design rests on: the feed's sequence is Lance's stable row id (`_rowid + 1`), assigned at commit
(`lance_docs/lance/format/table/row_id_lineage.md`, "Row ID Assignment"); a row is written once; a burst is one commit
per table (LH-533: 200 concurrent deliveries took 2 commits per table instead of one each); a page costs the rows it
returns, read by row-id range at one pinned version (LH-590). During the mixed period both implementations write the
same tables, so the Rust store must create them with the same schemas and write the same rows.

## Decision

1. **`rust/crates/lineage/src/store/`** holds the store. The decisions are functions of values; `Store` is the shell.

   | Rust | Decides or does | Python it answers for |
   | --- | --- | --- |
   | `tables` | the five tables and their Arrow schemas, each key field marked `lance-schema:unenforced-primary-key` (`lance_docs/lance/format/table/index.md`) | `FEED_SCHEMA` … `RUN_SCHEMA`, `_TABLES` |
   | `rows` | the batches a write sends; the columns, row ids and `EventRecord`s a read answers | `pa.Table.from_pylist`, `_record_of`, `_row_ids` |
   | `commit::admitted` | one event's rows, its run and the subject it is recorded for | `_recording_of` |
   | `commit::plan` | one group commit's writes in order (bindings, edges, feed), keys sent once, and which events are another author's run | `ProvenanceStore._commit`, `_first_of_each`, `_extends_another_authors` |
   | `group` | the group commit: one writer per process, a batch bounded by bytes, a refused batch split in halves, each caller answered | `record`, `_land_pending`, `_next_batch`, `_land`, `_settle` |
   | `window` | `Cursor` (where a read starts and its next row-id range), `FeedWindow` (bounds named, proven, by offset, read whole) over a `FeedReader` | `FeedWindow` |
   | `walk` | the derivation walk, `is_table_uuid`, the answer's order | `lakehouse.derived_tables.walk`, `is_table_uuid`, `ProvenanceStore._derived` |
   | `prune_predicate`, `parked_filter`, `delivery_feed_key` | the prune's delete, the parked read, a parked delivery's feed key | `prune_events`, `parked`, `delivery_feed_key` |
   | `Store` | `ensure`, `ready`, `record`, `record_refusal`, `parked`, `events_recorded`, `forget_parked`, `retire_recovered_parks`, `prune_events`, `window`, `list_events`, `feed_bounds`, `recorded_event`, `derived`, `cursor`, `advance_cursor`, `reset_cursor`, `feed_incarnation`, `run_author` | the same methods |

2. **The group commit runs on tokio.** `record` queues the event and spawns the burst's writer when none runs; the
   writer takes what waits, oldest first, up to 16 MiB of rows and always one event, commits it, and repeats until
   nothing waits, then ends. Each caller waits on a oneshot answer: `Recording::Landed`, `Recording::ForeignRun`, or
   `Recording::Failed`, which is what `intake::after_recording` takes. A batch the catalog refuses for its rows (400,
   413, 422) is split in halves until only the refused event fails; any other failure fails the batch whole. A writer
   that ends unwinding clears the burst's flag, so the next record starts another.
3. **The three writes follow the model.** One commit writes the run bindings, then the edges, then the feed rows, each
   an insert-only merge on the table's key, the order of `specs/lineage/EventPath.tla` (`Order == <<"runs", "edges",
   "feed">>`). The implementation needed no change to the model.
4. **The store stamps `received_at`** from its own clock when `record` is called, and `refused_at`/`updated_at` when it
   writes those rows, as the Python store does.
5. **Only the catalog's doors, as lineage's own identity:** every call goes through `lakehouse-tables` with the bearer
   asked per request (ADR 0222); the store holds no storage credential.

## How parity was shown

- **Vectors through the real Python** (`scripts/_port_store_vectors.py` → `rust/vectors/store.json`, 200 cases, the same
  bytes on two runs): the 5 schemas as the IPC stream `ensure` sends, decoded by Rust and compared field by field with
  their metadata; 15 batches put through the real `_recording_of` and `ProvenanceStore._commit` with the writer captured,
  the Rust plan sending equal `RecordBatch`es to each table (or none where Python sends none) and refusing the same
  events; 6 key lists through `_first_of_each`; 140 windows of the real `FeedWindow` over a recording reader (dense,
  holes, a pruned prefix, one row, empty; answered ascending and descending; every cursor; small takes and one large),
  the Rust window answering the same bounds, the same records and making the same reads; 11 walks through
  `ProvenanceStore._derived` and 8 keys through `is_table_uuid`; 7 parked deliveries' feed keys; 5 prune predicates at a
  fixed clock and 3 parked filters. `cargo test -p lineage --test store_vectors` (7 tests).
- **The group commit offline** (`tests/store_group.rs`, 6 tests): a committer that holds its first commit decides what
  is in flight, so the batches are exact: what waits lands in one commit, each caller gets its own outcome, a refused
  batch lands in halves (`[1..5]`, `[1,2]`, `[3,4,5]`, `[3]`, `[4,5]`), another failure fails the batch whole, a batch
  takes what its bytes allow, and a burst's writer ends and the next record starts another.
- **Against the real catalog** (`tests/store_catalog.rs`, 13 tests, skipped with a message without
  `RASK_PORT_CATALOG_URL`; each test in a namespace of its own): `ensure` twice moves no table's version; one event
  recorded four times (twice in one batch) is one feed row, one binding and its edges, and a terminal state re-sent with a
  later time keeps its first row; 30 concurrent events land in 1 feed commit (measured 2026-11-17), every sixth refused as
  another author's run and the rest landed; a run another author extends is refused only when refusal is on; a window
  opened before a commit and a prune reads the version it opened on; `since` and `after` page in feed order; a cursor
  only moves forward unless reset; a parked delivery is retired once its event is recorded, an outbox refusal kept; a
  prune removes only rows both old enough and forwarded, and the bounds move with it; a retry after a given-up attempt
  rewrites nothing that attempt landed, and that attempt's feed write landing late adds nothing; a store whose `edges`
  or `runs` table refuses its rows writes no feed row; a batch whose edges are refused lands the other events and fails
  only that one; the walk follows two hops and says when it was cut short.
- **Across implementations** (`scripts/port_store_cross.py`, with `rust/crates/lineage/examples/store_cross.rs` as the
  Rust half): in three namespaces the Python store records a corpus of ten events and the Rust store records it again,
  the reverse, and a half-and-half mix; the feed, edge and binding tables of the three are equal row for row in row-id
  order, every column but the receipt stamp, with equal schemas; both stores answer the same outcomes; and over the mixed
  tables both answer the same feed (with and without payload), every page forward and back, bounds, walk, run authors,
  recorded keys, parked deliveries, cursor and feed uuid. Result on 2026-11-17: identical.
  Run all of it with `bash scripts/port_store_check.sh`.

**Planted defects**, each restored after: the feed row written first (caught by
`no_feed_row_is_written_when_an_earlier_write_fails` and `a_refused_batch_lands_in_parts…`); the insert-only merge
made an upsert (`an_event_recorded_twice…`, `a_write_that_lands_late…`); the foreign-run check skipped
(`a_run_extended_by_another_author…`, `thirty_concurrent_events…`, the commit vectors); a page read at the current
version (`a_page_is_read_at_the_version…`); no group commit (`thirty_concurrent_events…` and four group tests); no split
on a refused batch (`a_refused_batch_lands_in_parts…` and the split group test); a bounds proof ignored (the window
vectors); a seq off by one (three real-catalog tests and the cross-implementation check).

## The model's steps and the tests that cover them

| `EventPath.tla` | Test |
| --- | --- |
| `Write`, in `Order` (runs, edges, feed); `AFeedRowIsWhole` | `no_feed_row_is_written_when_an_earlier_write_fails`; the commit vectors (which tables a batch writes) |
| A1, a write is insert-if-absent: a second record of one event, two in one batch | `an_event_recorded_twice_is_one_feed_row_and_one_set_of_edges` |
| `WriteFails` then `LateLands`: a given-up write landing after the retry | `a_write_that_lands_late_lands_nothing_twice` |
| `RetirePark` | `a_parked_delivery_is_retired_once_its_event_is_recorded` |
| `OnlyAllowedEventsAreRecorded`, for the run's author, which the model leaves out | `a_run_extended_by_another_author_is_refused_only_when_refusal_is_on` |

## What differs from the Python, on purpose

- **A batch's bytes** are each event's feed row and edges as JSON, where Python sums pyarrow's `nbytes`; batches may
  break at another event near 16 MiB. What lands is the same.
- **The bounds proof starts with the first read** (`take` or `bounds`), beside it, where Python starts it as a task at
  `open`; the reads made are the same (the window vectors compare them).
- **`recorded_event` answers `None` for a stored payload that is no JSON**, where Python's `json.loads` raised.
- **`prune_events` takes whole days as `u32`**; Python took any integer.
- **`delivery_feed_key`** reads a missing `data` as JSON null, which no event parses from, as Python's `None` did.

## Not ported yet

The intake, relay and HTTP doors that call the store; the store's traces (`lineage.record` span links) and metrics.

## Consequences

- The lineage crate gains `lakehouse-tables`, `arrow-array`, `arrow-schema` and `tokio`; `arrow-select` is a test
  dependency. The store is 1,146 lines of production Rust; the crate stands at 1,884 of its 2,621.
- A change to the Python store's schemas, rows, window or walk changes `rust/vectors/store.json`, and the vector tests
  fail until the Rust agrees. The real-catalog and cross-implementation checks need the fixture and are not in
  `make rust-check`.
