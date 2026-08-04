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

**Verification status — REVISED 2026-08-04 (#95).** The rask side is read from source. The
Lakekeeper side was originally from docs.lakekeeper.io excerpts, and that caveat turned out to
matter: a later audit **recovered the raw OpenAPI YAMLs** (both vendors render their API reference
via JavaScript, which is why the first pass could only quote prose) and refuted three claims this
file made — §1.1's drop-protection scope, §1.3's reclamation comparison, and §2.2's identity gap.
Each is corrected in place, marked `Corrected 2026-08-04 (#95)`.

The pattern in all three is the same and worth naming: **a claim sourced from a product's marketing
prose, then marked "(verified)".** The conclusions happened to be defensible; the evidence was not
what the label said it was. Where this file still says "(verified)" about a competitor, it means
prose unless it names an endpoint.

`open_table_maintenance.md` was corrected in the same round (#94) — most importantly its
"nothing reclaims yet", which was false.

## 1. Safety rails — the drop path (highest priority)

The multimodal estate's failure mode is worse than a tabular one's: a dropped gold table is not a
re-runnable SQL job, it is harvested page images and model-produced transcriptions.

### 1.1 Table/namespace drop-protection — CLOSED (#73)

> **Corrected 2026-08-04 (#95).** The "(verified)" on the sentence below was wrong, and the way it
> was wrong matters more than the fact: it quoted Lakekeeper's marketing LANDING PAGE, not their API.
> Their OSS spec carries **five** protection endpoints — table, view, generic table, **namespace and
> warehouse** — each with its own route. So this section reached "cheap, the guard already exists"
> from a sentence in a product blurb rather than from the API surface, and happened to land on the
> right answer. (The audit that found this recovered the raw OpenAPI YAMLs after noticing both
> vendors' doc sites render their API reference via JavaScript.)
>
> **The gap itself is now CLOSED** — #73 shipped `protected` on tables AND namespaces with
> `require_not_protected` at drop/deregister/rename, `force` turning the flag and nothing else.

Lakekeeper: drop-protection across tables, views, generic tables, namespaces and warehouses.

*As written:* `require_not_protected` was wired to warehouses and projects only, so nothing stood
between an authorized fat-fingered `POST /v1/table/{id}/drop` and byte deletion.

*As shipped (#73):* `protected` on tables AND namespaces, guarding drop/deregister/rename, with
`force=true` overriding the flag and NOTHING else — the FGA gate runs first and identically either
way. The marker is a control-root `_protection/` record rather than schema metadata, decided that
way so unprotect is never reachable through the properties door, toggling never creates a table
version, and the guard still answers for a corrupted dataset. It dies with the object, so a reused
id cannot inherit it.

### 1.2 Soft-delete + time-bounded undrop — CLOSED (#75), opt-in

Lakekeeper: dropped tabulars are marked deleted, recoverable until an expiration task runs; a
first-class `deleted` view. `docs/COVERAGE.md` records this as N/A by design — "replaced by Lance
version time-travel + `restore_table`". The caveat that stance glosses: **time-travel does not
survive `drop_table`** — restore rewinds a *live* table; drop deletes the bytes. The replacement
covers bad-write recovery, not bad-drop recovery, and the N/A was scoped to the ephemeral
spin-up-per-workload model.

*As shipped (#75):* with `LANCE_TRASH_GRACE_DAYS > 0` a drop DEREGISTERS and files a `_trash/`
record; `POST /v1/table/{id}/undrop` re-registers from it; `GET /v1/table/{id}/tasks` shows the
pending deadline (§2.4). It defaults to **0 / OFF** deliberately — a grace period changes what
`drop_table` MEANS for every caller, and that is not a default anyone should inherit silently. The
sweep REPORTS expired trash and deletes nothing.

COVERAGE.md's "soft-delete is N/A, time-travel replaces it" was corrected in the same change.

### 1.3 Reclamation — the ORPHAN pass is report-only; compaction and cleanup are not

> **Corrected 2026-08-04 (#95), twice over.**
>
> **The comparison was wrong.** `expire_snapshots` and `remove_orphan_files` appear ONLY in
> Lakekeeper's **PLUS** (commercial) spec — zero occurrences in the OSS one. Polaris executes no
> maintenance at all: *"Compaction, snapshot expiration, orphan cleanup … remain the user's
> responsibility"* (their own blog). So rask's report-only orphan pass is at **parity with OSS
> Lakekeeper and ahead of Polaris**, and the pressure this section applied — catch up with the
> field — was pressure toward a delete path that neither open-source peer actually has.
>
> **And "nothing reclaims" was wrong about us.** Compaction and version cleanup DELETE BYTES on
> every tick of the default chart; only the ORPHAN pass and trash expiry are report-only. See
> `open_table_maintenance.md` §2, corrected in the same round (#94).
>
> #79 stays on the list, but its justification changes completely: not "everyone else reclaims", but
> **our own correctness gap** — the detector is wrong on branches and multi-base datasets (§3 of the
> maintenance doc), and a reclaimer built on a detector that names live data is the one bug in this
> estate that is not recoverable.

Our orphan scan and reconciler REPORT and delete nothing: partially-failed writes and unpurged
buckets accumulate, `_transactions/*.txn` accumulate by design, and the orphan scan is off by
default because a wrong scan already once named 29 MB of live page images reclaimable. That caution
is correct. The gate for turning any reclaimer on stays as designed: the drift report runs clean
first — and #94's re-ordering puts **trash purge before orphan reclamation**, because a bounded
delete of a recorded path beats an inference from prefix subtraction.

## 2. Governance surface

### 2.1 Roles as a managed object — GAP (authz-plane)

Their model puts a reusable, nestable Project-level role between principal and grant (`/roles`,
verified); ours is tuple-level (`admin/access` Tuples/Graph/Check) plus per-object grants. FGA can
express roles today — the gap is a *managed surface* for them. COVERAGE.md's caveat applies
verbatim: a long-lived shared cluster wants runtime grant/role admin; N/A only while the model
stays spin-up-per-workload. That conditional is the same one as 1.2's — the two flip together.

### 2.2 Identities / user directory — mostly REFUTED; a narrower gap survives

> **Corrected 2026-08-04 (#95): the headline claim is REFUTED.** "No answer to who has access to
> this estate" is stale. `access_admin.py` ships `/v1/access/tuples` (GET/POST/DELETE), `/model`,
> `/check`, `/list-objects`, **`/list-users`** and **`/expand`**, plus `/simulate` — estate-admin
> gated, validated against the compiled model, with writes and disclosure reads audited.
> `list-users` and `expand` answer the auditor's question directly, and `/simulate` answers one no
> peer surfaces at all ("what WOULD this grant change").
>
> **Two narrower gaps survive, and they are the real content of this section:**
>
> 1. `role` is a first-class type in `model.fga` WITH nesting — and has no managed CRUD. The type
>    exists, the surface to administer it does not (this is §2.1, not a separate item).
> 2. There is no user DIRECTORY. A principal exists only as an OIDC `sub` appearing in some tuple,
>    so "list everyone who could touch this estate" is answerable and "list everyone the estate
>    knows about" is not. Whether that matters depends on whether an unGRANTED principal is a thing
>    the archive needs to name — decide that before building it.

They surface `/identities` + `/user-profile` while explicitly being "no Identity Provider" (IdP
stays external — same stance as our OIDC BFF).

### 2.3 Audit trail — PARTIAL, ours is event-shaped

We already audit the decisions that matter (write-tier credential issuance and denial,
`credentials.py`; control events for every lifecycle transition; OpenLineage provenance on every
table op including terminal drop/deregister markers — richer than anything their docs claim). What
we lack is the *queryable surface*: audit lands in logs and the event stream, not in an
"show me every credential issued for table X" view. Governance is only as good as its retrieval.

### 2.4 Per-object task visibility — SHIPPED (#75), read-only and single-id

Their warehouse/table detail has a `Tasks` tab; ours was estate-global (`admin/streams`/`dlq`/
`events`) — "what is queued for THIS table" was unanswerable. Required once 1.2's expiration sweep
exists: an undrop deadline the owner cannot see is not a safety feature.

**Shipped with #75:** `GET /v1/table/{id}/tasks`.

> **Residual, folded in here 2026-08-04 (#95) rather than filed separately.** Ours is READ-ONLY and
> reports exactly ONE thing: a pending trash deadline. Lakekeeper has `task/list`, `task/by-id` and
> `task/control` — cancel, request stop, run now — with queue and status filters. Ours cannot answer
> "what else is queued", and an owner who can SEE a deadline but not act on it is only half served.
>
> Also worth copying: **their undrop is PLURAL** (`undrop_tabulars` takes a set). Ours takes one id.
> The situation that needs undrop is a fat-fingered CASCADE, which by definition dropped many
> things — so the one shape rask cannot express is exactly the one the feature exists for.

## 3. Security hardening — recorded open items, promoted to this list

These are ours, already written down elsewhere, gathered here because "secure in production" is the
bar this file audits against:

- **Credential-level tenant isolation — attack test SHIPPED (#74), half of it still cannot run.**
  Byte-placement isolation was already proven (`test_warehouse_routing.py`,
  `test_warehouses_e2e.py`); the missing half was tenant B's vended credentials being REFUSED on
  bucket A. That test now exists and drives real buckets, both tiers — a mocked S3 proves nothing
  about a session policy. **Residual (#84):** the live half needs web-identity vending plus a second
  tenant admin to run in CI, so today it is a local-only proof.
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

> **Corrected 2026-08-04 (#95): the `doc`/description entry's "no equivalent" was STALE.**
> Dataset-level description + curated tags ship in `lineage/api/v1/endpoints/governance.py`. Two
> real gaps replace that claim, and both are sharper than the one it made:
>
> 1. They live on the **LINEAGE node, not the catalog object** — so a `deregister` + `register`
>    round-trip silently loses them. For a field whose purpose is to survive as documentation, being
>    attached to the wrong object is the whole bug.
> 2. There is nothing at **COLUMN** level. For a Swedish public archive that is the sekretess/GDPR
>    lever the estate has no way to express (#91) — governance, not ergonomics, so it does not
>    belong in this section at all.

- **Table description** — the write path exists on the lineage node; what is missing is binding it
  to the catalog object so it survives a re-register.
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
5. **2.1 roles** — when (if) the deployment model goes long-lived shared; same trigger as 1.2's
   COVERAGE caveat. (2.2 identities is mostly REFUTED — see the correction there.)

> **Re-prioritised 2026-08-04 (#95).** Items 1–3 shipped (#73/#74/#75). The archive-lens audit run
> in the same round found something that outranks everything left on this list, and it is not on it:
> **the HTR transcriptions — the archive's actual product — are written as raw ALTO XML to plain S3
> keys, outside the lakehouse entirely** (#88). No Lance table, no catalog registration, no lineage,
> no FGA. So every safety rail this document argues for currently protects THUMBNAILS, while the
> data a restricted fond is restricted about sits outside the boundary. Nothing else here changes
> that ordering.
>
> Ranked behind it, from the same audit: #90 (page images and objects served with no per-caller
> authorization), #91 (column-level classification), #92 (fixity digest over the archival master).
6. **4** — nothing until the interactive-listing tripwire fires; then a design round.
