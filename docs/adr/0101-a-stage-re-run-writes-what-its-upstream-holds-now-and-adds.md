# 0101. A stage re-run writes what its upstream holds now, and adds a column by `id` (LH-213, 2026-10-05)

The in-process stage skipped its write whenever the target's columns were a subset of the run's output and the
`source_rowid` lists matched element for element. Values were never compared, and a row corrected in place keeps
its stable `_rowid` and its position (it moves only its `_row_last_updated_at_version`,
`lance_docs/file_format.md:4270-4298`), so the corrected bronze payload never reached silver and silver kept the
first run's lineage. Its add-columns branch aligned by position through a second handle opened after the check, so
a commit landing between the two misfiled every derived value; and when the lists did not match, the full-sync
fallback could not widen at all (`merge_insert` refuses a source column the target lacks, "Append with different
schema", pylance 12.0.0), so that stage failed on every retry.

Chosen: the skip is deleted, and every run ends in the full-sync `merge_insert` on `id`, which carries corrected
values and re-stamps this run's lineage. A run that produces a column the tier lacks first adds it with
`LanceDataset.merge(out.select(["id", *new]), left_on="id")` on the handle that read the schema. That join is by
key, and Lance refuses the Merge when another commit has passed the handle's read version (measured: "preempted by
concurrent transaction Update"); a refused attempt re-reads the tier and decides again, at most three times.

The other answer, deciding "nothing changed" from upstream `_row_last_updated_at_version` against a watermark, was
not taken: the watermark would have to be read out of the target's lineage document, which a run without one does
not write, and it still could not see a change in the transform's own output.

Cost, stated: a redelivered trigger that the in-process executor's idempotency key does not catch (one handled by
another process) now rewrites the tier and commits a version. Writing only the rows whose content changed is
LH-212's conditioned merge, which restores that no-op by comparing values rather than identity.
