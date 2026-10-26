# 0174. The provenance namespace is maintained as every table is: a declared retention, a bring-your-own compaction job, and expiry after each compaction (2026-10-23)

**Status:** accepted. Source: owner, 2026-10-23, through the multi-question tool while taking LH-529 (#709). Amends ADR
0158 (a compaction's commits under the provenance namespace are announced) and ADR 0162 (a compaction needs `maintainer`
and `reader`, not `writer`).

## Context

Lineage's cost per recorded event grew with the estate's age until criterion 5's catalog-kill drill outran its 300 s
window. Measured offline on pylance 13.0.0 against RustFS 1.0.3, and read back on the live estate the same day:

- On an S3-compatible store Lance finds a table's latest version by listing `_versions/` on every open
  (`resolve_version_from_listing`, `rust/lance-table/src/io/commit.rs` at lance v13.0.0). The version hint is written and
  read only where listing is not lexically ordered (`uses_version_hint`, `commit.rs:346-348`), which S3 is except S3
  Express (`rust/lance-io/src/object_store/providers/aws.rs:261`). RustFS answers that listing's first page with every
  manifest key, up to 1,000, at about 0.85 ms per key (0.574 s for `provenance$events`' 729 versions live).
- Every scan of a key or filter column reads one data file per fragment, so a merge's key join and a filtered count grow
  with the fragment count.
- Each recorded event commits to each provenance table it touches, so each gains a version and a fragment per event.
  Nothing removed either: no policy covered the namespace, so maintenance kept the sweep's seven days of versions, and
  nothing compacted it, so the sweep listed the tables as due with no job to act on the list. Compaction alone was
  measured not to help (2026-10-21): it leaves the versions.
- One event made about six catalog calls, which opened the three tables 12 to 15 times. The catalog's describe door
  listed `_versions/` up to three times per call: once to judge whether the table is only declared, once to open it, and
  once more for the credential it vended, which lineage's version reads never use.

Lance's own guide answers this shape: "many small appends will lead to a large number of small fragments", met by
`compact_files` and `cleanup_old_versions` run "as a periodic background task" (`lance_docs/lance/guide/read_and_write.md` § Table
Maintenance).

## Decision

- **The provenance namespace's retention is declared.** The catalog writes the namespace's maintenance policy at boot,
  keep the last `LANCE_PROVENANCE_RETAIN_VERSIONS` (chart `services.lineage.provenanceRetainVersions`, 20). When none is
  declared it removes the policy it wrote, while that stands as written, so the estate converges to what git says; a
  hold set through the policy door, or set over the declared policy there, stands, because the door merges a change into
  the record it finds. Maintenance's expiry reclaims down to the declared policy with Lance's own
  `cleanup_old_versions`. No lineage read holds a version longer than one read.
- **The provenance tables are compacted by a bring-your-own job**, `services/table-maintenance`, run as a Kubernetes
  CronJob in a release of its own (`deploy/fixtures/table-maintenance` in development; the platform's in production).
  It reads the catalog's due list as its own ServiceAccount principal and runs Lance's own compaction protocol on each
  table the list names it, every commit judged at `version/create`. It imports no rask package. The product chart only
  names its account, `services.lineage.provenanceMaintainers`, and the catalog grants each named account `reader` and
  `maintainer` on the namespace at boot. Who may write and who may compact the namespace are configuration alone: each
  boot revokes a direct `writer` or `maintainer` tuple there that the settings no longer name, with the `reader` granted
  beside a maintainer, and leaves a `reader` granted any other way.
- **Expiry follows each compaction.** A compaction's commits under the provenance namespace are announced, and
  maintenance's write-event lane expires the table after it, as it expires every table after a commit (ADR 0162 §5).
  Lineage's own row writes there stay unannounced, so recording the announcement ends at its row.
- **A compaction needs no writer rung.** `version/create` admits a writer or a maintainer (`can_create_version`), and
  each staged operation still asks its own rung: the writer operations and a table's first version `can_write_data`, a
  compaction's two commits `can_maintain`. The spec doors vend a caller holding `can_read_data` and `can_maintain` the
  write tier's puts.
- **A detailed describe judges a table by the open that loads its metadata**, asking the storage itself only when the
  open finds no dataset, and a read of a table's version asks for no credential. Lineage's readiness read of its feed
  lists the feed's `_versions/` once.
- Recording several deliveries in one commit is its own row, LH-533 (#718).

## Considered options

- **The compaction in maintenance's or the catalog's pods:** rejected by the owner's ruling that the lakehouse builds
  nothing (ADR 0162).
- **The CronJob in the product chart:** rejected. Compute is bring-your-own and out of the product chart (ADR 0153).
- **A CronJob that compacts the provenance namespace alone:** not chosen. The due list is already estate-wide, so the
  same job serves any table whose owner grants it `maintainer`.
- **Knative or KEDA to trigger the job:** not chosen. The estate runs neither, and periodic batch work is what a CronJob
  is.
- **No compaction, the versions bounded alone:** rejected. Fragments grow by one per event without end.
- **Expiry by the 30-minute sweep alone, or by a timer of its own on maintenance:** not chosen. Expiry follows a commit
  in this estate, and a compaction is the commit that follows a provenance table's growth.
- **Lance's commit-path auto-cleanup:** rejected. Version reclamation has one owner, maintenance's expiry (LH-245).
- **The job as writer and maintainer:** rejected. A writer on the provenance tables could commit feed rows, and a
  maintainer's commits rewrite how a table is stored, never what it says (`model.fga`).
- **A narrower credential than the write tier for the job:** not available. A compaction puts new data files under
  `data/`, where a session policy can name a prefix but not refuse a put over a key already there
  (`catalog.core.vending.build_session_policy`).

## Consequences

- A provenance table keeps between about 20 and 40 versions, plus the commits of one compaction interval (5 minutes in
  development), and a fragment count bounded by that interval.
- A compaction that loses its version to one of lineage's writes leaves the manifest it staged: Lance's own cleanup
  never lists the name, and the job holds no delete. Maintenance's expiry removes each manifest staged for a version the
  table already holds, once it is ten minutes old (`MAINTENANCE_STAGED_MANIFEST_GRACE_SECONDS`), in the pass that
  reclaims versions; measured live before it, one about every 20 minutes on `provenance$runs`.
- An estate that installs the product chart without a compaction job keeps bounded versions and growing fragments; the
  due list shows the tables as due.
- A pipeline compacting its own tables needs `reader` and `maintainer` on them, not `writer`.
- The job's commits are judged and its puts are not: on a table it maintains it can put over a committed data file, as a
  writer can, which changes what the table says without a commit.
- A catalog replica that boots with an older list of maintainers during a rollout grants and revokes by that list until
  a replica boots again.
- A reader whose pinned version is expired between its two queries gets the spec's 404 and retries.
- Pinned by `services/catalog/tests/test_the_provenance_namespace_is_kept_as_its_settings_declare.py`,
  `tests/integration/test_the_commit_feed_is_a_lance_table_followed_forward.py`,
  `services/table-maintenance/tests/test_the_job_compacts_every_due_table_it_may_maintain.py`,
  `services/catalog/tests/test_a_stock_client_commits_every_operation_through_version_create.py`,
  `tests/integration/test_the_spec_doors_vend_the_tier_fga_grants.py`,
  `tests/integration/test_a_door_judges_whether_a_table_is_only_declared_by_its_storage.py` and
  `tests/unit/test_the_catalog_is_told_who_maintains.py`.
