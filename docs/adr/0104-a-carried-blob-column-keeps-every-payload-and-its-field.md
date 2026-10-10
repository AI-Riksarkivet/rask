# 0104. A carried blob column keeps every payload and its field metadata (LH-217, 2026-10-05)

An external base does not make every row of a blob column external. Blob V2 places each value by itself: a
`Blob(uri=...)` under the base is kind 3, and bytes written in the same column land inline (0), packed (1) or
dedicated (2) by the column's thresholds. Measured on pylance 12.0.0, a bronze column held kinds `[3, 0, 1, 2,
null]` and the in-process stage wrote silver as `[Blob, None, None, None, None]` with a success. The in-process
lane now forwards kind-3 rows as pointers and carries every other non-null row's bytes beside them
(`service_kit.lakehouse.blobs.carried_blob_values`), reading only those rows by row id with
`read_blobs(..., preserve_order=True)`, so a null row is never selected and cannot misalign the result. Copying
the whole column once any managed row exists was the other option, and it was refused: it would turn every
pointer into a copy of the corpus.

Both carry paths take the upstream's blob field as the downstream field instead of a fresh `blob_field(name)`,
because thresholds and `rask.classification` are that field's metadata (`lance_docs/guide.md`, blob v2
thresholds). That decides a tier's first write only: the full-sync `merge_insert` and the by-id widening `merge`
keep the target's schema whatever the source field says (measured: a bare target stays bare, a rich target stays rich). The usual order is
ingest, cascade, then classify, so every stage write then copies the upstream's `rask.*` field labels onto
same-named target fields that lack them (`compute._carry_governance_labels`, one `update_field_metadata`
commit). It only adds: a label the target already holds is never replaced or removed, because the estate
defines no order between classification values and vending refuses on a label's presence, so absent to
present is the only change known to be tighter. It asks no `can_classify`: that rung exists because a writer
could clear a label, and this copies one a classifier already set upstream. Thresholds are not re-carried onto
an existing tier; only the governance labels are.

Not covered: the Ray stage job (`scripts/ray_stage_job.py`) still rebuilds blob fields bare and `measure_stage`
carries no labels after it; that half is LH-326, parked.
