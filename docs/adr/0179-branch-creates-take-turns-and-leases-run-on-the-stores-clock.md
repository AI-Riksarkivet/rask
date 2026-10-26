# 0179. Branch creates take turns, leases run on the store's clock, and edges name a branch's incarnation (2026-10-26)

**Status:** accepted. Source: LH-413 (#438), admitted by the owner on 2026-10-20 and settled with the owner on 2026-10-26
through the multi-question tool, after the `lance_docs/` check the owner asked for. Amends ADR 0176.

## Context

- **Nesting names.** The create door refuses a branch name that nests with another (LH-203), judging the table's branch
  listing. Lance lets `/` into a branch name, joins the name onto `tree/` verbatim and keeps each ref in its own file,
  `_refs/branches/<name>.json` with `/` encoded (`lance_docs/lance/format/table/branch_tag.md` § Branch Name, § Branch
  Metadata Path, § Branch Dataset Layout), and specifies no arbitration of concurrent ref writes
  (`lance_docs/lance/guide/tags_and_branches.md`; `CreateTableBranch` in `lance_docs/ns_catalog/spec.yaml` defines only a
  409). So `a` and `a/b` are two keys no conditional write on a ref can make collide, and two creates each judging a
  listing the other had not yet changed both landed.
- **Clocks.** A location claim's 15-minute lease (ADR 0172) and a history fence's 15-minute hold (ADR 0176) were stamped
  with the writing pod's `datetime.now` and judged against the reading pod's, so either lapsed early or late by the skew
  between the two pods.
- **Edges.** `provenance$edges` named a branch by its ref alone. A branch deleted and created again under one name
  restarts its numbering, so `(table, ref, version)` named two commits, while the feed already carried the incarnation.

## Decision

1. **Branch creates on one table take turns.** The history fence gains a kind, `branch`, that excludes its own kind and
   reclaims and shares with tag writes. The create door and the erasure release's branch re-creation hold it around the
   listing, the nesting check and the create; a concurrent create answers 409 code 14. The store's conditional write on
   the fence record is the arbiter, so there is no second copy of the branch list.
2. **Every lease and hold is timed on the object store's clock.** Each stamp is the `Date` of the read the write
   follows (`service_kit.lakehouse.records.StoreRead.at`; RFC 9110 §6.6.1 has every origin server with a clock send
   one), and each is judged against the `Date` of the read that finds it. A local root uses the local clock, and a store
   that sends no `Date` is logged and timed on the pod's clock.
3. **An edge names each branch's incarnation.** `provenance$edges` carries `in_branch_identifier` and
   `out_branch_identifier`, the last uuid of pylance's `branch_identifier` chain: the output's from the run's `lance`
   facet, and the input's from the input dataset's `lance` facet, which the catalog fills with the input branch's
   incarnation when it builds the event. The live edges table takes the columns by a rebuild of the estate from git.

## Consequences

- Two branch creates on one table never run at once; a crashed create holds the table's branch creates for at most
  15 minutes on the store's clock.
- A branch delete announces every branch it removed, nested ones first, each with its incarnation, including those it
  removed before a pin made since its check stopped it.
- An input branch's incarnation is the one standing when the event is built, read best-effort like the input's uuid; a
  branch recreated between a pipeline's read and its commit is named by the new incarnation.
- Pinned by `tests/integration/test_a_branch_cannot_nest_inside_another_branchs_files.py` (the create race and the
  `version/delete` refusal), `tests/integration/test_a_lease_is_timed_on_the_stores_clock.py` and
  `tests/integration/test_a_write_event_names_the_commit_it_made.py` (the delete announcements and the edge columns).
- The erasure release's own `lease_until` and the other dated records still use the pod's clock; neither is a lease
  this decision covers.
