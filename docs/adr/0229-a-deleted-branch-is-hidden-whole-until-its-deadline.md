# 0229. A deleted branch is hidden whole until its deadline (2026-11-24)

**Status:** accepted. Source: LH-429 (#482), split out of LH-178 on 2026-10-11. Owner, 2026-11-24, through the
multi-question tool on the measurements below: "Keep the branch whole".

## Context

`POST /v1/table/{id}/branches/delete` destroyed a branch outright, while a table and a namespace are kept for their
warehouse's grace period (#75, #96). The row asked for a recoverable delete that recorded where the branch was cut and
recreated it there.

`lance_docs/lance/format/table/branch_tag.md` describes a branch as a reference file
(`_refs/branches/<name>.json`, the name URL-encoded) and a directory (`tree/<name>/`). It says nothing of a directory
whose reference file is missing, and `lance_docs/lance/guide/tags_and_branches.md` offers no way to keep a deleted
branch. So what follows stands on measurement, on pylance 14.0.3 on a local root, and is to be measured again at each
pylance bump:

- Lance's `branches.delete` removes `tree/<branch>/`. `create_branch(name, reference=(parent, version))` afterwards
  gives a branch that reads the parent's rows at that version and none of the commits made on the deleted branch.
- With the reference file taken away and `tree/<name>/` left, `branches.list()` no longer names the branch, and putting
  the file back restores it with its commits. A reader that knows the name still opens it meanwhile.
- A hidden branch's reference no longer holds the version it was cut from: after the parent was overwritten and
  `cleanup_old_versions` ran on it, the restored branch failed `Not found` on a data file. With a tag on that version
  the cleanup removed nothing the branch reads (the guide: "To remove a version that has been tagged, you must first
  delete the tag"; every reclaim the catalog and maintenance run passes `error_if_tagged_old_versions=False`).
- With `a`'s reference file away, `create_branch("a/b")` succeeds, and a later `branches.delete("a")` leaves `tree/a/`
  behind, since Lance keeps a directory another branch lives in.

Lakekeeper (read at 4e05f79) keeps no trash for refs; in Iceberg removing a branch removes only the ref, and its
snapshots stay until expiry, so the head is recoverable there until then.

## Decision

1. **Where the table's warehouse keeps what is dropped for a while, a branch delete hides the branch.** The catalog
   files a record (`lakehouse.records.branch_trash`, `_branch_trash/<table uuid>/<sha256 of the name>.json`) holding
   the reference file's own text, tags the version the branch was cut from (`rask.trash.<digest>`, a tag no door writes
   or deletes for a caller), and only then deletes the reference file. A branch is hidden once its record stands and its
   reference file is gone; a delete that died before that leaves a branch that stands. With no grace period the delete
   destroys the branch as before. A hiding delete runs inside the table's branch hold, which excludes a branch create,
   a restore, another such delete and every reclaim.
2. **A hidden branch is a missing one at every door, and its directory is still held.** Opening it answers 404 code 22.
   A create under its name, around it or inside it answers 409, as it would for a listed branch.
3. **`POST /management/v1/table/{id}/branches/restore`** (the delete's rung, `can_drop`) puts the reference file back,
   releases the tag and ends the record, for the branch named and each hidden branch inside it. It is refused once the
   deadline has passed. The door is named `restore` because a path ending in `undrop` is the trash doors'.
4. **The `catalog-branch-trash` timer deletes each hidden branch past its deadline for good** with Lance's own branch
   delete, then the tag and the record (`catalog.branchTrash.periodSeconds`). The record is marked first, because that
   delete needs the reference file back.
5. **An erasure leaves no hidden branch, and none is made while one is in flight.** Recording an erasure deletes every
   hidden branch of the table for good, whatever its deadline: its bytes may hold the subject, and no ref names them for
   the job or the verification to judge. From the record until the release of its pins has run, a branch delete on that
   table destroys the branch and no restore is served; the delete asks inside its hold. The purge at the record is best
   effort, after the obligation's trail is written, and an admitted verification purges again before it judges.
6. **A deregister deletes the table's hidden branches for good** once the table is detached and before its records
   end, since it leaves the bytes and nothing would end the directory or the tag afterwards.

## Consequences

- A deleted branch comes back with every commit made on it, inside the grace period.
- A hidden branch's bytes stay readable to a credential vended for the table until the deadline, as a table's old
  versions do; the catalog's own doors refuse the name.
- While a branch is hidden the catalog's tag keeps the version it was cut from. That version's files outlive the
  parent's cleanup, `version/delete` refuses it as it refuses any tagged version, and the branch it was cut from cannot
  be deleted: that delete answers 409 naming the hidden branch and its deadline.
- A control-root backup holds the records and not the tables' bytes. A record restored for a branch that has since been
  created again is a hide that never finished, and its branch stands.
- `POST /management/v1/table/{id}/branches/deleted/list` (a metadata read) lists the branches a restore can bring
  back, each with the version it was cut from and its deadline, and the console's refs panel lists them with a restore
  control (LH-603). It names who deleted none of them: that is in the audit trail.
- The timer is covered by the alerts every timer has (`LakehouseTimerStopped`, `LakehouseTimerFailing`); a branch one
  pass cannot delete is logged (`hidden_branch_not_purged`), counted on `catalog.branch_trash.failed` and tried again.
