# Lance Namespace Catalog — Interactive System Diagram

> ℹ️ **Point-in-time companion.** For authoritative current state defer to
> [`ARCHITECTURE.md`](ARCHITECTURE.md), [`TIER-GOVERNANCE.md`](TIER-GOVERNANCE.md),
> [`architecture/deployment.md`](architecture/deployment.md) and [`COVERAGE.md`](COVERAGE.md).

Companion to **[`system-diagram.html`](./system-diagram.html)** — a single-file, click-through
diagram of the catalog. Open it in a browser:

```bash
xdg-open docs/system-diagram.html      # Linux
# or just drag the file into any browser — no server, no build
```

This markdown is self-sufficient: you can read the whole system here without opening the HTML,
and vice-versa. For the prose architecture see [`ARCHITECTURE.md`](./ARCHITECTURE.md).

> **What the HTML adds:** pick a flow tab, press **Space** to play, and watch each request hop
> light up one wire + one node with its real payload on the side panel. Toggle the **data-plane
> mode** to see how the *same* operation changes shape between Mode B and STS vending. Click any node to jump to
> where it first appears; drag to re-layout; **T** = light/dark, **F** = fullscreen.

---

## The one idea

Three planes, three governance axes, **one identity**. Every table is `table:<id>` — and that
*same* string is the OpenFGA object (who may), the Lance dataset (what/when), and the dataset a
lineage feed row and edge name (how/who). The diagram is built around that single identity threading through all three.

| Plane | Question | Component in the diagram |
|---|---|---|
| **Control** | who may, where is it | **Catalog** (FastAPI/pylance) + **OIDC** + **OpenFGA** |
| **Data** | the bytes | **Object store** (S3-compatible; RustFS in development), reached by **Mode B** (server-mediated) or **STS vending** |
| **Provenance** | how/who did it change | **Lineage svc** (OpenLineage) → **commit feed + edge list** (Lance tables in the catalog's provenance namespace) → a bring-your-own graph backend *(catalog commit-events + job-emitted)* |

> ✅ **Provenance records the verified principal (audit `w8u4rc2tg` P0 #2/#3).** The catalog emits
> OpenLineage on table **create** with `author` = the verified token sub, recorded on the commit feed
> row, so "who created the table" is an audit fact read from `GET /events?table=<id>`. A pipeline's own lineage binds the author server-side on ingest, so it
> can't be forged. Emit covers the full write surface — create, insert, merge_insert, update, delete, and
> maintenance's runs — with the Lance-version facet on every measured write. The catalog's emitter is off
> unless `LANCE_LINEAGE_EMIT_ENABLED` is set (the chart sets it); ADR 0156 makes it mandatory on governed
> tiers (LH-472, #598).

---

## Modes — the data-plane shape (toggle, top-left)

Modes are **not** alternate flows; they're the deployment shape of the *data plane*. The same
flow runs in both — only the credential/byte-path changes.

| | **Mode B** *(server-mediated)* | **Web-identity vending** *(S3 direct, the default)* |
|---|---|---|
| Object store | S3-compatible (RustFS, AWS, Ceph RGW) | same — any STS-capable S3 (RustFS / Ceph / AWS) |
| Credential | **none leaves the catalog** | short-TTL, table-scoped **STS token** (`AssumeRoleWithWebIdentity` on the catalog's own ServiceAccount token) |
| Byte path | **server-mediated** (catalog reads on the client's behalf) | client reads storage **directly** with the vended token |
| OpenBao | not consulted per-request | not consulted: the catalog holds no store key (ADR 0170); a data base under its own credential reference reads that pair from the Dapr secret store |

The target is **S3-compatible storage** — RustFS is the development and CI backend (ADR 0168); AWS S3, Ceph RGW and
GCS-via-interop work the same way. The design is **vending-first** with a pluggable
`CredentialVendor` (`services/catalog/core/vending.py`, two modes: `WebIdentityVendor` (`web_identity`) and
`ModeBVendor` (`mode_b`)).
The toggle is the *credential-delivery* shape — both modes run on the **same** S3 storage.

> ℹ️ **Web identity works on RustFS** (and Ceph RGW, AWS; measured on RustFS 1.0.3, 2026-10-18,
> `docs/audits/2026-10-18/rustfs_web_identity_measure.py`): `WebIdentityVendor` sends the catalog's own projected
> ServiceAccount token, unsigned, to the store's STS `AssumeRoleWithWebIdentity` with an inline session policy scoped
> to the table's bucket/prefix → a short-TTL `{access_key, secret_key, session_token}`. The store grants the token the
> policy its subject names, and the session is the intersection of the two. For a backend with no STS, `ModeBVendor`
> keeps every credential inside the catalog.

> 🔑 **Who holds secrets (least-privilege).** Only the **catalog** and **lineage svc** consume OpenBao.
> A pipeline job (the user's, on their own compute) **never** reads OpenBao — it gets short-TTL scoped creds
> *from the catalog* and authenticates with **workload identity** (its own OIDC token), not a vault key.
> That is why only the catalog connects to OpenBao in the diagram — by design, not omission.

---

## Nodes

| Node | Role (color) | What it is |
|---|---|---|
| **Client** | user (mint) | LanceDB SDK / `lance-ray` / app — speaks the REST + Arrow API |
| **Pipeline job** | compute (magenta) | the user's pipeline moving data between tiers on their own compute (a catalog *client*, not an endpoint) |
| **OIDC IdP** | embed (amber) | Dex — issues JWTs; catalog verifies against JWKS (fail-closed) |
| **OpenBao** | seed (orange) | secrets · KV v2 — **catalog + lineage only**; jobs use workload identity (planned) |
| **Catalog** | orch (sky) | FastAPI over native `pylance` `DirectoryNamespace`; the control plane |
| **OpenFGA** | vector (violet) | authz (Postgres) — `can_*` actions, concentric owner⊇writer⊇reader, parent cascade |
| **Object store** | vector (violet) | the data plane — S3-compatible (RustFS in development); the mode toggle swaps the credential path (Mode B vs STS), not the backend |
| **Lineage svc** | orch (sky) | FastAPI, no database; ingests OpenLineage, serves the commit feed (`GET /events`) and what derives from a table (`GET /tables/{name}/derived`), forwards every event to a graph backend |
| **Provenance tables** | vector (violet) | `provenance$events` and `provenance$edges`: Lance tables in the catalog's provenance namespace, written through its `merge_insert` door as lineage's own principal |

---

## Flows (the tabs)

### 1. Create table — `POST /v1/table/{id}/create`
1. **Client → Catalog** — Arrow-IPC create with a Bearer JWT.
2. **Catalog → OIDC** — verify JWT against JWKS (cached); bad/absent token → 401.
3. **Catalog → OpenFGA** — `check can_create_table` on the **parent** namespace.
4. **Catalog → Object store** — create the Lance dataset location + record the table (version 1).
5. **Catalog → OpenFGA** — `grant_on_create`: seed `owner` grant + `parent` edge (inherits cascade).
6. **Catalog → Lineage** — emit OpenLineage with `author` = the **verified** sub, recorded on the commit feed row (the audit fact behind "who created this table"). **After the commit, never failing it**: the event is staged to the outbox before it is published, so a lineage outage never fails the 201 and the relay tick drains what was not delivered.
7. **Catalog → Client** — `201`; caller is the table's **owner** (⇒ writer ⇒ reader).

### 2. Read / query — `GET describe_table` (+ read)
1. **Client → Catalog** — `DescribeTable` with `vend_credentials=true` (the spec's own parameter, `lance_docs/ns_catalog/spec.yaml`) returns table-scoped `storage_options`; the rask extension `POST /management/v1/table/{id}/credentials?tier=read|write` vends a named tier. Which tier the spec doors vend is LH-229 (#149).
2. **Catalog → OIDC** — verify token.
3. **Catalog → OpenFGA** — `check can_read_data` (reader rung; cascades).
4. **Catalog → OpenBao** *(vending modes on S3)* — read the catalog's **own base key** (Dapr secret store) to mint the scoped token; **Mode B fetches nothing** (compute jobs never read OpenBao — see the Secrets note above).
5. **Catalog → Object store** — return location; **STS vending** also returns a short-TTL STS token, **Mode B** returns the location only.
6. **Client → Object store** — **S3:** read directly with the vended token. **Mode B:** the read instead goes through the catalog's Arrow-IPC query endpoint (no creds on the client).

### 3. Promote (medallion) — the user's pipeline, bronze → silver → gold
*The lakehouse runs no stage ([ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)): the pipeline is
the user's, on their own compute and orchestrator. The catalog announces every committed write on lineage, and a
pipeline reads those announcements from lineage's `GET /events` with its own token. Publishing a gold version is the
catalog's publish door, which runs the project's gate and moves the `published` tag
([TIER-GOVERNANCE.md](TIER-GOVERNANCE.md)).*
1. **Job → Catalog** — describe bronze (the job is a catalog client; it authenticates with **workload identity**, never OpenBao).
2. **Catalog → OpenFGA** — `check can_read_data` for `role:data_engineer#assignee` (role-as-subject).
3. **Job → Object store** — read only the **new bronze versions** — the version range *is* the change feed.
4. **Job → Catalog** — `merge_insert` silver with an `Idempotency-Key` (authorizes `can_write_data`; retry-safe).
5. **Job → Object store** — write the new silver dataset version (immutable; old versions stay for time-travel).
6. **Job → Lineage** — emit an OpenLineage run event: inputs=[bronze], outputs=[silver]. Ingest **binds the verified author** on the authenticated path (implemented; the open HTTP `/api/v1/lineage` guards forgery via `enforce_author`).
7. **Lineage → Catalog** — record the event as a `provenance$events` row and its table-level edges (bronze → silver) in `provenance$edges`, through the catalog's `merge_insert` door; a governed dataset is named by its bare catalog id.

### 4. Lineage query — `GET /tables/{name}/derived`
1. **Client → Lineage** — ask "what was built from bronze?" (how an erasure finds every derived table).
2. **Lineage → OIDC** — verify JWT (`RASK_OIDC_ENABLED`); no/invalid token → 401.
3. **Lineage → OpenFGA** — `check can_get_metadata` on the source (same right as describe-table); reuses the shared store, read-only.
4. **Lineage → Catalog** — read the edge list through the catalog's `query` door and walk it, nearest first, to at most 20 hops.
5. **Lineage → Client** — `200` with the derived tables the caller may see (`complete: false` when the walk stopped short). The full upstream/downstream graph is the bring-your-own backend's, which the forwarder feeds.

> **The governance gaps the diagram tracked (audit `w8u4rc2tg`) — now resolved:**
> 1. ✅ *Lineage query* reads are **authz-gated** (OIDC + `can_get_metadata`) with transitive-disclosure
>    filtering (P0 #1). Opt-in: `RASK_OIDC_ENABLED` + `RASK_FGA_ENABLED`.
> 2. ✅ Lineage **ingest** requires a verified token and **binds the author** to it, so provenance is not
>    forgeable (P0 #2); the catalog forwards the caller's bearer on emit.
> 3. ✅ The catalog **emits create-lineage** with the verified author — "who created the table" is an
>    audit fact on the commit feed (P0 #3). Insert/delete/merge-insert/update and
>    maintenance's runs all emit too — the write surface is fully covered.
>
> Tracked in [`docs/adr/`](adr/README.md). Run the whole loop: `make governance-chain` (or
> `dagger call governance-chain --demo` for the narrated [`governance_demo.py`](../scripts/governance_demo.py)).

---

## Workshop scenarios

- **"Show me security end-to-end"** — run *Create table*; pause on steps 2→3→5 (verify → authorize → seed-ownership). The cascade is why one create grants exactly the right future access.
- **"Two credential modes"** — open *Read / query*, step through once in **Mode B** (server-mediated), then flip to **STS vending** and step again. Same flow, same S3 storage, two credential paths.
- **"Where does lineage come from?"** — run *Promote*; the last two steps (emit → record) show the pipeline reporting its own run. Ingest **binds the verified author** (P0 #2), and the catalog announces every write the job commits.
- **"What's still open?"** — *Lineage query*, steps 2–3 are now the SHIPPED authz gate
  (`RASK_OIDC_ENABLED` + `RASK_FGA_ENABLED`, on in the chart); the remaining opens live in
  [`docs/adr/0007-7a-live-verification-residuals.md`](adr/0007-7a-live-verification-residuals.md) §7a (live-verification residuals) and
  [§9](adr/0008-9-feature-gaps-the-open-backlog.md) (feature gaps).
