# 0212. Publication goes, and a table's data contract is checked on every commit (2026-11-16)

**Status:** accepted. Source: owner, 2026-11-16, through the question tool: "Pure: remove publication". It replaces the
owner's 2026-11-15 choice "Keep publish, contract replaces gate". Row LH-594 (#832). Supersedes
[ADR 0161](0161-the-tag-gate-runs-at-publish.md) and amends [ADR 0156](0156-the-audit-grilling-amends-0155.md) C1 and
C9.

## Context

The catalog carried a promotion workflow from the time the lakehouse ran its own medallion stages:

- a quality gate declared per project (`/management/v1/project/{id}/gate/*`);
- a `publish` door that scanned a version's data against that gate and moved a reserved tag, `published`;
- review holds, which a `validator` approved or rejected, and a timer that expired them;
- the `validator` rung and `can_promote`, and a `publisher` rung for the door;
- the tag names `published`, `rask.held` and `rask.publishing.*`, which the spec's own tag doors refused.

That fixed one publication pattern on every user. It also left a real gap open: a table had no contract of its own, so
a commit that dropped, renamed or retyped a column its consumers rely on was refused nowhere.

The reference answers (read 2026-11-16):

- **Lance** (`lance_docs/lance/guide/tags_and_branches.md`, lance `main` a33c4682d). No publish, gate or promotion. A
  tag is a user-named pointer to a version (`v1-prod`, `stable`), created and moved with `tags.create` and
  `tags.update`; a tagged version is exempt from cleanup; branches stage work.
- **lance-namespace** (`lance_docs/ns_catalog/spec.yaml`). The tag and branch doors: `CreateTableTag`,
  `UpdateTableTag`, `DeleteTableTag`, `ListTableTags`, `GetTableTagVersion`, `CreateTableBranch`. No tag name is
  reserved.
- **Lance's schema** (`lance_docs/lance/format/table/schema.md` § Schema Evolution, `format/table/index.md`
  § Unenforced Primary Key). A nullability change keeps a field's id and a cast replaces the field; the primary key is
  unenforced and fixed once set. Lance names no provenance column: a row's lineage is its stable row id.
- **Lakekeeper** (main 3b2c949, `crates/lakekeeper/src/service/contract_verification.rs`). No publish or promotion. A
  `ContractVerification` hook can veto a metadata change on commit with a 409; it sees metadata, never data, holds
  nothing, and the open-source binary installs no verifier.
- **ODCS v3** (Open Data Contract Standard, Bitol, Linux Foundation) is the open contract format.

## Decision

1. **Publication is deleted**, with every consumer and no compatibility path (the estate is pre-production):
   - the project gate's doors, `lakehouse.gate_specs`, the `_gates` prefix, `gate_set` / `gate_deleted` and the `gate`
     object type;
   - `POST /management/v1/table/{id}/publish`, `promotion/approve`, `promotion/reject`, `GET /management/v1/promotions`,
     the catalog's publication and promotions services, `lakehouse.quality`, the `_promotions` prefix and the
     promotion-expiry timer (`catalog.promotion.expiryPeriodSeconds`);
   - `validator`, `validator_effective`, `can_promote` and `can_grant_validator` in the authorization model;
   - the events `table_published`, `promotion_held`, `promotion_rejected`, `promotion_expired`, the `publish_table`
     lineage run, the mover record a publication kept, and the commit reconciler's tag-move leg;
   - the console's Promotions pages, its sidebar row and Home's held count.
2. **Marking a version ready is the user's.** A pipeline tags the version it vetted, under a name it chooses, through
   the spec's tag doors at `can_create_tag` / `can_update_tag`, and its readers open that tag. No tag name is the
   catalog's but the one it writes itself: `rask.clone.*`, the tag a registered clone holds on the version it reads
   (ADR 0198). The rung that lets a pipeline tag without owning is renamed from `publisher` to `tagger`.
3. **A table holds a data contract its owner sets.**
   - **Doors.** `POST /management/v1/table/{id}/contract/set` takes an ODCS v3 document; `…/contract/describe` answers
     the one held, as it was set; `…/contract/delete` removes it. Set and delete ask the new `can_set_contract`
     (`owner_effective`); describe is a metadata read. They follow the catalog's own route grammar
     (`POST /<object>/{id}/<action>`), which is what the router's rung map reads.
   - **The subset.** The document is read strictly: `apiVersion` (v3), `kind`, `id`, `version` (semver), `status`, the
     descriptive `name` / `tenant` / `domain` / `dataProduct` / `tags`, and one `schema` object whose `properties`
     name columns (`name`, `physicalType`, `logicalType`, `description`, `required`, `primaryKey`,
     `primaryKeyPosition`, `unique`, nested `properties` for a struct). Any other field is refused 400. A stated
     `logicalType` is held to the family its Arrow type belongs to.
   - **No promise nobody keeps.** A `quality` rule, `slaProperties`, or `unique` on a column that is no primary key is
     refused 406: the catalog scans no data and runs no timer.
   - **What each field becomes.** `required: true` is a non-nullable Lance field, which Lance enforces on every
     fragment. `primaryKey` is a field of Lance's unenforced primary key, whose repeats every commit is already
     refused for. `physicalType` is the column's Arrow type.
   - **The table meets it when it is set.** A contract the table does not meet now, on main or a branch, is refused
     409 (code 19). The door reads the table before it writes the record and again after, and takes the record back
     when the second read fails, so a commit landing meanwhile is seen.
   - **An unreadable record is the owner's to end.** A record that is there and is no contract refuses every commit
     (it may be the contract a commit breaks); a set replaces it and a delete removes it.
   - **A breaking change needs a major version.** A contract that takes back what the standing one promises (a column
     no longer named, a type changed, `required` or `primaryKey` dropped) is refused unless its major version is
     higher; any changed contract carries a higher version.
   - **Record.** `record_key("_contracts", "table", <uuid>)` on the control root, keyed by the table's uuid, so a
     rename keeps it and the table's ending ends it (`lakehouse.object_records`). The first set is a put-if-absent and
     a replacement a write over the record it judged.
   - **Announced** untargeted as `table_contract_set` and `table_contract_deleted`; the router's decision on
     `can_set_contract` is the audit record.
4. **The contract is checked on every commit, and nowhere else.** A commit that drops, renames, retypes or
   loosens a contracted column is refused 409 (code 19), naming the column and the contract's version, where every
   commit already passes: `version/create` for a stock client (`managed_versions.judge`), `commit_stamped` for the
   catalog's own doors (a restore included), and the create door's Overwrite. A column is named by its path, so a
   rename reads as a removal. Additive changes pass. A commit is refused for a violation it introduces: one the
   version it follows already had (a branch cut from a version older than the contract, a commit that landed as the
   contract was set) is not the next writer's, so no table is frozen by a state its writers did not make. Nothing
   holds a version, scans a row or runs a freshness timer.
5. **`source_rowid` is no longer reserved.** Its only reader was the publish gate. Lance names no such column, and the
   platform names no column of a table's (ADR 0156 C9): a column a consumer relies on is held by the table's contract.
6. **Recorded, not built.** A `DataContract` custom resource, reconciled by the rask operator (XC-210, #817), will
   later drive the same door from git.

## Not taken, stated

- **A publish door without holds.** The owner's first click on 2026-11-16, withdrawn as a mis-click: it keeps a
  reserved tag and a second way to say what a tag already says.
- **Quality and freshness rules.** They need a data scan and a clock. A pipeline that vets its data tags the version it
  vetted.
- **A contract set from the properties door or at create.** A contract is set on a table that exists, by its owner, at
  a door of its own, so the trail names the act.
- **Serializing a set with every commit.** A set takes no commit turn. A commit that read no contract and lands after
  the door's second read leaves a violation the table shows, which no later commit is refused for.
- **Rewriting stored `validator` and `publisher` tuples.** Measured 2026-11-16 on the pinned OpenFGA: a tuple under a
  relation the model no longer defines is ignored by `Check`, still listed by `Read`, and deletable. Each ends with its
  object or its principal, as every tuple does (`auth/CHANGELOG.md`).

## Consequences

- **A deployment that used `publish`** tags instead: `tags/create` once, `tags/update` after. A pipeline identity that
  held `publisher` is granted `tagger`.
- **Every commit reads one more record**, the table's contract, on the control root.
- **Four facts:**
  - GitOps: a producer's contract lives in its own repository and is applied at the door, idempotently; nothing is
    live-only, and one timer fewer runs.
  - Cloud native: no new workload.
  - Lance-namespace and table idiomatic: versions are marked with Lance tags through the spec's doors, under the
    user's names, and the contract hands Lance what Lance enforces (nullability, the primary key).
  - Zero trust: an owner rung sets a contract, and the catalog judges every commit, which a stock client cannot
    bypass.
