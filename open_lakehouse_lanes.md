# Index builds move onto the maintenance work lane

Carried out of the cloud-native cutover plan when it closed (`docs/DECISIONS.md`), because it is the one row that never
belonged to the zero-trust/decoupling chain the rest of it was about. It is unstarted.

## The defect

Two operations still move BYTES THROUGH A POD that has no business carrying them:

- ~~**`rename_table`** — copies the dataset root in-process.~~ **CLOSED 2026-09-04.** A rename is a
  `__manifest` pointer move: register the destination id at the source's existing location, deregister
  the source. O(1) in the dataset, no byte read or written, and the byte-copy's three failure modes go
  with it. The answer is lance-ns's own V2 naming rule, measured on the `dir`
  backend the chart runs — see `docs/DECISIONS.md`, "A rename moves a POINTER, not bytes".
- **Index builds** run wherever they are invoked rather than on the work lane, so a large index is
  another unbounded in-handler cost.

## The shape the answer takes, and why it is already known

Compaction's split by credential is the template: pylance ships
`create_index_uncommitted` / `commit_existing_index_segments` as the same
plan-elsewhere / commit-in-the-catalog pair that `Compaction.plan` / `.execute` / `.commit` gave
compaction. So an index build becomes a `DatasetWorkItem` like any other unit, executed under a
credential vended for that table.

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

- any index-build unit type, or a decision about whether index work shares `DatasetPlan` or needs its
  own payload;
- a measured cost for the index build, which is the first thing to get.
