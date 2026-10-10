# 0106. The stage write is one module both engines land through (CP-056 step 2, 2026-10-05)

The in-process engine (`compute.transform_stage`) and the Ray stage job (`scripts/ray_stage_job.py`) each held their
own write: create, converge, widen, retraction, conflict re-plan, dataset-id stamp, label carry, lineage index,
contract and marker. Every write-semantics fix landed twice, and the Ray copy had drifted: it rebuilt blob fields bare
(no thresholds, no `rask.classification`), copied external blob bytes instead of forwarding the pointer, merged media
per batch and retracted by run id, had no widen, and never corrected a merged tier's declared dataset id.
`service_kit.lakehouse.tier_write` now owns everything from produced rows to the run's ONE marked commit:
`plan_window` decides full or delta before a row is read, and `write_tier` disarms auto-cleanup, retracts from
Lance's deletion record (delta), widens by id, corrects the declared id, carries labels, then creates or converges in
one commit carrying the marker; the lineage index follows it and the contract is checked last. Each engine keeps only
how it produces rows: the in-process table, and on Ray a driver table (head and delta), a re-runnable media stream,
or a staged dataset from `lance_ray`. The Ray image ships `service-kit`, so it imports the module the stage runners do.
This absorbs LH-326 (the Ray half of LH-212, LH-216 and LH-217), and LH-212's conditioned merge now lands in one place.

Deliberate behaviour changes:
- The in-process lane builds its order with the Ray lane's builder (`stage_submit.build_work_order`; `transform.
  _work_order` is gone), so it honours the trigger's `from_version` and runs the delta converge, checks the lane's
  cardinality contract, and marks its last commit with the run key.
- A destination without stable row ids is refused on both lanes (`UnstableRowIdsError`). The Ray lane's directory
  wipe of such a tier (`_reset_if_legacy`) is deleted: it went around the catalog and its trash.
- A full run that produced no rows from an upstream that holds rows is refused before any commit on both lanes
  (`EmptyFullSyncError`); the Ray lane refused this only for a staged set.
- A blob upstream always converges whole, on both lanes: its artifacts are decided from the column's first non-null
  payload, which a window cannot see.
- A streamed source (the Ray media lane) converges slice by slice: each slice but the last is an unmarked upsert, the
  rows to retract are found from the tier's `id` column and deleted by key, and the held-back last slice is the run's
  one marked commit. One whole-tier merge peaked at 1.29/3.43/6.05 GB VmHWM for 0.2/0.8/1.6 GB of 1 MiB blobs, and a
  `when_not_matched_by_source_delete` reads every target payload even from an id-only source; with the lakehouse
  allocator bound the sliced converge held 0.48/0.51 GB over 256/800 MiB tiers in 16-row slices. The media scan also
  bounds its read-ahead (`io_buffer_size`, Lance's default is 2 GB).
- The lineage index is built by the write on both lanes, after the marked commit; `measure_stage` no longer rebuilds
  it, and measures the marked version the outcome names.

The lakehouse and Ray images ship together: a stage runner on the new image no longer builds the index the old Ray
job relied on it to build, and an old stage runner would rebuild what the new job already built.
