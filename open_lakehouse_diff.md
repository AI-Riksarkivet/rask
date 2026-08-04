# open_lakehouse_diff — what a production-grade governed lakehouse still needs

Lakekeeper used as a **production-governance checklist** against our Lance-native multimodal
catalog: which safety, governance and security features their tabular plane has that ours does not,
plus the estate's own recorded production-safety open items that belong on the same list. Written
2026-08-04 against Lakekeeper ≤ v0.11 and `services/catalog` at HEAD. Companion to
`docs/audits/2026-07-26-routes-and-ia.md` (console/UI comparison, verified first-hand) and
`docs/COVERAGE.md` §"Lakekeeper diff".

**Non-goal, explicitly:** format-agnosticism. Lakekeeper's generic table exists to register foreign
formats as opaque pointers; we are building the opposite — a format-NATIVE multimodal lakehouse
(blobs, vectors, page images as first-class Lance data, catalog-mediated versions/branches/indices/
data plane). If the estate ever must govern a foreign-format table, that is a sidecar catalog, not
a `format` column here. The interesting part of Lakekeeper is not the pointer — it is the
production hardening they wrapped around every table-like object.

**Verification status:** the rask side is read from source. The Lakekeeper side is from
docs.lakekeeper.io excerpts and the claims the 07-26 audit verified directly (soft-delete/undrop,
drop-protection scope, catalog-served statistics, the console's route/tab sets); their OpenAPI was
not readable from this environment (network policy). Nothing below depends on a path string.

## 1. Safety rails — the drop path (highest priority)

The multimodal estate's failure mode is worse than a tabular one's: a dropped gold table is not a
re-runnable SQL job, it is harvested page images and model-produced transcriptions.

### 1.1 Table/namespace drop-protection — GAP, cheap, the guard already exists

Lakekeeper: drop-protection "across tables, views and generic tables" (verified). Us:
`require_not_protected` (`catalog/api/fga_deps.py:499`) exists and is wired to **warehouses and
projects only**. A table or namespace has no `protected` flag; nothing stands between an authorized
fat-fingered `POST /v1/table/{id}/drop` and byte deletion — and we have no undrop behind it (1.2),
so the missing flag is more dangerous here than it is there.
Pickup: a `protected` marker on the table + `require_not_protected` at the drop/deregister/rename
doors, `force=true` overriding the flag and NOTHING else (the warehouse door's exact contract, FGA
gate first and identically). Namespace delete gets the same flag in the same change.

### 1.2 Soft-delete + time-bounded undrop — recorded N/A, re-scoped here

Lakekeeper: dropped tabulars are marked deleted, recoverable until an expiration task runs; a
first-class `deleted` view. `docs/COVERAGE.md` records this as N/A by design — "replaced by Lance
version time-travel + `restore_table`". The caveat that stance glosses: **time-travel does not
survive `drop_table`** — restore rewinds a *live* table; drop deletes the bytes. The replacement
covers bad-write recovery, not bad-drop recovery, and the N/A was scoped to the ephemeral
spin-up-per-workload model. A long-lived production estate wants the real thing: drop = deregister
+ move the pointer to a trash namespace, the maintenance sweep expiring it on a clock — the sweep
service and its cron plumbing already exist (`services/maintenance`). 1.1 is the cheap mitigation
until then; this is the durable one.

### 1.3 Reclamation is report-only — OURS, not theirs, same list

Lakekeeper runs background expiration/purge tasks as a matter of course. Our reconciler and orphan
scan REPORT and delete nothing (`open_table_maintenance.md` §2): partially-failed writes and
unpurged buckets accumulate, `_transactions/*.txn` accumulate by design, and the orphan scan is off
by default because a wrong scan already once named 29 MB of live page images reclaimable. That
caution is correct — but "nothing reclaims" is not a production posture. The gate for turning any
reclaimer on stays as designed: the drift report runs clean first.

## 2. Governance surface

### 2.1 Roles as a managed object — GAP (authz-plane)

Their model puts a reusable, nestable Project-level role between principal and grant (`/roles`,
verified); ours is tuple-level (`admin/access` Tuples/Graph/Check) plus per-object grants. FGA can
express roles today — the gap is a *managed surface* for them. COVERAGE.md's caveat applies
verbatim: a long-lived shared cluster wants runtime grant/role admin; N/A only while the model
stays spin-up-per-workload. That conditional is the same one as 1.2's — the two flip together.

### 2.2 Identities / user directory — GAP (authz-plane)

They surface `/identities` + `/user-profile` while explicitly being "no Identity Provider" (IdP
stays external — same stance as our OIDC BFF). We have read-only chrome + frozen `/v1/me`: no
answer to "who has access to this estate, and what can they touch" without reading raw tuples.
For a governed archive that question is an auditor's, not a nicety.

### 2.3 Audit trail — PARTIAL, ours is event-shaped

We already audit the decisions that matter (write-tier credential issuance and denial,
`credentials.py`; control events for every lifecycle transition; OpenLineage provenance on every
table op including terminal drop/deregister markers — richer than anything their docs claim). What
we lack is the *queryable surface*: audit lands in logs and the event stream, not in an
"show me every credential issued for table X" view. Governance is only as good as its retrieval.

### 2.4 Per-object task visibility — GAP, becomes real with 1.2

Their warehouse/table detail has a `Tasks` tab; ours is estate-global (`admin/streams`/`dlq`/
`events`) — "what is queued for THIS table" is unanswerable. Low priority until 1.2's expiration
sweep exists, then required: an undrop deadline the owner cannot see is not a safety feature.

## 3. Security hardening — recorded open items, promoted to this list

These are ours, already written down elsewhere, gathered here because "secure in production" is the
bar this file audits against:

- **Credential-level tenant isolation is UNTESTED.** Byte-placement isolation is proven
  (`test_warehouse_routing.py`, `test_warehouses_e2e.py`); tenant B's vended credentials being
  refused on bucket A is not. For a multi-tenant estate this is the single most important untested
  claim in the security story — the STS session-policy scoping is only as real as the test that
  attacks it.
- **`warehouse_binding_cache` eviction is per-process.** Safe only because `replicas=1`. Scaling
  the catalog without wiring the control event to invalidation routes dropped namespaces at a
  deleted warehouse's bucket — a correctness hole that becomes a cross-tenant one the day buckets
  are recycled.
- **Kept, as designed (listed so nobody "fixes" them):** the no-existence-oracle rule on
  destructive doors (PermissionDenied ≡ NotFound — the door cannot enumerate ids); purge proving
  sole bucket ownership before cascade; identity→shape→parent→authz→conflict check order with
  guards BEFORE the native write; the 22-code RFC 9457 error contract.

## 4. Catalog-served listings and statistics — priced-in, with a tripwire

Their pitch (verified): normalized Postgres, statistics "with no object-storage scans". Our P7a bet
is the opposite (no app DB; the dataset is the state) and pays where they don't: `list_all_tables`
is an N-blocking-call namespace walk (`tables.py:72`), stats open the dataset, no estate-wide
search. Fine at admin frequency. The tripwire stands as recorded at P7a: interactive-frequency
filtered listings make this a design round (a real decision about a query store), never a cache
sneaked in as a patch.

## 5. Deliberately small gaps (ergonomics, not governance)

- **`doc`/description field** — no equivalent; nearest is schema metadata behind
  `load_detailed_metadata`. Small, unblocks the Table-detail description.
- **Properties write path** — properties ARE schema metadata (correct for a format-native catalog:
  they travel with the table, `deregister` included), but there is no update endpoint and reads
  need the describe backfill (`tables.py:241`, the #74 pylance find). An
  `update_schema_metadata`-backed endpoint closes it; decide reserved-key policy first.

## 6. Where we are already ahead of the checklist

Governance-relevant only: tiered credential vending (`tier=read|write` on separate FGA rungs,
audited issuance, web-identity exchange, multi-base fallback to server-mediated IO — Polaris still
lists generic-table vending as a limitation, Lakekeeper has no tier split); the lineage moat
(reconcile, column lineage, terminal drop markers, rename stitching dest←source); `declare_table`
provenance from before first byte; and the entire format-native surface — versions, tags, branches,
indices, the Arrow-IPC data plane — which for a MULTIMODAL estate is not a convenience but the
governance boundary itself: the catalog, not the client, mediates every write.

## 7. Pickup order

1. **1.1 protection flag** (table + namespace doors) — small, closes the sharpest safety gap now.
2. **3 first bullet: tenant-isolation attack test** — no new feature, just proof of the security
   claim already shipped.
3. **1.2 trash-namespace undrop** on the existing maintenance sweep — the durable drop-safety
   story; brings 2.4 with it.
4. **2.3 audit retrieval surface** — the event stream already carries the data.
5. **2.1/2.2 roles + identities** — when (if) the deployment model goes long-lived shared; same
   trigger as 1.2's COVERAGE caveat.
6. **4** — nothing until the interactive-listing tripwire fires; then a design round.
