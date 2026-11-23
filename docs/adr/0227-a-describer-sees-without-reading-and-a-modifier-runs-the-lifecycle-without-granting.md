# 0227. A describer sees without reading, and a modifier runs the lifecycle without granting (2026-11-23)

**Status:** accepted. Source: [[LH-570]] (#790), decided by the owner on 2026-11-22 through four questions ("All but
granting", "Also on a project", "No, grant both", "Show it"), and audited on 2026-11-23 against Lakekeeper's OpenFGA
model on `main` (`authz/openfga/v4.15`, `crates/authz-openfga`).

## Context

The data ladder was owner > writer > reader. Two needs had no rung:

- **Seeing without reading.** A table's metadata (`can_get_metadata`) and its place in a listing asked `reader`, which
  is data access. A data steward or a discovery page could not see that a table exists without being able to read its
  rows.
- **The lifecycle without grant authority.** Drop, protection, restore, rename, the refs, version deletes, retention,
  maintenance, erasure, labels and the data contract asked `owner`, which outside managed access also grants. A
  pipeline that retires or restores tables had to hold the authority to hand out access.

## Decision

The ladder is **owner > modifier > writer > reader > describer**, each rung holding the ones below it, and each a stored
relation with an `_effective` twin as every rung already is ([[LH-222]]).

1. **`describer` sees.** `can_get_metadata` reads it, on a table, a namespace and a warehouse, and listings filter on
   it. A describer gets the describe answer everyone gets, the storage path included, and no credential: the path is
   the bucket and the table's id, which the answer already carries, and the store refuses a request without a
   credential. Reading a row, a blob or a change feed, and being vended a credential, stay the reader's
   (`can_read_data`). A describer refused a table's data is told the refusal (403), since an absence would deny what
   its own listing shows it; a caller who cannot see the table is still told the absence. Two things a describer's
   sight does not carry, because each is data: the `predicate` of a delete or an update in the commit history, which
   names row values and is shown only to a caller who may read the rows; and standing as a run's input in lineage,
   which asks `can_read_data` as the catalog asks it of a commit's inputs, so seeing a table is no licence to record
   having read it.
2. **`modifier` runs the lifecycle: all but granting.** Everything an owner does to an object except giving or taking
   access and seeing who holds it: drop, undrop, rename, restore, branches and tags, version deletes, the retention
   policy, maintenance runs, erasure, protection, labels and the data contract, and on a namespace its delete,
   protection and labels. A modifier also writes and reads, because replacing or restoring a table already changes its
   rows. Moving a table into another namespace still asks grant authority beside the drop's rung, so a modifier moves
   none out.
3. **Grant authority is untouched.** `manage_grants`, `pass_grants` and `can_revoke_grant` are as they were. The two
   rungs are handed out as the others are: by a grant manager, or by a holder of the rung who also holds the grant
   option. The doors that disclose who holds access (`access/list`, `access/check`, `access/graph`) on a table and a
   namespace ask a verb of their own, `can_review_access` (`owner_effective or manage_grants`); they asked the drop's
   rung, which a modifier now holds. An owner keeps the review, inside a managed scope too, and a modifier never has
   it.
4. **Both are grantable on a project**, where one grant covers every warehouse of the project, those made later
   included. The project's admins hand them out: an admin already owns every warehouse of the project, so it gives
   nothing it does not hold, where a security admin holds no data and grants no data rung on a warehouse. Membership
   gives neither. A project-wide rung is not narrowed by a warehouse's managed access, which governs who may grant
   beneath it, not what a grant from above reaches.
5. **`classifier` and `tagger` do not include seeing.** A steward who labels and looks holds two grants, and the
   console offers the pair as one choice, so nobody holding one of them gains a right from this change. Classifying
   itself stays the classifier's and the owner's, not the modifier's: removing a classification reopens raw vending of
   the column, which is a change to who may read it.

No tuple is rewritten or added: an existing grant keeps its meaning.

## How it compares with Lakekeeper

Lakekeeper's ladder is `ownership > modify > write > select > describe`, with the same stored-and-effective split, the
same grant rule (`manage_grants`, or the rung with `pass_grants`), and upward visibility from any stored rung. This
decision is that ladder under rask's names. It differs in three ways, each chosen:

- rask's `modifier` covers doors Lakekeeper has no equivalent of (refs, version deletes, retention, erasure, labels,
  the contract).
- Lakekeeper lets anyone who may grant a rung list assignments; rask keeps that with `manage_grants`, so a modifier
  never sees who holds access.
- Lakekeeper offers five rungs on a project; rask offers these two, so nobody can be given project-wide read alone.

Lakekeeper's `create` is a rung of its own outside the ladder; in rask creating stays the writer's. Not changed here.

## Consequences

- "Hands out nothing" is about grants, not about bytes. A modifier who also may create in a namespace can deregister a
  table and register the same location again as a table it owns, as any writer can copy rows it reads into a table of
  its own. The new table has a new uuid and none of the old one's grants.
- The estate's projects can now carry a data rung; `project#member` still carries none.
- A new `can_*` on these types that means "sees" reads `describer_effective`, and one that means "lifecycle" reads
  `modifier_effective`; none reads a bare rung.
