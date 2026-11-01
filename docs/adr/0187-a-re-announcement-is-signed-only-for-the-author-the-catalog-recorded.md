# 0187. A re-announcement is signed only for the author the catalog recorded authorizing the commit (2026-11-01)

**Status:** accepted. Source: LH-550 (#741), as the owner widened it on 2026-11-01 while LH-551 (#742) was worked (its
close comment and the map #478); the owner chose, the same day and through the multi-question tool, how the writes Lance
commits by itself record their author (#741's comments).

## Context

ADR 0177 has every commit name its author: a catalog door stamps `rask.author`, the caller it verified, in the
transaction properties; `version/create` refuses a stock commit naming anyone but its caller and records the caller of
one naming nobody; and the commit reconciler re-announces a lost announcement for that author. A transaction property
proves nothing about a version no catalog door made, though. Any writer that can put a final manifest (a maintain-tier
or store-root credential) can stamp any `rask.author`, and a registered table's history was written outside the catalog
altogether (LH-550's register marks). So lineage (LH-551) records the catalog's own announcements on its signature but
checked the reconciler's, marked `rask_lance.reannounced`, as any producer's: a lost announcement re-announced after its
author's grant was revoked stayed refused.

Lakekeeper needs no such record. An Iceberg commit is a transaction in its own database, which knows who committed, and
its events are fire-and-forget (ADR 0177). A Lance commit is a put-if-not-exists on the object store
(`lance_docs/lance/format/table/transaction.md` § Commit Protocol), so the catalog keeps the record itself.

Measured on pylance 13.0.0: `LanceDataset.commit(detached=True)` and `dataclasses.replace` keep a transaction's uuid;
`write_dataset` and `LanceDataset.insert` take none and mint one inside the commit; a stock client chooses its own and
keeps it when it stages the commit again on a later version. A transaction committed straight to the store keeps any
uuid it is given, another commit's or one that is no uuid at all: the format names the uuid the transaction's identity
(`lance_docs/lance/format/table/transaction.md` § Transaction Types) and the store arbitrates only the manifest's path.

## Decision

- **Every commit of an operation the doors announce records the caller it was authorized for**, at
  `_commit_authors/<table uuid>/<transaction uuid>/<version>.json` (`service_kit.lakehouse.commit_records`), naming the
  ref it was made on (the branch and its incarnation, `""` for main). The emitter's own rule decides which operations
  those are (`provenance_namespace.announcer`): the provenance namespace's row writes keep no record, since the feed holds
  no row for them. A record is written once: one held for another caller or another ref refuses `version/create` 400 code
  13, and the same caller on the same ref again is a retry, which leaves the record as it was. `version/create` refuses
  400 a staged commit naming no transaction, or one whose uuid is not canonical (lower-case hyphenated hex, as Lance mints
  it), since the record is keyed by it.
  - The eleven detached doors (`dataplane.commit_stamped`) and `version/create` record before the finalize, and their
    record also names the SHA-256 of the manifest the catalog judged and finalizes, which the backend lands unchanged.
    An attempt whose finalize did not land deletes its record; at `version/create`, whose transaction a retry shares,
    only the request that created the record does, and only when the version it was staged for does not hold that very
    manifest (a finalize whose answer was lost landed it).
  - The create door's write and an unkeyed insert or overwrite, which Lance commits in one call, record just after the
    commit lands, under the transaction their files are recorded under (`commit_authors.record_landed`, owner
    2026-11-01), naming no manifest: the record is read off the version itself. A failure is logged
    `commit_author_unrecorded` at ERROR and counted on `catalog.commit_authors.unrecorded`, and the door still answers.
- **The reconciler signs a re-announcement only for the author recorded for the transaction a version holds, at that
  version of that ref, and, where the record names a manifest, only when that manifest is the one committed there**,
  never for the `rask.author` the version carries. Where the catalog authenticates its callers, any other version of an
  announced operation, and a version whose transaction cannot be read, is counted unauthored and never announced; where
  it authenticates nobody, the first is announced naming no author and the second, whose operation nothing names, is
  passed over. A record is forgotten once its version is on the feed, past the horizon or counted unauthored, before the
  ref's mark moves past it: a delete the store refuses holds the mark below that version. A version the catalog finds
  under both manifest naming schemes matches no record.
- **Lineage records a catalog-signed re-announcement on that signature**, as it records the original, and
  `rask_lance.reannounced` is gone.

## Consequences

- A lost announcement whose author's grant was revoked since is recorded when the reconciler re-announces it.
- A version committed past every door is never announced for anyone, whatever `rask.author` it stamps, whichever
  transaction's uuid it carries, and whichever version it lands at: a writer that reads an in-flight commit's uuid off
  its staged transaction and lands that version first lands other bytes, which match the record nowhere. It pages as
  unauthored.
- A crash or a store failure between a write Lance committed and its record leaves that version unrecorded. If its own
  announcement was also lost, it pages as unauthored rather than being re-announced, and the `commit_author_unrecorded`
  log names it. The reconcile grace (10 minutes by default) is far longer than the record takes to land.
- A record can outlive an attempt that did not land (its delete refused, the catalog stopped first, the request
  cancelled): it names a manifest only that attempt's judged bytes match, and landing those bytes is landing the commit
  the catalog judged, for the caller it authorized. A retry of a commit sharing its transaction while the first attempt
  is in flight keeps the first attempt's record: if the retry lands other bytes and its announcement is lost too, it
  pages as unauthored, and the catalog's audit log holds a `commit_version` success for it.
- A detached door's commit whose finalize was refused although its put landed keeps its record: answered as a conflict,
  the door answers that version as its commit rather than applying the change again on it, and when the store does not
  answer whether it landed, the door refuses 503, its outcome unknown.
- Every commit of an announced operation costs one conditional write on the control root before it lands, a detached
  commit's lost race one read of the version's manifest, and the reconciler, for each version missing from the feed, one
  read of its record and one of its manifest, and one delete for each version it confirms; a version first read past
  the horizon has its transaction read off the manifest the pass opens anyway. The provenance namespace's row writes cost
  none of these.
- A stock client reusing another caller's transaction at the same version is refused.
