# 0164. A stock client commits on a branch through `CreateTableVersion`; `CreateTableBranch` creates it (2026-10-15)

**Status:** accepted. Source: owner, 2026-10-15, through the multi-question tool on LH-490 (#632): how a branch is
created, what the table's write credential reaches, and the test seams. Amends ADR 0157 (a branch commit was refused
406), ADR 0163 §5 (an obligation on a branch head can now be fulfilled) and the write tier of LH-202 / LH-229 (the
table's credential reached main only).

## Context

`version/create` refused a commit naming `branch`, so a stock client could write a branch only through the catalog's
own `insert` / `merge_insert?branch=`, and a bring-your-own job could not fulfil an erasure obligation on a branch
head (ADR 0163 §5). `lance_docs` gives the shape: the spec puts `branch` on `CreateTableVersion`, `ListTableVersions`
and `DescribeTable` and defines `CreateTableBranch` (`ns_catalog/spec.yaml`); a branch is a shallow clone inside its
table, keeping its own version history and files under `tree/<name>/` (`lance_docs/lance/format/table/branch_tag.md` § Branch Dataset Layout,
`lance_docs/lance/format/table/layout.md` § Shallow Clone).

Measured on pylance 13.0.0 against the catalog under uvicorn:

- A stock client opens a branch with the table's own `DescribeTable` (no `branch`), then sends `ListTableVersions` and
  `CreateTableVersion` with `branch`, staging under `tree/<branch>/` on the credential that describe vended. Append,
  delete, update, a compaction (`ReserveFragments` then `Rewrite`, copying the inherited fragments into the branch),
  an index build and an index drop all arrive this way, and the `dir` backend finalizes a staged branch manifest.
- Lance's own `create_branch` commits a shallow `Clone` on the new branch and then writes `_refs/branches/<name>.json`
  itself with put-if-absent (`rust/lance/src/dataset.rs` `create_branch`, `rust/lance/src/dataset/refs.rs` at
  v13.0.0); with the commit refused, no ref is written.
- Lance lists `_refs/` with a delimiter listing that returns child prefixes as well as files and reads every name
  ending in `.json` as a ref (`refs.rs` `fetch_tags` / `Branches::fetch`, `lance-io` `read_dir`), so one planted
  `_refs/tags/<x>.json/<y>` object fails every tag listing.
- Lance finds a ref's manifests by listing its `_versions/` recursively and reading each object's file name
  (`rust/lance-table/src/io/commit.rs` `resolve_version_from_listing`), and the version hint never decides the latest
  version (it is read only where listing is unordered, and verified there).
- On MinIO `RELEASE.2025-04-22T22-12-26Z` (the estate's build), a session policy over 2,048 bytes is refused
  (`cmd/sts-handlers.go` `populateSessionPolicy`). Bisected with a 63-character bucket, today's write policy fits a
  table prefix of 109 characters; per-directory branch globs fit 18; one `tree/*` grant with the final-manifest Deny
  widened to any depth fits 89 (152 to 132 at a 20-character bucket).

## Decision

1. **`CreateTableBranch` creates a branch.** The spec door (`POST /v1/table/{id}/branches/create`, `can_create_branch`,
   the owner rung) writes the branch's first version and its ref, the five steps the LanceDB blog's branching post
   (<https://www.lancedb.com/blog/branching-and-shallow-clone>) lists. A stock client's own `create_branch` reaches `version/create` for a branch the table does
   not list and is refused 404 (`TableBranchNotFound`, code 22) naming `CreateTableBranch`, with nothing committed.
2. **Every later commit on a branch goes through `CreateTableVersion` with `branch`, judged like main's.** The judge
   opens the staged version at the branch's own location (`tree/<branch>`) against the branch version it follows,
   applies every rule main's commits meet, judges the branch's bases against the table's root (a base inside the table
   is its own, `base_registry.is_own`), and finalizes through the backend with `branch`. The rungs are the table's, as
   for `insert?branch=`: the FGA model has no branch object. The lineage event names the branch.
3. **The compaction gate weighs only bases outside the table.** A branch sets flag 16 and reads its parent through a
   dataset-root base on the table's root (or its parent's `tree/<name>`); those bases are the table's own history, and
   a branch's compaction copying what it inherited is what a branch's own history is for. The gate is one module both
   maintenance's due list and the judge ask (`service_kit.lakehouse.compaction_gate`).
4. **The table's write credential reaches every branch.** The write tier of a `DescribeTable` (or table-scoped) vend
   adds put on `<table>/tree/*` and widens the final-manifest Deny to `<table>/*.manifest` at any depth. `_refs/` and
   `_mem_wal/` stay ungranted. A branch-scoped vend keeps its own branch's directories only.

## Considered options

- **Serve Lance's own `create_branch`** by judging its `Clone` at `version/create`: the client then writes the ref
  itself, which needs a writer grant under `_refs/`. A put over an existing key is not refusable by a session policy,
  so a writer could rewrite a branch's `parent_version`, which cleanup's pins read, or fail every ref listing with a
  planted prefix. Not offered.
- **Judge the `Clone` and have the catalog write the ref**: zero-trust, but the client's own put-if-absent of the ref
  then fails (AccessDenied on the estate), so a stock `create_branch` raises an error although the branch exists.
- **Add the branch grant only for tables that have a branch**: keeps a branchless table's headroom, but a credential
  vended before a branch existed cannot stage on it until it refreshes.
- **Keep the table's credential on main**: a stock client with a vended credential could not stage on a branch at all;
  only the documented per-branch isolation (the LanceDB blog's branching post, <https://www.lancedb.com/blog/branching-and-shallow-clone>: "read-only on the main dataset and
  write-only on the branch directory") would be kept, and the FGA model grants a writer every branch anyway.
- **Vend per branch through `DescribeTable.branch`**: the spec field exists, but pylance 13.0.0 never sends it
  (`dataset.rs` `write_into_namespace_impl` describes with the table's id alone), and the describe door refuses a
  branch it cannot describe. Not built until a client sends it.

## Consequences

- A pylance user creates a branch with `create_table_branch` on the namespace, then writes it with pylance as usual;
  `ds.create_branch()` on the estate answers 404 naming the door, and its staged files stay under `tree/<name>/`.
- A writer of a table may stage on every branch of it, and every such commit is judged; a stray staged file is garbage
  the judge never admits.
- The longest table prefix a write credential fits within MinIO's 2,048-byte session policy drops from 109 to 89
  characters at a 63-character bucket (132 to 112 at 40, 152 to 132 at 20; the catalog's own policy, bisected on the
  estate's MinIO build). A table whose prefix falls in that 20-character band vended a write credential before and now
  answers its describe with the store's refusal (503); one longer did not vend before either. The owner chose this cost
  (2026-10-15). Measured before deploying: the 15 live tables the estate admin lists write policies of 1,016 bytes at
  most, against a bound of 2,042 for the compact JSON (MinIO's re-marshal is 6 bytes longer).
- A silver or gold branch commit through `version/create` names its run like main's: the run rides the staged
  manifest's transaction properties, judged by the same rule, and the backend finalizes that manifest with `branch`.
  The catalog's own `insert?branch=` door still refuses a run, because its stamped commit finalizes main only.
- An erasure obligation's branch heads are fulfilled by the job through branch commits, and the verification no longer
  says a branch is owed until branch commits are served.
