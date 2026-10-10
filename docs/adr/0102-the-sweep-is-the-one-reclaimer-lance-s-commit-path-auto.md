# 0102. The sweep is the one reclaimer: Lance's commit-path auto-cleanup is closed (LH-245, 2026-10-05)

Lance runs automatic cleanup inside the commit of whoever writes, every N commits, from the
`lance.auto_cleanup.*` manifest keys (`lance_docs/guide.md:3857-3923`). Measured on pylance 12.0.0: with
`interval=1, older_than=0s` one ordinary append took a table from versions 1..7 to 7..8, and a
`LanceDataset.commit` of an `Append` (the catalog's `/commit` shape) did the same. That deletion passes none of
the sweep's gates: a `cleanup_enabled=False` hold and a protected base both return before reclamation, it runs
under the writer's identity, and nothing records it. pylance 12's Python `write_dataset` has no
`skip_auto_cleanup`; it takes only `auto_cleanup_options`, which arms the lane at create.

Chosen: retire the lane rather than govern it. The row offered "allow it only where maintenance is the sole
writer", and nothing in the estate can establish that: the catalog's doors, the ingest lander and the stage lanes
all commit to tables maintenance also maintains. So the policy field `auto_cleanup_interval_commits` is gone from
`PolicyRequest`, `PolicyResponse`, `DatasetPlan` and `compact_one`, and every pass deletes whatever
`lance.auto_cleanup.*` keys a table carries right after it opens it, before the nested-branch, manifest-flag
and protected-base refusals (`optimize.py::_disarm_commit_path_cleanup`). All keys under the prefix, not the two
`disable_auto_cleanup` deletes: measured, that call leaves `lance.auto_cleanup.retain_versions` standing. The write
happens only when a key is present, because `delete_config_keys` commits a version even with nothing to delete
(measured: 12 -> 13). The write probe counts an armed table as "may write", so the disarm is signed by the
vended credential where one is on offer. A failed disarm is reported as an `auto_cleanup:` error and retried
next tick; the sweep summary's `auto_cleanup_disarmed` counts the tables found armed.

The row's third item, mapping `retain_versions` onto Lance's own key, is moot once the lane is closed:
`retain_versions` reaches `cleanup_old_versions` directly, which honours it.

Not covered: a table whose policy skips it (`compact_enabled=False` or a cadence interval not yet elapsed) is
not opened, so its keys stay until a tick maintains it; no rask writer arms the lane today (none passes
`auto_cleanup_options` or writes the keys), so only a key a writer sets outside rask, or one left from earlier
policies, reaches that window. A stored policy record that still carries the retired field is ignored.
