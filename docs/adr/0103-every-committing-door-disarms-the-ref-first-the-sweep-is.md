# 0103. Every committing door disarms the ref first; the sweep is the backstop (LH-245, 2026-10-05)

The sweep-only disarm above failed live on helm rev 280: the queue lane visits about 5 datasets a minute against
606 work orders a tick (LH-195), so an armed scratch table waited 45 minutes unvisited, and three appends through
the insert door deleted versions 1 and 2. A design that leaves a table armed until the sweep reaches it cannot meet
"an ordinary append can never delete a version".

Measured on pylance 12.0.0 before choosing: the hook reads the config of the manifest a commit PRODUCES. An
`update_config` that arms a table deletes versions in its own commit; a `delete_config_keys` that disarms one
deletes none; an append through a handle opened before the disarm rebases onto the disarmed manifest and deletes
none (`insert` and `LanceDataset.commit` of an `Append` at the stale read version alike). `.interval` alone arms
the hook. Schema-metadata keys arm nothing. An overwrite carries the config forward; tags and branch creation
delete nothing. A restore produces the restored version's config, so restoring an armed version re-arms the
table inside the restore's commit. pylance 12's Python `write_dataset`, `insert`, `merge_insert` and `commit`
take no `skip_auto_cleanup` (the native `WriteParams` field exists; `write_dataset`'s params dict never sets it).

Chosen: (1) no door can arm. The property guard every create/declare/register/update/namespace door already
calls (`core/formats.py`, renamed `reject_unsupported_properties`) refuses `lance.auto_cleanup.*` 400 code 13,
and LH-202 leaves writers no manifest write of their own, so only a maintain-tier credential or an out-of-band
writer can arm a table. (2) Every catalog door that commits disarms the ref it commits on first, through
`core/namespace.disarm_commit_path_cleanup` over the shared `service_kit.lakehouse.auto_cleanup.disarm`: insert,
merge_insert (both arms), update, delete, the three column doors, field and schema metadata (both paths),
`/commit` (main and branch), `/compaction_commit`, create in overwrite mode, the merge-key index build, the two
index builds and drop, the in-pod compact and reindex doors, and the erasure door, which disarms main and every
branch head (`disarm_every_ref`) before its first delete: its deletes and compactions are commits, and its
`retain_days` window is the only reclamation it may do. The disarm is a config-only commit made only when a key
is present. (3) The restore door refuses a version whose config carries the keys, because no disarm can precede a
commit that brings them. (4) The two writers that commit with static keys outside the catalog disarm with the
same `disarm` before their first commit on a tier: the medallion in-process lane (`compute.open_to_commit`, for
the seed, the stage's widening merge, full-sync merge, label carry and lineage index, and the media ingest's
overwrite of an existing bronze) and `scripts/ray_stage_job.py` (`_disarm_destination`, once per run, before any
of its merges, deletes or writes). (5) The sweep's disarm stays as the backstop for a table nobody commits on.

Not covered: `alter_table_backfill_columns` and `refresh_materialized_view` are native stubs on the `dir` backend
(measured: NotImplementedError) and commit nothing; `alter_transaction` was not probed and does not disarm. The
Ray job's staging dataset is created by overwrite and not disarmed.
