# 0096. A write event names the commit the write made: version, ref and branch incarnation (LH-214, 2026-10-05)

Every catalog write door states the version its own commit made, and `emit_measured_write` takes `pin_version` and
`branch` with no default, so a door that states neither no longer falls back to a reopen. The version comes from the
write itself: a handle's `version` after the commit (insert, update, delete, the column ops, schema metadata on the
dataplane path, the in-pod compact and reindex, `/commit`), a response's `version` (merge_insert), or, for a response
carrying only a `transaction_id` (restore, schema metadata on the native path, the index doors), the spec's
`DescribeTransaction`, whose `properties.version` is that transaction's commit (`dataplane.committed_version`). Lakekeeper
builds its commit event the same way, from its own transaction's context. Measured on pylance 12.0.0: the `dir` backend's
`insert_into_table` answers `{}`, so the main insert now goes through the handle path a branch insert already used (one
path for every ref; `LanceDataset.insert` advances its handle to its own commit after any rebase), and its
`num_inserted_rows` is the payload's row count rather than a count diff a concurrent append would inflate. The `dir`
`describe_transaction` reads main's history only (a branch restore's id answers `TransactionNotFound`), so a branch's
transaction is found by walking that branch's handle down from its head, bounded at 64 versions. A commit that cannot be
identified emits versionless and schemaless rather than the latest snapshot's number.

Every branch-targeted write emits its ref (update, delete, schema metadata, the in-pod compact and reindex, and the queued
index worker in `services/maintenance` did not), and a request naming main by name emits no ref, as Lance records main.
A recreated branch restarts its numbering, so a branch write also carries `lance.branchIdentifier`: the last uuid of
pylance's `branch_identifier` chain in `branches.list()`, minted at the branch's create and different across a
delete-and-recreate within one second. It also rides `list_branches`' per-branch `metadata` (key `branch_identifier`,
written over a user key of that name; `BranchContents` has no field for it), the branch created/deleted control events, and
the tag control events, which now name the ref a tag pins (`branch`, plus its identifier). The identifier is read after
the write, so a delete-and-recreate landing between a branch write and its readback would stamp the new incarnation.

Accepted with it: the lineage service stores `lance.ref` on the WROTE edge but not the identifier, so the graph itself
still cannot tell two incarnations' `(ref, N)` apart; the identifier lives on the event. The two compaction lanes'
evidence gate refuses every branch on pylance 12.0.0 (a branch's data files resolve through main's root, flag 16), so
their branch emit is wired and unexercised.
