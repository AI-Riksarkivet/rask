# 0046. A rename moves a POINTER, not bytes (2026-09-04)

`rename_table` copied the dataset root in-process and deleted the source. It answered 200, which is
why nothing flagged it, but the cost of a rename was the DATASET's size, paid inside a request
handler — unbounded work no pod sizing fixes, the same class as the `maintenance/compact` door before
it became a 202.

**lance-ns already answers this.** `RenameTableRequest` carries `id`, `new_namespace_id` and
`new_table_name` — identifiers, no location — so the operation is defined as a remap and nothing in
the request could describe a relocation. What makes that servable here is the V2 storage layout: a
table's directory is `<hash>_<object_id>` while the authoritative name→location mapping lives in the
`__manifest` table, and the spec says the `object_id` suffix "ensures uniqueness and aids debugging"
(`lance_docs/namespace.md` § *Manifest Table Directory*). It is a label, not a resolution path. The
hash prefix exists for object-store throughput and for create/delete/recreate conflict prevention,
not for addressing.

So the rename is: register the destination id at the source's existing location, deregister the
source. **O(1) in the dataset.**

MEASURED on the `dir` backend the chart runs (`LANCE_REST_IMPL=dir`, pylance 10.0.0):

| | before | after |
| --- | --- | --- |
| destination resolves to | — | the SOURCE's own location, unchanged |
| rows / versions | 4 / 2 | 4 / 2 |
| `list_tables(ns1)` | `['old']` | `['new']` |
| bytes moved | the whole dataset | none |

Three failure modes go with the copy: the half-copy on a failed relocation, the non-atomic source
delete that could strand a partially-deleted source, and the read-rewrite that would have collapsed
version history (which the copy existed to avoid). What replaces them is one window: a failure
between the two calls leaves BOTH ids resolving to ONE dataset — no data duplicated, no data at risk,
recoverable by deregistering either id. `_copy_dataset` / `_delete_dataset` are deleted.

**Two consequences worth stating, because both look like regressions and are not.**

The source's terminal lineage marker is now `DEREGISTER_TABLE`, not `DROP_TABLE`. A DROP says the data
is gone — `dropped_at()` fires and the reconciler stops expecting the location — which would be a lie
about a dataset that is still there, still governed, and still the destination's own history.

Protection still gates a rename exactly like a drop, even though no byte moves. The bytes surviving is
not the point: the protection record, every policy and every grant are keyed on the ID, and a rename
takes the table out from under all three.

**One shape is REFUSED rather than served.** A V1 root-namespace table is stored under compatibility
naming (`<name>.lance`), where the location IS the name, and the spec's rule is that renaming one
"transitions to the V2 hash-based path naming" — a relocation. Serving that with a byte copy would put
the unbounded work straight back, so it is an `InvalidInputError` naming the reason. rask's own
`require_parent` guard refuses root tables, so no table reachable through these doors has that shape.

The INDEX-BUILD half of docs/adr/0046-a-rename-moves-a-pointer-not-bytes-2026-09-04.md "A rename moves a POINTER, not bytes" is untouched by this and still stands: index builds
run wherever they are invoked, and `create_index_uncommitted` / `commit_existing_index_segments` give
them the same plan-elsewhere / commit-here split compaction now uses.
