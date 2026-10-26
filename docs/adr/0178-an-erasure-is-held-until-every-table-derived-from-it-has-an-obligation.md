# 0178. An erasure is held until every table derived from its table has an obligation of its own (2026-10-26)

**Status:** accepted. Source: LH-332 (#358), admitted by the owner on 2026-10-20 with its design (name the derived tables,
hold the obligation until each has an obligation of its own, open or released, announce the catalog's tag drops),
retitled to the derived tables on 2026-10-25, and settled with the owner on 2026-10-26 through the multi-question tool:
the tag drops are announced on the control lane, and the lineage lag is documented and parked as LH-541 (#727). Amends
ADR 0163 §3.

## Context

ADR 0163 makes an erasure an obligation on one table. A run that read that table wrote its rows into another, which keeps
them whatever the job does to the source, and nothing tied an erasure to those tables: the derived tiers kept the
subject's rows.

- Lineage's edge list (ADR 0158) records every table a successful run wrote from another, a governed table keyed by the
  uuid the catalog minted for it, and is never pruned. Lineage answers it on `GET /tables/{name}/derived`, which leaves
  out a derived table its caller may not see (`can_get_metadata`) and says only that its answer is incomplete.
- The source's predicate cannot judge a derived table: the derived table may keep the subject under columns of its own,
  or only as the `source_rowid` of the source's stable row ids (`lance_docs/lance/format/table/row_id_lineage.md`
  § Stable Row ID).
- Governance decisions read only what rask holds (ADR 0158), and the catalog already reads `provenance$events`
  in-process for the commit reconciler (ADR 0177).

## Decision

1. **The verification names every table derived from the erased one, by uuid.** `catalog.services.erasure_derived`
   locates `provenance$edges` through DescribeTable on the provenance namespace, opens it in-process, and walks it to its
   last table with lineage's own walk, shared in `service_kit.lakehouse.derived_tables`, so the verification and
   lineage's door never disagree about what was derived. Each derived table is a `derived:<uuid>` surface.
2. **Each holds the obligation open until an obligation following it is recorded on it** (`followed`), open or
   released. The link is the new obligation's own `follows`, naming the obligation it follows, and a chain of them
   counts: a table derived through another is that table's derived table too. An obligation on the derived table without
   that link counts for nothing, because one recorded there for another subject would release this one with its subject
   forgotten in that table. A `follows` naming no recorded obligation is refused 400.
3. **A derived table holds it while anything of it stands.** One no obligation can be recorded on any more holds it
   while its bytes stay (`held`): deregistered, its claim released and a dataset still at the location that claim
   records, which may hold the subject until the bytes are deleted. One dropped with its bytes, its claim released and no
   dataset at its location, does not (`gone`), and neither does one no claim records any more, another table having
   taken its location (`gone`, saying so). A recoverably dropped table keeps its claim, its bytes and its undrop, and
   holds the obligation like a live one. The claims are read whole, since a claim that cannot be read may be the table's
   own.
4. **A reader is shown a derived table only while they may see it** (`can_get_metadata`, the rung lineage's door asks):
   its uuid, and in its detail the name its newest edge recorded. The rest are left out of the report and of the
   obligation that reader is shown, and those holding the obligation open are counted in one `derived` surface. This is
   how the design keeps zero trust unchanged (owner, 2026-10-20). Lineage's door and its events feed hide a table's identity from a caller without
   a grant on it, and Lakekeeper hides an object's existence from one (`crates/lakekeeper/src/service/authz/namespace.rs`
   at `907a2ef0`); the erasure doors are not the place that discloses it.
5. **No edge list, nothing to follow.** A catalog keeping no provenance namespace (the code default; the chart names one
   whenever it deploys lineage), or one whose lineage has recorded no edge yet, names no derived table. An edge list or a
   claim that cannot be read keeps the obligation open as a failed `derived` surface.
6. **The verification's own tag drops are announced on the control lane** (owner, 2026-10-26). Each tag it drops is a
   `table_tag_deleted` control event, untargeted, naming the tag, the ref it pinned and the obligation, as the tag door
   and the release tick (ADR 0138) announce theirs, and the report lists them in `untagged`. A tag delete makes no version
   (`lance_docs/lance/format/table/branch_tag.md` § Tag Storage), so it has no commit-feed row to be on lineage.

## Considered options

- **Ask lineage's derived door**: its answer is filtered by the asker's grants, so the verification would see only what
  its caller may see, and an erasure job's identity sees no other project's tables; the verification would also wait on
  lineage being up. Rejected for the in-process read ADR 0158 and ADR 0177 already rely on.
- **Count any obligation on the derived table, or any recorded after the source's**: either lets an obligation for
  another subject release this one. Rejected by the owner's "it must never be forgotten" (2026-10-20).
- **Name every derived table to every maintainer of the source**: discloses tables they hold no grant on, which lineage's
  own door does not. Rejected.
- **Judge a derived table with the source's predicate, or follow `source_rowid` from the catalog**: the predicate names
  the source's columns, and a derived table's own obligation carries a predicate of its own (owner, 2026-10-20).

## Consequences

- Before a source's obligation is released, whoever may erase each derived table records an obligation on it with
  `follows`. The source's report names each one still owed by uuid, and lineage's door names it to whoever may see it.
- Lineage records a derived table's edges when it consumes the catalog's announcement of that table's write, so a table
  written from the source while lineage is behind, or moments before a verification, is not named by it, and a release
  is final. No in-process signal says lineage has caught up: the catalog's outbox drops an event once the bus takes it,
  not once lineage records it. LH-541 (#727, parked) asks for a horizon a verification could wait on.
- A deregistered derived table registered again at its location becomes a table of a new uuid that no edge links to the
  source, and its claim leaves the old uuid named by no claim, so it reads as `gone`.
- Edges an outside producer keyed by a name (a governed table it named without its uuid, admitted only with authorization
  off, or a name the table held before a rename) are walked only from the source's current name, as lineage's door walks
  them; a name-keyed derived table holds the obligation for good, since no obligation can be tied to a name.
- The walk reads the edge list once per hop, every edge it holds about the frontier, on each verification.
