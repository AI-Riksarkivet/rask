# Ingest, Lance-table sources, incremental runs, and tier movement — the decisions

The five decisions this plane rests on, what has actually landed against them, and the two policies
that are expressed in tuples rather than code.

**What is deliberately NOT here.** This file records DECISIONS and what they settled. Implementation
status lives in the root
`open_ingest_design.md`. Those items sat on this page for part of 2026-08-22 and were moved back the
same day: `docs/` asserts settled, so unfinished work placed here reads as decided regardless of the
prose around it. A DECISION can be settled while its disposition is not, and the two must not be
conflated.

The plan carried an evidence convention worth keeping: `path:line` means read from source,
`(measured <date>)` means observed against a running system, and `UNVERIFIED` means an inference.
They are not interchangeable, and several rulings below turn on which one a claim was.

---

## The five decisions, as they stand

| Question | Decision | State |
| --- | --- | --- |
| **1b** — an existing Lance table as a source | `lance-append` at fragment/row-range grain over UNGOVERNED locations; `lance-register` is the existing catalog door, not an ingest run; **no overwrite mode** | register door **shipped**; the `lance-append` kind **shipped** (`8e2da00a`, keyed on FRAGMENTS — offsets are not stable across versions) |
| **1c** — incremental / CDC | anti-join against bronze itself at enumerate (no new store), triggered by a cron at the outer edge | mechanism **shipped and bounded**; the cron **shipped** (`e629e2cc`) |
| **1d** — what must pre-exist | warehouse + namespace **yes**, table **no** | **shipped**, and the refusal is now pinned on both sides |
| **2** — "manual push to bronze only" | a **tuple-seeding policy**, not a code change | **ruled**; the policy is recorded below |
| **3 / 4** — annotator output, tier movement | annotations are DERIVED so silver is correct; readiness is the `published` tag | **ruled**; tenancy **fixed**. Which trigger drives the cascade is a separate decision, not this one — `open_ingest_design.md` §4 |

---

## What landed

* **1d, the table must NOT pre-exist.** A 403 from `describe` means "try create", not "give up" —
  before that fix every ingest run that ever succeeded did so against a table someone had already
  created. CREATE is the authoritative existence oracle rather than a read door, because it is gated
  on the PARENT's `can_create_table`, which is the estate's create-on-parent rule. Swapping `describe`
  for `exists` was considered and killed by measurement: `exists` 403s on an absent table too.
* **1d, the namespace refusal.** A missing warehouse-scoped namespace is refused naming the three
  admin doors, because ingest is a WRITER — having it provision the chain would make the data plane
  mint its own `project#admin` tuple. Both halves are pinned now
  (`services/ingest/tests/test_unit_dedupe_and_namespace_refusal.py`), including that the catalog
  still emits the prose ingest matches on: this is a cross-service contract held together by a string
  literal, and rewording it silently degrades the actionable refusal to a generic 400.
* **1c, the anti-join, and its ceiling.** The mechanism shipped; its stated cost — O(existing rows)
  per tick — had no bound until `RASK_INGEST_INCREMENTAL_MAX_ROWS`. That ceiling REFUSES and never
  samples, because truncating an anti-join inverts it: a partial "already have" set makes the run
  re-land rows bronze already holds.
* **3, tenancy.** The annotator published every tenant's labels into one bare `silver` namespace. Now
  derived through `warehouse_registry.namespace_for`, resolved at the DOOR before authorization so
  the gate checks the object the write lands in.
* **§6's one critical, `DWF-MGT-003`.** Nothing could stop a live ingest run;
  `POST /v1/ingests/{run_id}/terminate` closes it, bounded rather than instant and saying so.

---
---

## The two policies this plan was the only record of

**Manual push to bronze — a tuple-seeding policy, not a code change.** Nothing in `fga_deps.py` or the
model distinguishes a human principal from a service one; every subject is `user:`, and services are
`user:service-*`. A code-level tier guard would therefore have to INVENT that distinction, and an
invented one drifts from the tuples that actually decide. So the rule is expressed in tuples:

* grant human principals `writer` on `namespace:<proj>-bronze` — they push there and nowhere else;
* services keep `can_create_table` / `can_promote` on the silver and gold namespaces, which is what
  the movers already check as their own identity.

**Status, stated plainly: no seeding path in this repo grants that.** `scripts/seed_estate.py` drives
the real doors in hierarchy order but seeds no human bronze writer, so the policy is currently
unexercised rather than enforced or violated. Recording it here is the point — it was written down in
exactly one place, and that place was a file scheduled for deletion.

**A manual push uses `merge_insert`, not a raw insert** (corrected 2026-08-07):
`when_not_matched_insert_all()` is native insert-if-not-matched, so a re-push converges instead of
duplicating. The catalog's door accepts it; only the UI is missing.

---

## The cross-cutting rules worth keeping

* **Delta bookkeeping is data, not state.** §1c's anti-join, §4's `published` tag and the
  publication `{from_version, to_version}` delta are one ruling applied three times: the answer is
  computed from the artifact that must be correct anyway, so there is no second store to drift.
* **One irreversible operation gets exactly one door.** Overwrite is refused as an ingest mode and
  promotion is refused on the medallion producer, for the same reason — a second, weaker path to a
  governed operation is drift.
* **Create-on-parent is the authorization rule.** It is load-bearing wherever the door exists:
  `register`, the catalog's create doors, and the medallion promotion decision. Two doors the plan
  named (a watch/schedule door, a table-level promote door) do not exist, so the rule is not yet
  load-bearing at four sites as the plan claimed.
