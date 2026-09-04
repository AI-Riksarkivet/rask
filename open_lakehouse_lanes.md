# Index builds and rename leave the request handler — BOTH CLOSED 2026-09-04

Carried out of the cloud-native cutover plan when it closed (`docs/DECISIONS.md`), because it is the one row that never
belonged to the zero-trust/decoupling chain the rest of it was about. Both rows are closed; what remains is measurement and in-cluster verification, listed at the foot.

## The defect

Two operations still move BYTES THROUGH A POD that has no business carrying them:

- ~~**`rename_table`** — copies the dataset root in-process.~~ **CLOSED 2026-09-04.** A rename is a
  `__manifest` pointer move: register the destination id at the source's existing location, deregister
  the source. O(1) in the dataset, no byte read or written, and the byte-copy's three failure modes go
  with it. The answer is lance-ns's own V2 naming rule, measured on the `dir`
  backend the chart runs — see `docs/DECISIONS.md`, "A rename moves a POINTER, not bytes".
- ~~**Index builds** run wherever they are invoked.~~ **CLOSED 2026-09-04.** `create_index` /
  `create_scalar_index` publish one `IndexWorkItem` and answer with its id; a maintenance worker
  builds it under the table-scoped credential. That is the spec's OWN model — `CreateTableIndex` says
  "index creation is handled asynchronously" and points at `ListTableIndices` /
  `DescribeTableIndexStats` for progress — so the synchronous build was the divergence, and no 202 or
  new response shape was needed.

## The shape the answer takes, and why it is already known

This file expected compaction's split to transfer: pylance ships `create_index_uncommitted` /
`merge_existing_index_segments` / `commit_existing_index_segments`, which is the same three-phase
shape. **It does not transfer, and the reason is measured** (pylance 10.0.0, 2026-09-04): an index
segment carries no `json`, `to_json` or `serialize`, so unlike `CompactionTask` / `RewriteResult` it
cannot cross a process boundary. The WHOLE build therefore moves to the worker rather than a plan —
which still takes it off the request path, and the finer split becomes available the day those
segments serialize.

The unit is its own type (`IndexWorkItem`, not `DatasetWorkItem`) on its own pubsub COMPONENT, which
answers this file's second open question. Component, not merely topic: `ackWait`, `durableName` and
`queueGroupName` are per-component in Dapr's JetStream pubsub, so a second topic on the work
component would inherit the work queue's 720s window — sized for a compaction, not for a vector
index over a large table.

The rename decision is MADE, and it turned out to be neither of the two options this file listed. It is
not a server-side copy and not a base-path rewrite: lance-ns V2 keeps the authoritative name→location
mapping in `__manifest` and treats the `<hash>_<object_id>` directory name as a debugging label, so a
rename is one manifest-row move and the dataset's path simply stops matching its name — which is the
correct design rather than a tradeoff. Measured and closed; see `docs/DECISIONS.md`.

## What is already in place

- the work lane itself (`maintenance/services/work_queue.py`, `api/work.py`), proven in-cluster: a
  whole-estate tick planned and published 20 units in 1.76s and the executor consumed them;
- `DatasetWorkItem` in `service_kit.lakehouse.work_items`, with two producers already (the sweep and
  the catalog's on-demand compact door), so a third needs no new contract;
- per-table vended write credentials, so a unit that rewrites bytes is not signed by a root key.

## What is NOT in place

- a measured cost for an index build on a real dataset, and a value for `maintenance.indexAckWait`
  chosen from it rather than from the 3600s placeholder;
- in-cluster verification of the lane (the unit tests drive it end to end, but no build has crossed a
  real broker).
