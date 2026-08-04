# open_lakehouse_diff — rask catalog vs. Lakekeeper generic tables

What Lakekeeper's generic-table plane has that our catalog does not, and which of those gaps are
real work vs. recorded design decisions. Written 2026-08-04 against Lakekeeper ≤ v0.11 and
`services/catalog` at HEAD. Companion to `docs/audits/2026-07-26-routes-and-ia.md` (the console/UI
comparison, which verified several Lakekeeper claims first-hand) and `docs/COVERAGE.md` §"Lakekeeper
diff" (the recorded N/A-by-design list). This file is the CATALOG-API half of that comparison.

**Verification status** (stating what was checked, not implying more): the rask side is read from
source (`catalog/api/v1/endpoints/*`, `fga_deps.py`, the `rask-lance-catalog` skill). The Lakekeeper
side is from docs.lakekeeper.io excerpts, the Polaris generic-table spec (which Lakekeeper's API
mirrors), the fresha "Lakekeeper Generic Table API Design" write-up, and the claims the 2026-07-26
audit verified directly. The exact generic-table REST path strings and the drop-with-purge default
were NOT read from their OpenAPI (network policy blocks docs.lakekeeper.io and the repo from this
environment) — nothing below depends on a path string.

## 1. The one-sentence diff

A Lakekeeper generic table is a **format-agnostic pointer**: the catalog stores
`name + format + location + doc + properties`, treats the contents as opaque, and delegates all
interpretation to the engine — explicitly no commit coordination. Our catalog is a
**format-native Lance catalog**: 47/47 Lance Namespace operations, and the catalog itself mediates
versions, tags, branches, restore, column evolution, indices, transactions, and an Arrow-IPC data
plane. They solve overlapping problems from opposite ends: Lakekeeper knows *nothing* about the
inside of a table; we know *everything* — about exactly one format.

## 2. Where the designs converge (no action)

Independent convergence on the same shape, which is evidence the shape is right:

- **Hierarchy**: both are `project > warehouse > namespace (nested) > table`. Their warehouse is a
  storage profile; ours is ONE bucket with `top_ns → root_uri` bindings.
- **Authorization**: both OpenFGA, permissions inherited down the hierarchy.
- **Credential vending**: both vend short-lived table-scoped storage credentials from the same
  authz model that gates metadata. Ours is the richer contract (§4).
- **Drop vs. detach**: their `purge` flag on drop (metadata-only vs. data-deleting) is our
  `deregister` / `drop` split. Equivalent coverage, different shape.

## 3. The gap list (the point of this file)

Ordered by how real the gap is. "Decision" = recorded in `docs/COVERAGE.md` or an audit; a decision
entry is here because it carries a caveat worth re-checking, not to relitigate it.

### 3.1 Table-level drop-protection — GAP, cheap, the guard already exists

Lakekeeper: drop-protection "across tables, views and generic tables" (verified claim, 07-26 audit).
Us: `require_not_protected` (`catalog/api/fga_deps.py:499`) exists and is wired to **warehouses and
projects only**. A table (or namespace) has no `protected` flag; nothing stands between a fat-
fingered `POST /v1/table/{id}/drop` and byte deletion — and unlike Lakekeeper we have no undrop
behind it (3.2), so the blast radius of the missing flag is larger here than it is there.
Pickup: a `protected` marker on the table (schema metadata or the namespace `__manifest` row) +
`require_not_protected` at the drop/deregister/rename doors, `force=true` override, same contract as
the warehouse door (force overrides the flag and NOTHING else). Namespace delete should get the same
flag in the same change.

### 3.2 Soft-delete + time-bounded undrop — DECISION (N/A by design), with a caveat

Lakekeeper: dropped tabulars are marked deleted, recoverable until an expiration task runs; the
console has a first-class `deleted` tab. Generic tables get it automatically.
Us: `docs/COVERAGE.md` records this as N/A — "replaced by Lance version time-travel +
`restore_table`". The caveat that stance glosses: **version time-travel does not survive
`drop_table`** — restore rewinds a *live* table; drop deletes the bytes. So our replacement covers
the "bad write" recovery case, not the "bad drop" one. The N/A is scoped to the ephemeral
spin-up-per-workload model; a long-lived shared estate would want the real thing (a drop that
deregisters + moves the pointer to a trash namespace, with the maintenance sweep expiring it).
3.1 is the cheap mitigation that shrinks this caveat.

### 3.3 Catalog-stored `doc` / description — GAP, small

Their entity carries an optional `doc` (comment/description). We have no equivalent field on any
surface — the nearest thing is free-form schema metadata, which is invisible unless a caller knows
to describe with `load_detailed_metadata`. Pickup: a reserved schema-metadata key surfaced by
describe + the lakehouse Table detail, or a field on the `__manifest` row. No spec conflict either
way (the Lance spec's DescribeTable already returns open metadata).

### 3.4 Catalog-side table properties — PARTIAL, sharp edges

Theirs: free-form key/values in their own store (`generic_table_properties`), independent of the
data, no reserved keys. Ours: properties ARE Lance schema metadata — they live in the dataset, so
(a) `deregister` takes them with the bytes, (b) there is no properties-update endpoint (writes go
through the alter/data plane), and (c) reads need the describe backfill (`tables.py:241` — pylance
8.0.0 returns `metadata` empty, the #74 find). Being format-native this is arguably CORRECT — the
properties travel with the table — but the read/write ergonomics are a real gap vs. a
`set/remove properties` pair. Pickup if wanted: an `update_schema_metadata`-backed properties
endpoint (the lineage op name already exists in `lineage_emit.py:86`).

### 3.5 Multi-format registration — OUT OF SCOPE, by construction

The entire point of their generic table (`format: delta|lance|parquet|…`) and the entire non-point
of ours: this catalog implements the Lance Namespace spec, `register_table` attaches Lance datasets.
If the estate ever needs to govern a foreign-format table, that is a Lakekeeper-shaped sidecar
catalog, not a `format` column here — recorded so nobody tries to bolt it on.

### 3.6 Search / statistics without object-storage scans — GAP, priced-in, watch it

Their pitch (verified claim): normalized Postgres, "rich statistics served straight from the
catalog, with no object-storage scans". We made the opposite bet at P7a (no app DB; JSON+CAS
registries; the dataset is the state) and pay exactly where they don't: `list_all_tables` is an
N-blocking-call namespace walk (`tables.py:72`), `/{id}/stats` opens the dataset, and there is no
estate-wide search. The COVERAGE bar says this is fine at admin frequency. The tripwire recorded
here: the moment the lakehouse UI needs filtered/paginated listings at interactive frequency, that
is the "high-frequency filtered listings" clause from the P7a decision — a design decision to
revisit, not a cache to sneak in.

### 3.7 Per-object task visibility — GAP (UI-plane, already recorded)

Their warehouse/table detail has a `Tasks` tab (expiration queue etc.); ours is estate-global
(`admin/streams`/`dlq`/`events`) — you cannot ask "what is queued for THIS table". Recorded as
finding #5 in the 07-26 audit; it becomes catalog-relevant the moment 3.2's expiration sweep exists.
Roles/identities (findings #2–#3 there) stay in that audit — authz-plane, not catalog.

## 4. Where we are ahead (so this file isn't read as a deficit list)

- **Format-native operations** their generic table structurally cannot have (opaque contents):
  versions/time-travel, tags, branches, restore, column evolution, indices, transactions, stats
  computed from data, the Arrow-IPC data plane.
- **`declare_table`** — reserve an id before data exists, with provenance. No analogue.
- **Tiered credential vending**: `tier=read|write` mapped to separate FGA rungs, audited issuance,
  web-identity (AssumeRoleWithWebIdentity) exchange, `read_version` for optimistic commits, and the
  multi-base fallback to server-mediated IO. Polaris still lists generic-table vending as a
  limitation; Lakekeeper vends but has no tier/rung split on one endpoint.
- **The lineage moat** (COVERAGE.md): every table op emits OpenLineage; rename stitches
  dest←source; drop/deregister leave terminal markers.
- **Error contract**: RFC 9457 problem bodies carrying the spec's 22 numeric codes, one handler
  (`ns_errors.py`), vs. Iceberg-REST-style error JSON.

## 5. Suggested pickup order

1. **3.1 protection flag on table + namespace doors** — small, closes the sharpest safety gap.
2. **3.3 `doc`** — small, pure ergonomics, unblocks the Table detail description field.
3. **3.4 properties endpoint** — medium; decide reserved-key policy first.
4. **3.2 trash-namespace undrop** — only if/when the deployment model stops being
   spin-up-per-workload; re-read the COVERAGE.md caveat then.
5. **3.6** — do nothing until the UI hits the interactive-listing tripwire; then it's a design
   round, not a patch.
