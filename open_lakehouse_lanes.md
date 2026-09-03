# Index builds and rename move onto the maintenance work lane

Carried out of the cloud-native cutover plan when it closed (`docs/DECISIONS.md`), because it is the one row that never
belonged to the zero-trust/decoupling chain the rest of it was about. It is unstarted.

## The defect

Two operations still move BYTES THROUGH A POD that has no business carrying them:

- **`rename_table`** — `catalog/api/v1/endpoints/tables.py::rename_table` copies the dataset root
  in-process, repoints the namespace and deregisters the source. It answers 200, which is why nothing
  flags it, but a rename's cost is the DATASET's size and it is paid inside a request handler. That is
  the same class as the `maintenance/compact` door before it became a 202 — see `docs/DECISIONS.md`,
  "The lakehouse cloud-native cutover".
- **Index builds** run wherever they are invoked rather than on the work lane, so a large index is
  another unbounded in-handler cost.

## The shape the answer takes, and why it is already known

Compaction's split by credential is the template: pylance ships
`create_index_uncommitted` / `commit_existing_index_segments` as the same
plan-elsewhere / commit-in-the-catalog pair that `Compaction.plan` / `.execute` / `.commit` gave
compaction. So an index build becomes a `DatasetWorkItem` like any other unit, executed under a
credential vended for that table.

A rename is different in kind and must NOT become a byte copy on the work lane. The right answer is a
server-side copy (the object store's own, no bytes through any pod) or a base-path rewrite that moves
no data at all. Which of the two depends on whether the estate is willing to leave a dataset's bytes at
a path that no longer matches its name — that is the decision this work has to make first, and it is
not made.

## What is already in place

- the work lane itself (`maintenance/services/work_queue.py`, `api/work.py`), proven in-cluster: a
  whole-estate tick planned and published 20 units in 1.76s and the executor consumed them;
- `DatasetWorkItem` in `service_kit.lakehouse.work_items`, with two producers already (the sweep and
  the catalog's on-demand compact door), so a third needs no new contract;
- per-table vended write credentials, so a unit that rewrites bytes is not signed by a root key.

## What is NOT in place

- any index-build unit type, or a decision about whether index work shares `DatasetPlan` or needs its
  own payload;
- the rename decision above;
- a measured cost for either, which is the first thing to get: `rename_table` is a 200 today and
  nobody has recorded how long it takes on a real dataset.
