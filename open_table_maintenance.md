# open: table maintenance — what is left

Re-opened 2026-08-16 after a day of fixes, worked down the same day. Seven of the nine items are now
closed and verified; what remains is below, and both remaining items are blocked on something other
than effort.

**Delete this file when the list is empty.**

---

## T6 — the medallion tiers still emit no lineage and no FAIL (producer half)

The READ half landed (`70675e5e`): a producer may declare `lineage.dataset_id` in the dataset's schema
metadata and the sweep will use it. The WRITE half — stamping that key in `services/medallion` — is
**not done**, because that service is being actively edited by another session (four modified files
and a commit within the hour) and landing it there now would collide.

Why a declared key rather than a derivation: the mapping does not exist. The chart composes those URIs
from the namespace alone while the canonical id is a separate literal, so `medallion/bronze` is BOTH
`bronze$events` and `bronze$pages`. The name must equal the OpenFGA object id, because delivery
re-checks `can_get_metadata` against `table:<output name>` — a wrong name counts every recipient
HIDDEN, which is worse than silence.

**Backfill gap:** datasets already on disk carry no key and keep the URI derivation.

*Blocked on: the medallion session finishing. Effort once free: small.*

---

## T8 — the purge can never certify, and the last two blockers are not switches

`orphaned_annotation_tasks` (3) and `unbound_namespaces` (5) both count into `report.total`
(`reconcile.py:650-653`), and `report_is_clean` refuses on any non-zero total (`purge.py:218-220`) —
but **no door in the product can clear either**. This needs a decision, not a fix: give them a door,
exclude them from the certification total, or accept that `trashPurge` stays off. It is off by default
today and its own values comment says it should stay off, so nothing is broken.

*Blocked on: an owner decision.*

---

## RESOLVED — the 9-day-old versions were correctly retained

`versions_removed` reading 0 was RIGHT, and both of my earlier explanations were wrong. The datasets
still holding 9-day-old versions are all in the TRASH: `bind86-bronze$converge-proof`,
`$replay-proof`, `$ui-converge`, `$freshtable-v1/v2`, `$createprobe` each have a `_trash/` record, so
the F6(d) exclusion freezes them until undrop or purge. A recoverable drop is not maintainable by
design — the sweep may not rewrite bytes someone can still restore.

Proof it was exclusion rather than failure: running `cleanup_old_versions(older_than=7d)` on one of
them by hand removed 5 versions and 7,567 bytes immediately (7 -> 2). Cleanup works; the sweep simply
must not call it there.

**A mistake of mine, recorded because it cost something.** That hand-run WAS the destructive call the
exclusion exists to prevent — I ran it to diagnose the count without first checking the dataset's
trash state. The dataset survives correctly (latest v7 intact, 7 rows, the `published` tag on v5
preserved, undrop unaffected), but time-travel to versions 1-4 is gone on an object someone could
still have restored. The lesson is the one this service keeps teaching: check the governance state
before touching bytes, including when you are only "looking".

## Also deferred (small, unblocked)

- **Multi-base leak**: dead files in a non-root base are reclaimed by nothing (measured:
  `EXTRA_BASE_DELETED = []`). Lance offers no API that reclaims them, so this needs an upstream
  answer rather than a local fix.

---

## Closed 2026-08-16

**The nine-item list, worked the same day:**

- **T1** `ce57bb67` — `protected_roots` dropped its credentials, so all 27 datasets were unreadable and
  both shallow-clone data-loss guards were inert since they shipped. Verified live 0 -> 1 protected,
  27 -> 0 unreadable.
- **T2** `5657a164` — the flag-16 refusal blocked all three operations; split per-operation after
  measuring that cleanup and index maintenance are root-scoped and only compaction materialises a copy.
- **T3** `4c8b9e31` — a legal hold was overridable by auto-cleanup, and the reclaimer committed a new
  version every 120s even when its config was unchanged.
- **T4** `18615b13` — a REFUSED dataset was stamped as freshly maintained, hiding a permanent condition
  behind a transient label for the whole cadence window.
- **T5** `15c74e2d` — no failure metric and no alert; added `compaction.datasets.failed` plus three
  rules. (The earlier "no durable work item like Lakekeeper's task_log" framing was wrong: a durable
  per-dataset failure record already existed.)
- **T7** `93021817` — the #60 index report and the truncated-prefix list were computed every tick and
  discarded.
- **T9** `e4e73b68` — `MAINTENANCE_POLICY_ROOT` rendered nowhere while values told operators to "set
  both together"; reconcile defaulted to the wrong root; a dead parameter.
- **index_columns** `2f85b270` — #60's dropped-index check was unreachable by construction: the
  argument existed, the check existed, and no policy field could name the columns. Added to
  `PolicyRequest`, threaded through the sweep, OpenAPI + TS client regenerated.

**The first wave, earlier the same day:**

Tier sizing dead for every governed tier (3 URI layouts, tier at opposite ends) · the sweep's summary
reaching no log sink (`obs.py` still named `compaction`) · maintenance policy leaked on drop and lost
on rename · the reconciler refusing to resolve its FGA store when unpinned (3 categories permanently
UNAVAILABLE) · GreptimeDB OOMKilled ×8 (the estate's only telemetry sink) · the chart's own
observability bucket reported as drift forever · the ray-batch e2e leaking 32 datasets into the
governed bucket (45 → 13) · lock/replica coupling left unguarded · no maintenance dashboard ·
`lineageEmit` and `orphanScan` off · 9 ghost governance tuples · `k3s-pins.sh` destroying the pins its
own guard refused to overwrite.
