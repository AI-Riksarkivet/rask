# 0217. Four behaviours stand as the spec, the format and the measured load leave them (2026-11-17)

**Status:** accepted. Source: XC-217 (#840), the backlog audit's leftovers, which found four behaviours with no record:
LH-035 (#172), LH-041 (#173), LH-050 (#178) and LOW-029 (#187). Decision 4 was put to the owner with its two
alternatives and chosen on 2026-11-17 ("Keep planning again").

## Context

Each of the four is how the catalog already behaves. Three follow from a limit: the spec has no such field, the format
offers no such write, the load does not ask for it. The fourth, what a merge does when another writer took its version,
has a real alternative and changes what a caller of the merge door is answered, so it was the owner's to decide. The evidence was re-measured on 2026-11-17 on pylance 13.0.0, lance-namespace 0.11.1 and the vendored
`lance_docs/ns_catalog/spec.yaml`.

## Decision

1. **The spec's query door takes no inline-bytes option** (LH-035). `QueryTable` answers a blob column as Lance
   answers it, a descriptor, and a caller reads a blob's bytes through `GET /management/v1/table/{id}/blobs` or with
   the credential the catalog vends. A `blob_handling` parameter on `/v1/table/{id}/query` would be a rask-only
   parameter on a spec route: `spec.yaml` has no field naming a blob (0 matches) and `QueryTableRequest` has 22 fields,
   none of them one, so a stock client could not send it, and `/v1` holds only the spec's operations (ADR 0156, held by
   `tests/integration/test_spec_conformance.py`). `scanner(blob_handling=...)` is an SDK scan mode, not a namespace
   operation.

2. **A tag move is last-writer-wins** (LH-041). `tags/update` calls `LanceDataset.tags.update(tag, reference)`, whose
   signature takes no expected prior reference (`Tags.update(self, tag, reference=None)`), and the spec's
   `UpdateTableTag` lists no 409 (`spec.yaml`: 200, 400, 401, 403, 404, 503, 5XX). Two moves of one tag therefore both
   answer 200 and the tag names whichever landed second. Creation is arbitrated: `tags.create` refuses a name that
   exists. Arbitrating a move would mean the catalog writing `_refs/tags/<tag>.json` itself under a conditional put,
   which makes it a second writer of a file Lance owns; it never hand-writes a ref. Every ref write holds the table's
   history fence (ADR 0176), which two ref writes share: it keeps a move apart from a reclaim, not from another move.
   Who may move a tag is the model's `can_update_tag`, which a `tagger` and the table's owner hold; a pipeline that
   needs one mover grants `tagger` to one identity and keeps the owner's hands off the tag.

3. **The catalog keeps no query store for its listings until listing load is interactive** (LH-050). A namespace or
   table listing is read from its root's `__manifest`, and the warehouse listing from the warehouse registry's records,
   each judged per item on every request; there is no cache or index of names beside them. A store of names is a second
   record that can disagree with the catalog's own, and nothing measured asks for one: on 2026-09-24 the namespace and
   table listing routes were not among the catalog's eight busiest, and `/management/v1/warehouses` read 0.000627
   requests a second on 2026-09-20. The decision re-reads itself: the alert
   `CatalogListingLoadReachedInteractiveFrequency` (`deploy/alerting/rules.yml`) fires when those listings are served
   above 1 request a second for 30 minutes, which is when this decision is to be reopened.

4. **A merge that lost its version is planned again on the rows that won, and commits** (LOW-029). The merge door
   plans `MergeInsertBuilder.execute_uncommitted` on the ref's latest version and
   finalizes it as exactly the next version (`dataplane.commit_stamped`). A merge whose version another writer took is
   planned again on the new latest, so it reads the winner's rows before it writes; it takes the ref's commit turn for
   its remaining attempts where the turn is free (ADR 0186), and after ten lost attempts it answers 409 code 14. A
   matched row is then replaced whole by the merge's source row, as `when_matched_update_all` says, including a field
   the other writer had just changed: the later merge wins the row, and inside those ten attempts neither caller is
   told. What the door never does is commit a merge planned on rows it did not read: the detached finalize refuses a
   version that is taken.

   The alternatives the owner turned down: answering every merge that lost its version 409 code 14, which refuses a
   merge that raced a write to other rows; and answering 409 only when the rows it matched changed under it. Lance detects a conflict for `merge_insert` per fragment and retries
   (`lance_docs/lance/guide/performance.md` § Fragment Sizing and § Conflict Handling), so a door built that way could
   refuse only a merge whose fragments another writer rewrote, not every merge that raced a write. A caller can
   already state its own precondition: `when_matched_update_all_filt` (`spec.yaml`) is a condition on the matched row,
   and a merge that must not overwrite a newer row can carry one over a version or timestamp column of its own; the
   spec does not present the filter as a concurrency control, and that reading is this entry's.

## Consequences

- The alert's text and its promtool expectations name this entry (decision 3) where they named the backlog row.
- Under decision 4, a caller that wants optimistic concurrency on a row supplies the condition; the catalog
  adds no version column, since the platform names no column (ADR 0212).
