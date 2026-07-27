# Lance Namespace Catalog — Interactive System Diagram

> ℹ️ **Point-in-time companion** (markers refreshed 2026-07-11 — insert/delete/compaction emit,
> read-side lineage authz, the OpenBao/Dapr secret store, and credential vending are all built +
> deployed). For authoritative current state defer to [`ARCHITECTURE.md`](ARCHITECTURE.md),
> [`DEPLOY.md`](DEPLOY.md), and [`COVERAGE.md`](COVERAGE.md).

Companion to **[`system-diagram.html`](./system-diagram.html)** — a single-file, click-through
diagram of the catalog. Open it in a browser:

```bash
xdg-open docs/system-diagram.html      # Linux
# or just drag the file into any browser — no server, no build
```

This markdown is self-sufficient: you can read the whole system here without opening the HTML,
and vice-versa. For the prose architecture see [`ARCHITECTURE.md`](./ARCHITECTURE.md); for the
gap register + Lakekeeper diff see [`SYSTEM-SKETCH.md`](./SYSTEM-SKETCH.md).

> **What the HTML adds:** pick a flow tab, press **Space** to play, and watch each request hop
> light up one wire + one node with its real payload on the side panel. Toggle the **data-plane
> mode** to see how the *same* operation changes shape between Mode B and STS vending. Click any node to jump to
> where it first appears; drag to re-layout; **T** = light/dark, **F** = fullscreen.

---

## The one idea

Three planes, three governance axes, **one identity**. Every table is `table:<id>` — and that
*same* string is the OpenFGA object (who may), the Lance dataset (what/when), and the lineage
graph node (how/who). The diagram is built around that single identity threading through all three.

| Plane | Question | Component in the diagram |
|---|---|---|
| **Control** | who may, where is it | **Catalog** (FastAPI/pylance) + **OIDC** + **OpenFGA** |
| **Data** | the bytes | **Object store** (MinIO / S3-compatible), reached by **Mode B** (server-mediated) or **STS vending** |
| **Provenance** | how/who did it change | **Lineage svc** (OpenLineage) → **AGE graph** *(catalog create-events + job-emitted)* |

> ✅ **Provenance records the verified principal (audit `w8u4rc2tg` P0 #2/#3).** The catalog emits
> OpenLineage on table **create** with `author` = the verified token sub — a
> `(:User)-[:CREATED]->(:Dataset)` edge, so "who created the table" is an audit fact queryable at
> `GET /datasets/{id}/creator`. Job-emitted lineage (promote/compaction) binds the author
> server-side on ingest, so it can't be forged. Emit now covers the full write surface — create,
> insert, merge_insert, update, delete, and the compaction sweeper — with the Lance-version facet
> on every measured write. (Emission is opt-in: `LANCE_LINEAGE_EMIT_ENABLED`.)

---

## Modes — the data-plane shape (toggle, top-left)

Modes are **not** alternate flows; they're the deployment shape of the *data plane*. The same
flow runs in both — only the credential/byte-path changes.

| | **Mode B** *(server-mediated, default)* | **STS vending** *(S3 direct, recommended)* |
|---|---|---|
| Object store | MinIO / S3-compatible (AWS, Ceph RGW, RustFS) | same — any STS-capable S3 (MinIO / Ceph / AWS) |
| Credential | **none leaves the catalog** | short-TTL, table-scoped **STS token** (`AssumeRole`) |
| Byte path | **server-mediated** (catalog reads on the client's behalf) | client reads storage **directly** with the vended token |
| OpenBao | not consulted per-request | catalog reads its base/role credential (Dapr secret store — built) to mint STS |

The target is **S3-compatible storage** — MinIO is the default test backend; AWS S3, Ceph RGW,
RustFS, GCS-via-interop all work the same way. The design is **vending-first** with a pluggable
`CredentialVendor` (`services/catalog/core/vending.py`, four modes: `StsVendor` /
`WebIdentityVendor` / `StaticPrefixVendor` / `ModeBVendor`).
The toggle is the *credential-delivery* shape — both modes run on the **same** S3 storage.

> ℹ️ **STS works on MinIO** (and Ceph RGW, AWS): `StsVendor` points boto3's STS client at the S3
> endpoint and calls `AssumeRole` with an inline session policy scoped to the table's bucket/prefix
> → a short-TTL `{access_key, secret_key, session_token}`. **RustFS** rejects plain `AssumeRole`
> but supports `AssumeRoleWithWebIdentity` — that's `WebIdentityVendor`
> (`vending.mode=web_identity`), the RustFS-native scoped-STS path, live-verified
> (`docs/DEPLOY.md`). For a backend with neither, `StaticPrefixVendor`/`ModeBVendor` remain.
> That's the point of pluggable vending. *(RustFS lifecycle e2e: `scripts/rustfs_e2e.sh`.)*

> 🔑 **Who holds secrets (least-privilege).** Only the **catalog** and **lineage svc** consume OpenBao.
> Compute jobs (**lance-ray**) **never** read OpenBao — they get short-TTL scoped creds *from the
> catalog* and authenticate with **workload identity** (KubeRay SA / OIDC token), not a vault key.
> That is why only the catalog connects to OpenBao in the diagram — by design, not omission.

---

## Nodes

| Node | Role (color) | What it is |
|---|---|---|
| **Client** | user (mint) | LanceDB SDK / `lance-ray` / app — speaks the REST + Arrow API |
| **lance-ray job** | compute (magenta) | medallion promotion + compaction on the KubeRay cluster (a catalog *client*, not an endpoint) |
| **OIDC IdP** | embed (amber) | Dex — issues JWTs; catalog verifies against JWKS (fail-closed) |
| **OpenBao** | seed (orange) | secrets · KV v2 — **catalog + lineage only**; jobs use workload identity (planned) |
| **Catalog** | orch (sky) | FastAPI over native `pylance` `DirectoryNamespace`; the control plane |
| **OpenFGA** | vector (violet) | authz (Postgres) — `can_*` actions, concentric owner⊇writer⊇reader, parent cascade |
| **Object store** | vector (violet) | the data plane — MinIO / S3-compatible; the mode toggle swaps the credential path (Mode B vs STS), not the backend |
| **Lineage svc** | orch (sky) | FastAPI; ingests OpenLineage, serves upstream/downstream/producers/graph |
| **AGE graph** | vector (violet) | Apache AGE (Postgres) — the provenance DAG, queried with openCypher |

---

## Flows (the tabs)

### 1. Create table — `POST /v1/table/{id}/create`
1. **Client → Catalog** — Arrow-IPC create with a Bearer JWT.
2. **Catalog → OIDC** — verify JWT against JWKS (cached); bad/absent token → 401.
3. **Catalog → OpenFGA** — `check can_create_table` on the **parent** namespace.
4. **Catalog → Object store** — create the Lance dataset location + record the table (version 1).
5. **Catalog → OpenFGA** — `grant_on_create`: seed `owner` grant + `parent` edge (inherits cascade).
6. **Catalog → Lineage** *(P0 #3, default OFF)* — emit OpenLineage with `author` = the **verified** sub → a `(:User)-[:CREATED]->(:Dataset)` edge (the audit fact behind `GET /datasets/{id}/creator`). **Awaited inline, best-effort** — errors are swallowed so a lineage outage never fails the 201, and the shipped B4 reconcile back-fills anything lost.
7. **Catalog → Client** — `201`; caller is the table's **owner** (⇒ writer ⇒ reader).

### 2. Read / query — `GET describe_table` (+ read)
1. **Client → Catalog** — describe; creds via the dedicated **`POST /v1/table/{id}/credentials?tier=read|write`** (vending is its own endpoint, not a describe param).
2. **Catalog → OIDC** — verify token.
3. **Catalog → OpenFGA** — `check can_read_data` (reader rung; cascades).
4. **Catalog → OpenBao** *(vending modes on S3)* — read the catalog's **own base key** (Dapr secret store) to mint the scoped token; **Mode B fetches nothing** (compute jobs never read OpenBao — see the Secrets note above).
5. **Catalog → Object store** — return location; **STS vending** also returns a short-TTL STS token, **Mode B** returns the location only.
6. **Client → Object store** — **S3:** read directly with the vended token. **Mode B:** the read instead goes through the catalog's Arrow-IPC query endpoint (no creds on the client).

### 3. Promote (medallion) — `lance-ray` producer + Dapr movers, bronze → silver → gold  *(built & deployed: the `lance-ray` producer fires the cascade and the event-driven movers promote each stage, emitting OpenLineage at every hop — see [`FLOW.md`](FLOW.md). The **distributed** lance-ray Ray Data job is the rask future; the in-process fake-Ray compute fills the same contract today.)*
1. **Job → Catalog** — describe bronze (the job is a catalog client; it authenticates with **workload identity**, never OpenBao).
2. **Catalog → OpenFGA** — `check can_read_data` for `role:data_engineer#assignee` (role-as-subject).
3. **Job → Object store** — read only the **new bronze versions** — the version range *is* the change feed.
4. **Job → Catalog** — `merge_insert` silver with an `Idempotency-Key` (authorizes `can_write_data`; retry-safe).
5. **Job → Object store** — write the new silver dataset version (immutable; old versions stay for time-travel).
6. **Job → Lineage** — emit an OpenLineage run event: inputs=[bronze], outputs=[silver]. Ingest **binds the verified author** on the authenticated path (implemented; the open HTTP `/api/v1/lineage` guards forgery via `enforce_author`).
7. **Lineage → AGE** — `MERGE` Run/Job/Dataset + `WROTE`/`DERIVED_FROM` edges; dataset name = `table:<id>`.

### 4. Lineage query — `GET /datasets/{id}/upstream`
1. **Client → Lineage** — ask "where did gold come from?"
2. **Lineage → OIDC** *(implemented, default off)* — verify JWT (`LINEAGE_OIDC_ENABLED`); no/invalid token → 401.
3. **Lineage → OpenFGA** *(implemented, default off)* — `check can_get_metadata` (same right as describe-table); reuses the shared store, read-only.
4. **Lineage → AGE** — openCypher `DERIVED_FROM*1..` traversal; the dataset name is an **agtype bind param**, never interpolated (injection-safe).
5. **Lineage → Client** — `200` with the transitive upstream set.

> **The governance gaps the diagram tracked (audit `w8u4rc2tg`) — now resolved:**
> 1. ✅ *Lineage query* reads are **authz-gated** (OIDC + `can_get_metadata`) with transitive-disclosure
>    filtering — was a full data-estate leak (P0 #1). Opt-in: `LINEAGE_OIDC_ENABLED` + `LINEAGE_FGA_ENABLED`.
> 2. ✅ Lineage **ingest** requires a verified token and **binds the author** to it — provenance is no
>    longer forgeable (P0 #2); the catalog forwards the caller's bearer on emit.
> 3. ✅ The catalog **emits create-lineage** with the verified author — "who created the table" is an
>    audit fact at `GET /datasets/{id}/creator` (P0 #3). Insert/delete/merge-insert/update and the
>    compaction sweeper all emit too — the write surface is fully covered.
>
> Tracked in [`docs/DECISIONS.md`](DECISIONS.md). Run the whole loop: `scripts/governance_e2e.sh` (or
> `DEMO=1 scripts/governance_e2e.sh` for the narrated [`governance_demo.py`](../scripts/governance_demo.py)).

---

## Workshop scenarios

- **"Show me security end-to-end"** — run *Create table*; pause on steps 2→3→5 (verify → authorize → seed-ownership). The cascade is why one create grants exactly the right future access.
- **"Two credential modes"** — open *Read / query*, step through once in **Mode B** (server-mediated), then flip to **STS vending** and step again. Same flow, same S3 storage, two credential paths.
- **"Where does lineage come from?"** — run *Promote*; the last two steps (emit → MERGE) show provenance is a **byproduct of the job**. Ingest now **binds the verified author** (P0 #2). The event-driven medallion movers that emit this lineage are built & deployed ([`FLOW.md`](FLOW.md)); only the *distributed* lance-ray Ray Data job is still the rask future.
- **"What's still open?"** — *Lineage query*, steps 2–3 are now the SHIPPED authz gate
  (`LINEAGE_OIDC_ENABLED` + `LINEAGE_FGA_ENABLED`, on in the chart); the remaining opens live in
  [`docs/DECISIONS.md`](DECISIONS.md#7a--live-verification-residuals) §7a (live-verification residuals) and
  [§9](DECISIONS.md#9--feature-gaps-the-open-backlog) (feature gaps).
