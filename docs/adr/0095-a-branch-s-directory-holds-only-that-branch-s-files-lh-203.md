# 0095. A branch's directory holds only that branch's files (LH-203, 2026-10-05)

The format joins a branch name verbatim onto `tree/` and allows `/` and `_` in it (`lance_docs/file_format.md` § Branch
Name, § Branch Dataset Layout), so `a/b` lives inside `tree/a/` and `a/_versions` inside branch `a`'s version directory.
Lance treats what lies under a branch's directory as that branch's. Measured on pylance 12.0.0: a reclaim of `a` deleted
`a/_versions`'s manifests below `a`'s head and a cold read of it failed `Not found`; deleting `a/_versions` deleted
`a`'s whole history; deleting `a` while `a/b` existed removed only the ref. The rule that a branch's directory holds only
its own files is therefore rask's, and lives in `service_kit.lakehouse.branch_layout`.

- Create refuses a segment that is a layout name (`_versions`, `_transactions`, `_deletions`, `_indices`, `_refs`,
  `_mem_wal`, `data`, `tree`) as InvalidInput (400, 13), and a name that is a `/`-prefix of an existing branch or has
  one as InvalidTableState (409, 19): the name is well formed, the table's state refuses it.
- A branch-write vend grants `tree/<b>/{_versions,_transactions,_deletions,_indices,data}/*`, not `tree/<b>/*`.
- Delete goes through the branches nested inside the named one, deepest first, so no files are left behind. Deleting a
  branch that lies inside another's file directories (`a/_versions`) is refused 409, because Lance would delete the
  parent's files with it; deleting the parent removes both. A branch outside that set forked from any member of it
  refuses the whole delete (409) before anything is removed, since pylance refuses a forked-from branch only after the
  nested ones are gone. Considered and not taken: refusing a delete with nested
  branches, which leaves such a legacy pair undeletable by either name.
- Every reclaim refuses a branch with another inside it: the sweep (`refused_by="nested_branch"`), `maintenance/run` and
  `version/delete` (409). The create refusal means only a table that predates it carries such a pair.
- The sweep lists branches from `_refs/branches` (one listing per dataset, the cost of the `tree/` probe it replaces),
  so a nested branch is maintained and a deleted branch's leftover directory is not.

Not done here: describe does not honour `DescribeTableRequest.branch` with `vend_credentials` (it still refuses a non-main
branch); a write vend for `a` still reaches a legacy `a/_versions`'s files, which live under `a`'s `_versions/`; the
erasure's per-ref reclaim does not ask the nested-branch gate.
