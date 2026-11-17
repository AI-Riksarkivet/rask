# 0218. A table carries no `lance.compaction.*` config: its compaction is planned from its maintenance policy (2026-11-17)

**Status:** accepted. Source: XC-206 (#804), items Q3 and L7 of the 2026-11-09 audit, which found the refusal LH-538
built with no record. ADR 0199 listed it as "LH-538 (no record)": a record of its own stating the reason, or the
refusal dropped. This is the record.

## Context

Lance keeps a table's config in its manifest, and `lance_docs/lance/format/table/table_config.md` says of the `lance.`
keys it lists: "Any library that reads or writes the Lance format is expected to understand and honor" them. Eighteen
of those keys are `lance.compaction.*`; the page says Lance ignores an unrecognized one with a warning. They are set
with `Dataset.update_config()`, an ordinary transaction, so any writer of the table can set them.

Lance's planner reads them. Measured 2026-11-17 on pylance 14.0.3, on a local table of 25 one-row fragments:

| Manifest config | Option passed to `Compaction.plan` | Plan |
| --- | --- | --- |
| none | none | 1 task over 25 fragments |
| `lance.compaction.max_source_fragments=2` | none | no task |
| `lance.compaction.max_source_fragments=2` | `target_rows_per_fragment=1024` | no task |

The config wins even where the caller passes an option of its own.

The lakehouse compacts nothing itself (ADR 0162): maintenance decides what is due and a bring-your-own job does the
rewrite and commits it through `version/create`. Maintenance decides with that same planner
(`maintenance.services.due.compaction_due` calls `Compaction.plan` with the fragment size the table's maintenance
policy names), and the policy is set at an owner-tier door (`POST /management/v1/table/{id}/policy/set`), audited, and
inherited from the namespace and the project.

## Decision

**No door stores a `lance.compaction.*` key, and a table that carries one is refused where it would enter.** A table's
compaction is planned from its maintenance policy alone.

- Every door that takes a `properties` map answers 400 to one that names such a key
  (`catalog.core.formats.reject_unsupported_properties`).
- `version/create` refuses a commit whose manifest config carries one (`catalog.services.managed_versions`).
- The register door refuses a dataset that carries one (`catalog.services.table_bases`).
- The restore door refuses a version that carries one (`catalog.services.dataplane`).

The refusal is by prefix, so a key a later Lance honours is refused before it is documented.

The reason, against the four facts:

- **Lance-idiomatic.** The format asks a library to honour a `lance.` key a table carries, and it does: nothing here
  reads a table past such a key or plans around one. What the catalog declines is storing the key, as it declines
  `lance.auto_cleanup.*` (ADR 0102, ADR 0103). The page lists what a key means to Lance, not that every catalog must
  accept every key.
- **Zero trust.** With the key accepted, any identity holding a write credential could silence the due list for a table
  (the third row above) or steer every rewrite of it, with no audit record and no grant that names the act. The policy
  door asks the owner tier and leaves a record.
- **GitOps.** A table's maintenance is then stated in one place, the policy record, which a project's declaration can
  carry. A second source in the manifest is state no reconcile reads.
- **Cloud native.** Unchanged: no workload is added or removed.

## Consequences

- A stock client that sets compaction config with `update_config()` is refused at `version/create` and told to set the
  table's maintenance policy. The knobs the policy does not carry (`max_rows_per_group`, `compaction_mode`, the
  `max_source_*` bounds and the rest) are a bring-your-own job's own `compact_files` arguments, which the refusal does
  not touch: it judges the manifest's config, not a job's options.
- A table registered from elsewhere must have the keys removed first (`delete_config_keys`, before `RegisterTable`, as
  the refusal says).
- Reopen this when the policy must carry a knob Lance reads only from config, or when the due list stops being planned
  by `Compaction.plan`.
