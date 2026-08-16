# open: table maintenance — what is left

Re-opened 2026-08-16 after a day of fixes. Thirteen defects were closed and verified live (listed at
the bottom); this file is only what remains. Every item below carries `file:line` and a measurement,
because the recurring failure in this service has been prose that no longer matched the code.

**Delete this file when the list is empty.**

The one-line state of the estate: the sweep runs every 120s, reports `datasets 27, refused 12,
errors {}`, and **does no work at all** — because every dataset that needs compaction is refused, and
every dataset it maintains needs nothing. That is item T1+T2.

---

## T1 — the shallow-clone protection is INERT in production (data loss)

**What.** `base_refs.protected_roots` takes `storage_options` and never uses it
(`services/maintenance/src/maintenance/services/base_refs.py:119` signature, `:133`
`manifest_base_paths(lance.dataset(uri))` — AST: zero LOAD sites for the parameter). So every `s3://`
open fails for want of credentials and endpoint, and `BaseRefs.protected` is **empty on every tick**.
The maintenance pod carries no ambient `AWS_*` (only `MAINTENANCE_S3_*`), confirmed against
`chart/templates/maintenance.yaml`.

**Why it matters.** Both guards built on it are therefore no-ops: the #114 sweep refusal and the #128d
purge refusal. They exist to stop maintenance running on a dataset that some shallow clone depends on
— and that is a *measured* data-loss path, not a theoretical one: compact+cleanup on a base deleted 23
files including `_deletions/0-6-…arrow` and four `_indices/` files, after which the clone could not be
opened at all (`ArrowInvalid: … Not found: src_base/_deletions/…`). The sweep only logs
`maintenance_base_refs_incomplete` (`sweep.py:230`) and proceeds.

**How.** Thread `storage_options` into the open, and the #102 bounded session with it — `base_refs` is
the one maintenance open that does not pass `session=shared_lance_session()`, unlike `orphans.py:156`,
`purge.py:273`, `reconcile.py:294`, `optimize.py:169`. Then assert the pre-pass returns a NON-empty
protected set on an estate that has clones, so "inert" cannot recur silently.

**Watch out.** Fixing this turns the guards on for the first time. Expect datasets that were being
maintained to start refusing — that is the point, but it will move the numbers.

*Effort: small. Severity: highest — it is the only item on this list that can destroy data.*

---

## T2 — the flag-16 refusal is over-broad by exactly one operation

**What.** `describe_unsupported_flags` refuses manifest feature flag 16 (`base_paths` / shallow clone)
for the whole dataset, which blocks compaction **and** `optimize_indices` **and**
`cleanup_old_versions` together (`optimize.py:185`). 17 datasets are refused, and they are precisely
the ones with 4 fragments / 7 versions; the 9 maintained ones need nothing.

**Why the split is safe — measured on pylance 9.0.0, not reasoned.**

| operation | on a shallow clone | verdict |
| --- | --- | --- |
| `cleanup_old_versions` | root-scoped. In one call with dead fragments on BOTH sides it removed the 2 clone-owned files and left all 4 base-owned ones; `BASE_FILES_DELETED = []`. Holds for inherited `_deletions/` and `_indices/` too, across 6 cleanup shapes and 10 repeat cycles | **allow** |
| `optimize_indices` | zero work when the clone has no new rows; a delta index into the clone's own root when it does; base byte-identical | **allow** |
| `enable_auto_cleanup` | same safety (but see T3) | allow |
| `compact_files` | silently materialises a full private copy — a pristine clone went 1,072 → 108,199 bytes against a 119,693-byte base — defeating the feature. Never damages the base | **refuse** |

Lance's own spec contemplates exactly this: *"The clone can append new data… without affecting the
source dataset. Only the manifest and new data files are stored in the clone location"*
(`lance_docs/file_format.md:3186-3190`).

**How.** Do **not** widen `SUPPORTED` in
`packages/service-kit/src/service_kit/lakehouse/features.py:72` — it is also consumed by
`orphans.py:259` and `catalog/services/maintenance.py:31`, and widening changes both silently. Add a
second, narrower mask beside it (`SUPPORTED_FOR_GC = SUPPORTED | FLAG_BASE_PATHS`) plus
`describe_gc_unsupported_flags`, leaving `describe_unsupported_flags` as the compaction gate. Correct
the module docstring at `features.py:10-14`, which currently asserts the blanket refusal is required.

**Order.** Do T1 first. Allowing cleanup on clones is safe *for the clone*; it is the base side that
needs the protection T1 restores.

*Effort: medium.*

---

## T3 — `auto_cleanup` makes the reclaimer a version producer, and overrides a legal hold

**What.** `enable_auto_cleanup` is `update_config`, a Lance transaction **even when the config is
byte-identical** — measured: version 1→2→3→4 over three calls on an unchanged config. `compact_one`
calls it unconditionally for any policied dataset, every 120s (`optimize.py:297`).

Worse, the `elif` chain silently overrides two other policy fields:

- `cleanup_enabled=False` — documented as *"keeps the ENTIRE version history: a tier under legal
  hold"* — is **unreachable** once `auto_cleanup_interval_commits` is set, so the dataset's own commit
  path deletes the versions the hold existed for.
- `retain_versions: N` alone substitutes a 14-day window (`optimize.py:306`).

**How.** Read the existing config and skip the write when it already matches; make the three policy
fields compose rather than shadow, with `cleanup_enabled=False` winning over everything.

*Effort: small. A legal-hold override is the kind of defect that is embarrassing in an audit.*

---

## T4 — a REFUSED dataset is stamped as successfully maintained

**What.** The cadence stamp gates on `result.error is None` (`sweep.py:337`), and a refusal carries
`error=None` by construction (`optimize.py:180,187,201`). So with any `compact_interval_hours` policy,
a **permanent** refusal is recorded as a transient `policy_interval` skip.

**Why.** The refusal disappears from the report for the whole interval — the estate looks maintained.

**How.** Gate the stamp on "work actually happened", not "nothing raised".

*Effort: small.*

---

## T5 — no failure metric and no alert (this is the real "durable work item" gap)

**What.** The earlier framing — "no durable work item, like Lakekeeper's `task_log`" — was wrong. A
durable per-dataset failure record **already exists**: the deterministic-run-id FAIL OpenLineage
event. What is missing is a *metric* and an *alert*. `core/metrics.py` defines seven instruments and
**not one counts a failure**; `chart/alerting/rules.yml` has six groups and no maintenance group; the
new Perses dashboard has four panels and no failure panel. vmalert evaluates PromQL, so a log line and
a trace span can never page.

**How.** Add a failure counter and three alert rules. **Do not** build a task table, actors, retries or
terminal states — the estate has no application DB by design, and the gap does not need one.

*Effort: small. Highest value-per-line on this list.*

---

## T6 — the medallion tiers can never be named, so they emit no lineage and no FAIL

**What.** `table_id_from_uri` cannot name `medallion/<tier>`, so those datasets emit nothing. The
mapping is not merely missing from the URI — it **does not exist**: the chart composes the URI from the
namespace alone (`medallion.yaml:310-311`) while the canonical id is a separate literal
(`bronze$events`, `gold$htr`, `:270/:272`). `medallion/bronze` is *both* `bronze$events` and
`bronze$pages`. Live shapes contradict each other (`medallion/bronze-pages` → `bronze$pages` vs
`medallion/bronze-media` → `bronze-media$objects`), and for a tenant the project qualification exists
only in the name, never in the path (`transform.py:247-250` vs `:303-304`).

**And the silence is accidental, not safe** — `table_id_from_uri` already fabricates `"base"` for
`medallion/models/trocr_base`.

**Cost.** Not only provenance: the FAIL emit is the estate's only per-dataset maintenance failure
surface, and a real sweep on 2026-08-16 failed on 11 datasets — every one invisible.

**How.** Stop deriving the name. Have the producer stamp `lineage.dataset_id` into the dataset's
schema metadata and have the sweep read it. Note the **backfill gap**: datasets already on disk carry
no such key.

*Effort: medium. Do T5 first — it closes the visibility half far more cheaply.*

---

## T7 — reporting holes (three, all "computed then dropped")

1. **`summarize()` drops `index_findings`, `auto_cleanup_configured`, `bytes_removed`**
   (`optimize.py:55,60,64` set at `:243,:270,:309`; `sweep.py:443-472` has no key for any). So #60's
   entire output — a `describe_indices` + `index_stats` pass **per index per tick**, the call that
   panics and needed two `BaseException` guards — never leaves the process except as a count in one
   warning.
2. **`truncated` is accumulated and never consumed** (`sweep.py:153,168`; AST: LOAD sites = `[168]`).
   A tick that never walked a prefix is indistinguishable from one that did. Its own docstring says
   the sweep counts it; the reconciler does (`reconcile.py:725-726`), the sweep does not.
3. **`index_columns` is unreachable by construction** — no caller supplies it and no policy field
   exists (`optimize.py:139` → `:270`), so the dropped-index check is dead code whose docstring
   promises *"a policy that names the columns it depends on gets a real answer"*.

*Effort: small each.*

---

## T8 — the purge can never certify, and the last two blockers are not switches

`orphaned_annotation_tasks` (3) and `unbound_namespaces` (5) both count into `report.total`
(`reconcile.py:650-653`), and `report_is_clean` refuses on any non-zero total (`purge.py:218-220`) —
but **no door in the product can clear either**. Decide deliberately: give them a door, exclude them
from the certification total, or accept that `trashPurge` stays off. Today it is off by default and
its own values comment says it should stay off, so this is a decision, not a bug.

---

## T9 — small correctness/config items

- **`MAINTENANCE_POLICY_ROOT` is rendered nowhere** (`config.py:92`; only `MAINTENANCE_CONTROL_ROOT`
  at `maintenance.yaml:165`). An operator following `values.yaml:982-984` moves the trash/warehouse
  reads and leaves the policy reads behind.
- **`reconcile()`'s `control_root` defaults to `resolved_policy_root`**, contradicting its own
  docstring (`reconcile.py:761` vs `:752-755`). Latent only because `routes.py` passes it explicitly.
- **`build_report(namespace_root=…)` is dead** — zero LOAD sites (`reconcile.py:590-597`).
- **Multi-base leak**: dead files in a non-root base are never reclaimed by anything (measured:
  `EXTRA_BASE_DELETED = []`).

---

## Suggested order

**T1 → T5 → T3 → T4 → T2 → T7 → T6 → T9 → T8.**

T1 first because it is the only data-loss item. T5 next because it is the cheapest way to stop the
service failing silently — and it is what makes every later change observable. T2 before T6 because
T2 is what actually makes the sweep do work.

---

## Closed 2026-08-16 (do not re-open)

Tier sizing dead for every governed tier (3 URI layouts, tier at opposite ends) · the sweep's summary
reaching no log sink (`obs.py` still named `compaction`) · maintenance policy leaked on drop and lost
on rename · the reconciler refusing to resolve its FGA store when unpinned (3 categories permanently
UNAVAILABLE) · GreptimeDB OOMKilled ×8 (the estate's only telemetry sink) · the chart's own
observability bucket reported as drift forever · the ray-batch e2e leaking 32 datasets into the
governed bucket (45 → 13) · lock/replica coupling left unguarded · no maintenance dashboard ·
`lineageEmit` and `orphanScan` off · 9 ghost governance tuples · `k3s-pins.sh` destroying the pins its
own guard refused to overwrite.
