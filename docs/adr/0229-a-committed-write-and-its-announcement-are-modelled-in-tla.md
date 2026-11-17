# 0229. A committed write and its announcement are modelled in TLA+ (2026-11-17)

**Status:** accepted; the second model (ADR 0221). It describes the Python catalog as it runs today and is the
upstream half of `specs/lineage/EventPath.tla`. Source: the owner, 2026-11-17: "So we have not modeled the whole
lakehouse?".

## Context

`EventPath.tla` proves no announcement is lost once its producer has staged or published it. It says nothing about
whether a committed write is announced at all, which is the catalog's side and the larger part of Phase 1's "events
correct". The catalog's answer is in three modules: a door commits and then announces
(`catalog.core.lineage_emit`), it records the author it authorized the commit for (`catalog.services.commit_authors`,
ADR 0187), and a reconciler reads each table's version log and announces again what the feed lacks
(`catalog.services.commit_reconcile`, LH-497).

## Decision

1. **`specs/catalog/CommitAnnounce.tla` models a version from its door to the feed:** the door's record, commit and
   announce in either of its two orders (record then commit for a detached commit; commit then record where Lance
   commits inside one call), a door that dies or fails at any step, the store's age for each version (young, past the
   grace, past the feed's retention), and the reconciler's two acts: announce a version again as its recorded author,
   and move its mark past a version. What lineage does with an announcement is one step here: it lands, or lineage
   keeps it off the feed.
2. **What it proves, for two versions of either shape with up to two failures (5,176 states):**
   - *Only committed versions are announced.*
   - *The reconciler announces again only for an author the catalog recorded.*
   - *The mark passes only what is accounted for:* on the feed, past the feed's retention, or counted as unauthored.
   - *An authored write is not given up:* the mark passes one that lineage accepts only when it is on the feed.
   - *A write with no author is counted* before the mark passes it, never aged out unseen.
   - *Every authored write lineage accepts reaches the feed* once failures stop.
3. **Three configs must fail, and do:**
   - `CommitAnnounceNoReconciler.cfg`: with no reconciler, a door that dies after its commit loses the announcement.
     The reconciler is what makes the version log the outbox.
   - `CommitAnnounceSlow.cfg`: a reconciler that reaches a version only after the feed's retention has passed it
     gives an authored write up. It runs with the hourly audit against a retention of days, and must stay well inside it.
   - `CommitAnnounceUnauthored.cfg`: **not every committed write reaches the feed.** A one-call door (the create
     door's write, an unkeyed insert) that dies between its commit and its record leaves a version with no recorded
     author. The reconciler counts it and pages; it announces it for nobody. This is a ruling, not a defect (owner,
     2026-11-01, `catalog.services.commit_authors`), and the model states it as a property that fails so the limit
     is visible where "events correct" is claimed.
4. **The model was shown to bite.** Five planted defects were each reported: announcing again with no recorded
   author, a mark that passes a version the feed lacks, a door that announces before it commits, a reconciler that
   never announces again, and an unauthored write passed without being counted.
5. **A property over a whole behaviour can be expected to fail too.** `scripts/tla_check.sh` takes
   `EXPECT VIOLATION temporal` beside an invariant's name.

## What the model assumes

- **A1.** Versions commit in order with one winner each: Lance's commit is a compare-and-swap in the object store.
- **A2.** An announcement and a second one of the same commit are one feed row: the run id is derived from the commit
  (`catalog.core.commit_ids`) and the feed is insert-only by key (`EventPath.tla` A1).
- **A3.** The reconciler reaches a version before the feed's retention passes it (`CommitAnnounceSlow.cfg`).

## Not modelled

Branches and their incarnations, a registered table's earlier versions, renames, operations no door announces, and
the announcements that are not of a commit (a grant, a drop: `catalog.services.announcement_intents`, which files its
announcement before its change and is a protocol of its own).

## Consequences

- "Every committed write is announced" is false as stated and true as: every write whose author the catalog recorded
  is announced, and every other is counted and paged for. The count is `catalog.commit_authors.unrecorded`.
- The Rust catalog keeps both door orders and the reconciler's rule as modelled, or changes this model first.
