# 0218. Lineage's decisions about an event are pure functions in Rust, checked against the Python by vectors (2026-11-17)

**Status:** accepted; the first feature of the port (ADR 0217). No door is served by Rust yet: this is what every
lineage door and delivery decides with, ahead of the doors themselves. Source: the owner, 2026-11-17 ("try to port the
code to rust following these principles, keep ADR of features").

## Context

Whatever reaches lineage, by HTTP or from the bus, is an OpenLineage event, and four things are decided from the event
alone: whether lineage takes it, the row the feed keeps for it, the table-level edges it adds, and the view a notifier
may target by. In Python these are spread over `lineage.models`, `lineage.services.provenance` and
`lineage.api.v1.endpoints.events`, and three of them read a clock or the process's settings on the way.

## Decision

1. **The decisions are four modules of `rust/crates/lineage`, and none reads a clock, a setting or a table.**

   | Rust | Decides | Python it answers for |
   | --- | --- | --- |
   | `model` | what an event must hold and may not exceed (64 inputs and outputs, 1 MiB as JSON, 1,000 names), who its author is, and the two reads of a raw body (`author_sub_from_payload`, `run_id_from_payload`, `without_signature_facet`) | `lineage.models` |
   | `feed` | the key an event is stored under, its feed row, its scope, and its edges with their keys | `feed_key`, `feed_row`, `scope_of`, `edges_of` in `lineage.services.provenance` |
   | `targeting` | the targeting view of a stored event, and the catalog datasets its column lineage reads | `targeting_view`, `_column_lineage_datasets` in the events endpoint |
   | `paging` | how much the next read of a filtered page takes, and where a follower continues | `_next_chunk`, `_forward_cursor` in the events endpoint |

   `rust/crates/lakehouse-core` holds what they share with the other services: `naming` (which datasets lie outside
   the catalog), `events` (the facet names, a dataset's uuids, a static event's id) and `pyjson`.
2. **The catalog's dataset namespace is an argument.** Python reads it from the process's settings inside
   `is_external_source`; the Rust functions take it, so a decision is a function of what it is given.
3. **The store stamps the time.** A feed row and an edge carry no `received_at` here; the store adds it when it
   writes them.
4. **An edge's key hashes the bytes Python hashes.** The key is the SHA-256 of a JSON array, and Python writes every
   character outside printable ASCII as an escape where `serde_json` writes UTF-8. `lakehouse_core::pyjson` writes
   Python's form, so a table named outside ASCII has one edge key in both implementations while both write the table.
5. **Parity is shown by vectors** (`scripts/gen_port_vectors.py` → `rust/vectors/`): 53 events, of which Python takes
   40, and 218 function cases. For every event the Rust answers are equal to Python's: whether it is taken, the stored
   JSON byte for byte, the feed row, every edge with its key, the targeting view and the column-lineage datasets. Two
   planted defects (a changed key suffix, a changed member of the edge key) failed 4 and 21 cases.

## What differs from the Python, on purpose

Each is outside the corpus, measured on both implementations 2026-11-17:

- **A member lineage reads that has the wrong JSON type is absent.** Python used it as it came: an author `name` of
  `5` became the author `"5"`, and a run `ref` of `7` the branch `"7"`. An `ownership` facet that is a list raised
  `AttributeError`, a 500. Rust reads a string where a string is meant and otherwise finds nothing.
- **A body that spells one member both ways is refused** (`runId` and `run_id`, `eventType` and `event_type`).
  Python took the first and stored the second beside it, so one stored event named two run ids.
- **A dataset version in text is ASCII digits.** Python's `int()` also took `"1_000"` as 1000.
- **A whole number beyond 64 bits is stored as a float.** Python stored its digits. Every other number is stored as
  Python stores it.
- **Refusal messages are the port's own.** Which events are refused is the same.

## Consequences

- The lineage crate is 618 of its 2,621 budgeted lines with its decisions in place, and `lakehouse-core` 108.
- The doors, the store (the catalog's `query` and `merge_insert` doors), authentication, the OpenFGA gates and the
  bus are not ported: `docs/rust-port.json` shows 0 of lineage's 10 operations served.
- A change to one of the Python functions above changes a vector, and `make rust-check` fails until the Rust agrees.
