# 0183. An erasure names every table a run declared it read, whatever lineage has recorded (2026-10-28)

**Status:** accepted. Source: LH-541 (#727), admitted by the owner on 2026-10-28 with its design first, and settled the
same day through the multi-question tool. Amends ADR 0178 §1 and replaces its consequence that a release cannot see a
write lineage has not recorded.

## Context

ADR 0178 names the tables derived from an erased one from lineage's edge list. Lineage records a derived table's edges
when it consumes the catalog's announcement of that table's write, so a table written while lineage is down or behind is
not on the list. A verification then releases the obligation, and a release is final. Reproduced on 2026-10-28 on a real
catalog and lineage: with lineage stopped, a table written from bronze was never named, and the obligation was released.

- The commit reconciler's per-ref mark (ADR 0177) cannot serve as a "lineage has caught up" signal. It passes versions
  older than its horizon without reading them, versions with no recorded author, and operations left unannounced. It
  scans nothing younger than its grace, a table that has only been declared writes no mark, and a write that declares
  no inputs leaves no edge whatever the marks say.
- A commit's run and inputs ride its `rask.*` transaction properties (ADR 0156 C9), but Lance documents a transaction only
  for commit retries and conflict detection (`lance_docs/lance/format/table/transaction.md` § Transaction Files). The
  inline copy is "an optional cache optimization" (§ Inline Transaction Sections), and cleanup removes old version
  metadata (`lance_docs/lance/guide/read_and_write.md` § Cleanup old versions). The format does not promise a
  transaction as a record.
- A declared input names a table by name, version and branch (`catalog.core.run_context.RunInput`). Lineage resolves the
  name to a uuid only when it consumes the announcement, so a name renamed or reused before then names another table, or
  none.
- The catalog cannot see what a pipeline read: a vended credential reads the store directly.
- Lakekeeper (`f65abcf`) and Polaris (`9f3f0ea`) have no erasure or derived-data mechanism to compare against.

## Decision

1. **Derived means declared.** A table is derived from another when a run writing it declared that table, or a table
   derived from it, among its inputs. An input at any version or on any branch counts, since the catalog cannot cheaply
   tell which versions held the subject, and naming one table too many only costs its owner a following obligation. A
   write that declares no inputs derives from nothing, and the obligation's report says that this lies outside the
   guarantee.
2. **A declaration is checked when it is committed.** Every catalog door and `version/create` resolve each declared input,
   under the committer's grant, to the uuid it names at that moment. An input that names no table, or one the committer
   may not read (`can_read_data`) at the version and branch it names, refuses the commit with 400 code 13 naming the input,
   before anything is finalized. A commit's provenance becomes a fact the catalog checked, and no name-keyed edge can come
   from a commit.
3. **The catalog keeps its own input record.** For each writing table, keyed by its uuid, the catalog records every input
   uuid that table's commits declared, with the time and version at which each was first declared. The record is written
   before the commit is finalized, beside the author `version/create` already records (ADR 0177). It stands while the
   table stands, untouched by Lance's cleanup.
4. **The verification names derived tables from the edge list and the input record together**, following chains through
   both, so a write is named the moment it commits, whether lineage is up or not. A commit's declarations count whoever made
   it: a missing author is the reconciler's concern, not the erasure's. A record that cannot be read keeps the obligation
   open as a failed `derived` surface, as ADR 0178 §5 already does for an unreadable edge list or claim.
5. **A release stays final, and is watched.** On each tick the commit reconciler checks new edges and new input records
   against every released obligation's source and its derived chain. A derived table whose write was declared before the
   release is a late derived table. It pages (`ErasureDerivedAfterRelease`, an alert rule in `deploy/alerting`) and is
   listed on the obligation for whoever may erase it to record an obligation of its own, shown only to a reader who may
   see it (ADR 0178 §4). Released obligations are watched for as long as they are kept, since the edge list is never
   pruned.

## Considered options

- **Wait for lineage**: refuse a release until every live table's retained versions are on lineage's feed. It makes
  every release wait on lineage being up, and an unauthored commit, which is never announced, would block it for good.
- **A settle window**: release only once the reconciler's grace has passed since the last commit. It narrows the gap and
  does not close it, since a lineage outage longer than the window still lets a table escape.
- **Read Lance transactions instead of a record**: rests on an artifact the format does not promise as a record, and a
  version cleaned up before it was read becomes a residual.
- **Resolve input names when the verification runs**: a rename between the write and the verification either blocks the
  release or names the wrong table.
- **Treat writes with no declared inputs as suspect**: every untagged write would block every erasure until someone
  attested it did not read the source.
- **Reopen a released obligation on a late derived table**: the release's tag drops cannot be undone, so "open" would
  mean "owed" for a source already released.

## Consequences

- A pipeline must name each input its run read as a table the committer may read, or its commit is refused. A run that
  declares no inputs is accepted, and an erasure says it cannot reach what that run wrote.
- Existing commits recorded no input uuids. The estate is pre-production, so they are test data, and there is no
  name-based read path for them.
- An outside producer's lineage event keyed by a name while authorization is off still holds an obligation for good (ADR
  0178). Such an event is made by lineage's door, not by a commit, so the commit-time check does not cover it.
- The verification reads the input record of every table once per verification, and the reconciler's tick reads the
  records changed since its last pass against the released obligations.
