# Catalog architecture & status

The doc to read first. Plain-language map of **what we're building, how the pieces fit,
where we are right now, and what's next**. Skim the diagrams; read the section you need.

> 🧭 **Want what happens to a write, in order — a client writes through the catalog → the catalog announces it →
> lineage records it → the user's pipeline moves it on → maintenance?** Read **[`FLOW.md`](FLOW.md)**.
>
> 🔬 **Are we correct against the spec?** See [`COVERAGE.md`](COVERAGE.md) and the recorded spec deviations in
> **[`docs/adr/0020-feature-gap-minor-deviations-1-7-the-spec-deviation-register.md`](adr/0020-feature-gap-minor-deviations-1-7-the-spec-deviation-register.md)**.
>
> 📜 **What contract do producers and consumers actually rely on?** See **[`DATA-CONTRACT.md`](DATA-CONTRACT.md)** —
> "the Lance manifest is the schema, the version is the handshake": the storage/bus/identity
> contract, its enforcement points, what Dapr+NATS do and don't enforce, and the Lakekeeper comparison.

> 🖱️ **Prefer to click through it?** Open [`system-diagram.html`](system-diagram.html) — an
> interactive walk-through of the four core flows (create / read / promote / lineage), with the
> data-plane mode toggle (Mode B server-mediated vs STS vending) and real payloads per step.
> ([`system-diagram.md`](system-diagram.md) is the text companion.)

---

## 1. What this is (one paragraph)

rask's catalog is a **REST catalog for Lance datasets** — a thin HTTP service (FastAPI) that
lets clients create/list/describe namespaces & tables, read/write table data (Arrow-IPC),
and does it with **authentication** (OIDC) and **fine-grained authorization** (OpenFGA,
Zanzibar-style). The actual table bytes live in object storage (MinIO/S3) as **Lance
datasets**; the catalog is the *control plane* that names them, locates them, hands out
scoped credentials, and decides **who may do what**. Think "Iceberg REST catalog /
Lakekeeper, but for Lance (multimodal: vectors + blobs + columns)."

---

## 2. The two planes (the single most important idea)

```
                CONTROL PLANE                          DATA PLANE
        (this service — decides + locates)     (engines — move the bytes)
   ┌─────────────────────────────────────┐   ┌──────────────────────────────┐
   │  FastAPI REST catalog                │   │  lance-ray / pylance / a job │
   │  - namespaces & tables CRUD          │   │  - read_lance / write_lance  │
   │  - describe -> location + creds      │──▶│  - add_columns (ETL)    │
   │  - OIDC authn + OpenFGA authz        │   │  - transforms / vector search│
   │  - seeds ownership tuples on create  │   │  - commits new Lance versions│
   └─────────────────────────────────────┘   └──────────────────────────────┘
                    │                                        │
                    ▼                                        ▼
        OpenFGA (tuples)  +  OIDC IdP (Dex)          Object store (MinIO/S3)
                                                      Lance dataset dirs (*.lance)
```

The catalog **moves no data between tiers itself** (no ETL inside the API process). It authorizes, locates, records
and announces, and it governs every write in its own write path: compute is a **client** of the catalog. So moving
data between tiers is the user's job, while the rules a tier write must satisfy are the catalog's (§6).

### Credential vending & STS — how, and why it matters for the lakehouse

A client asks for a **credential** to reach object storage directly through the spec's own parameter,
`DescribeTable` / `DeclareTable` with `vend_credentials` (`lance_docs/ns_catalog/spec.yaml`), or through rask's
extension `POST /management/v1/table/{id}/credentials?tier=read|write`, which names a tier. Which tier the spec doors
vend, and that write credentials never reach `_refs/` or a final manifest, is LH-229 (#149). Three pluggable modes
(`chart` `vending.mode`; `services/catalog/core/vending.py`, `VendingMode`), strongest first:

1. **STS vending (`StsVendor`) — recommended.** The catalog calls the S3 backend's STS
   `AssumeRole` with an **inline session policy** scoped to *just this table's bucket/prefix and
   tier* (read vs write), and gets a **short-TTL** `{access_key, secret_key, session_token}`. The
   client reads/writes the bytes directly with that token. Works on **MinIO, Ceph RGW, AWS**.
2. **Mode B (`ModeBVendor`) — safe default.** No credential is vended; the client uses the
   catalog's server-mediated Arrow-IPC endpoints. Backend-agnostic; nothing is delegated.
3. **Web-identity (`WebIdentityVendor`, `web_identity`).** The caller's OIDC token is exchanged via
   `AssumeRoleWithWebIdentity` for creds bound to a role policy, narrowed per table by an inline session policy.

**Why STS is the right default for a *governed* lakehouse:**

- **Least privilege, per table.** The session policy means a token minted for `silver$features`
  can't touch `gold$revenue`. The OpenFGA decision (`can_read_data` / `can_write_data`) is
  **projected onto the storage layer**, so authorization still holds on the direct-I/O fast path —
  not just at the API.
- **Short-lived.** Tokens expire in minutes (`expires_at_millis`); a leaked one has a small,
  bounded blast radius instead of being a standing key.
- **No durable secrets on compute.** A client's jobs never hold long-lived storage keys — they
  get a fresh scoped token per table and authenticate to the catalog with **workload identity**.
- **Direct I/O, no proxy bottleneck.** Bytes flow client ↔ storage (not through the catalog), so
  the catalog stays a thin control plane while large **multimodal** reads/writes scale on the engine.
- **One identity, three axes.** The same `table:<id>` gates *who-may* (OpenFGA), *which token*
  (the vended STS creds), and *what-changed* (Lance versions + lineage).

---

## 3. Component map

```mermaid
flowchart LR
  C[Client / a user's pipeline / curl] -->|Bearer token| API[FastAPI catalog]
  API -->|verify JWT via JWKS| DEX[(OIDC IdP — Dex in dev)]
  API -->|check can_* / write tuples| FGA[(OpenFGA)]
  FGA --> PG[(OpenFGA datastore — Postgres or SQLite)]
  API -->|pylance DirectoryNamespace| OS[(Object store — MinIO/S3)]
  OS --- L[Lance dataset dirs: bronze/…, silver/…, gold/…]
```

Code layout (where each concern lives). The catalog service is a uv workspace member, so its
package sits under `services/catalog/src/catalog/`, layered into `api/` (routes/deps/security),
`core/` (config + infra), and `services/` (business logic). The cross-service modules
(`secrets`, `dapr_auth`, `fga`, `oidc`, `audit`, `exceptions`) moved into **`service-kit`** at
R19 — `services/common` and `packages/common` are both gone, and every service now imports
`from service_kit.X` (the governed subset lives under `service_kit.governed`):

| Path | Responsibility |
|---|---|
| `services/catalog/src/catalog/main.py` (entrypoint `catalog.main:app`) | App lifespan: build OIDC verifier + OpenFGA client into `app.state`, graceful shutdown |
| `services/catalog/src/catalog/api/security.py` | **Authn** — verify the OIDC token → `CurrentToken` (or fail closed) |
| `services/catalog/src/catalog/api/fga_deps.py` | **Authz** — `authorize` (router-level pre-op `can_*` check) **and** `seed_ownership` (post-create grant). The op→`can_*` map is the only policy logic in the app. |
| `packages/service-kit/src/service_kit/governed/fga.py` | Shared OpenFGA client wrapper (`from service_kit.governed import fga`): `check`/`batch_check`/`list_objects`/`write_tuples`/`grant_on_create`, id helpers, retry + fail-closed |
| `packages/service-kit/src/service_kit/governed/auth/model.fga` / `model.json` / `model.fga.yaml` | The authorization **model** (DSL, the JSON the app loads, and the model tests) — the model owns the privilege math |
| `services/catalog/src/catalog/api/v1/endpoints/*` | Thin HTTP handlers → `services.native`/`services.dataplane` for the backend, `fga_deps.seed_ownership` for grants |
| `services/catalog/src/catalog/services/native.py`, `dataplane.py` | Call pylance (run in a threadpool — it's blocking) |
| `services/catalog/src/catalog/core/config.py` | `pydantic-settings` (env-driven config) |

---

## 4. Request lifecycle (what happens on every call)

```
HTTP request
  │
  ├─ 1. Authn  (services/catalog/api/security.py)
  │      verify the OIDC JWT (signature via JWKS, iss/aud/exp, alg allowlist).
  │      → CurrentToken (the caller's sub) or 401. Fail closed if verifier missing → 503.
  │
  ├─ 2. Authz  (services/catalog/api/fga_deps.authorize — a router-level dependency)
  │      derive (object, can_* relation) from the route, e.g.
  │        describe table  -> check can_get_metadata on table:<id>
  │        insert  table   -> check can_write_data   on table:<id>
  │        drop    table   -> check can_drop         on table:<id>   (owner tier)
  │        create  table   -> check can_create_table on the PARENT    (create-on-parent)
  │      OpenFGA says yes/no. No → 403. OpenFGA down → 503 (never silent allow).
  │
  ├─ 2b. Overwrite gate  (create mode=Overwrite of an EXISTING table, in the create handler)
  │      Overwrite is spec-defined as drop+recreate, so it ADDITIONALLY requires owner-tier
  │      can_drop on the existing table — BEFORE the destructive write. Without it a mere
  │      namespace writer could overwrite (destroy) and, via step 4b, seize another user's table.
  │
  ├─ 3. Handler  (services/catalog/api/v1/endpoints/*)
  │      run the pylance backend op in a threadpool (it's blocking I/O).
  │
  ├─ 4. Seed  (only on create — fga_deps.seed_ownership)
  │      write owner + parent tuples for the new object so the creator keeps access
  │      and the new object inherits the cascade. No-op when FGA is off/unauthenticated.
  │
  └─ 4b. Revoke  (on drop/deregister/rename-source/overwrite — fga_deps.revoke_ownership)
         delete EVERY tuple on the removed object so a reused id can't inherit stale grants
         (privilege bleed). A Cascade namespace drop enumerates its descendants first and
         revokes each; an Overwrite resets the replaced table's ACL (owner-gated at 2b).
```

**Why two authz touch-points?** Step 2 is the *pre-op check* ("may you?"). Step 4 is the
*post-create grant* ("you made it, you own it; link it into the tree"). Step 4b is the
*revoke-on-removal* (drop/overwrite clears grants so a reused id starts clean). All live in
`fga_deps.py` so all request-time authz policy is in one module.

---

## 5. The authorization model (OpenFGA)

### What exists today (P0 — shipped & tested)

```
catalog                      (the root; "the project" today)
  └── namespace              (self-nesting: bronze, bronze$domain1, …)
        └── table

types:   user, role, catalog, namespace, table
subjects: user:<sub>  AND  role:<name>#assignee   (grant a team/role, not just a person)
rungs:   owner ⊇ writer ⊇ reader   (concentric; cascade DOWN via `parent`)
actions: can_get_metadata, can_read_data, can_write_data, can_drop, can_deregister,
         can_delete, can_create_table, can_create_namespace, …   (the app checks THESE)
```

- **Concentric**: an `owner` is automatically a `writer` is automatically a `reader`.
- **Cascade**: a grant on a namespace flows to its nested namespaces and tables via the
  `parent` edge (which `seed_ownership` writes on create).
- **Roles as subjects**: `role:data_eng#assignee` can be granted a rung on any object;
  add people to the role and they all inherit it. (This is how "teams" work.)
- **`can_*` actions**: the app never checks raw `owner/writer/reader`; it checks an action
  like `can_write_data`, and the *model* decides that reduces to the writer rung. Move the
  policy in the model, not the code.

The model is in three kept-in-sync files: `model.fga` (human DSL, source of truth),
`model.json` (what the app loads into OpenFGA), `model.fga.yaml` (model tests with a
tier scenario: bronze/silver/gold + ingestion/data_engineer/analyst roles).

### The hierarchy as built

The model now carries `estate`, `project`, `warehouse`, `team`, `classification` and the `validator` and `publisher`
rungs beside the types above (`model.fga`). A table lives in `project > warehouse > namespace > table`; a team is
granted a rung on a project or a namespace, and `can_promote` / `can_update_tag` gate publication. See
[`AUTHZ.md`](AUTHZ.md) and the `openfga` skill.

---

## 6. The medallion: tier governance (bronze → silver → gold)

Tiers are **namespaces**; datasets are **Lance tables** (each a `*.lance` directory). Default storage: **one bucket
per warehouse, tiers = prefixes** (authz and physical storage are independent axes).

```mermaid
flowchart LR
  SRC[an external source] --> P0[the user's ingest job]
  P0 -->|namespace doors, via catalog| B[bronze tables]
  B -->|read new versions| P1[the user's pipeline: transform]
  P1 -->|namespace doors, via catalog| S[silver tables]
  S --> P2[the user's pipeline: aggregate] -->|namespace doors, via catalog| G[gold tables]
  CAT[catalog write path: tier permission, provenance, gate] -. governs .- B & S & G
```

**Moving data between tiers is the user's pipeline, on its own compute; the rules a tier write must satisfy are the
catalog's**, applied in its write path so no writer can bypass them
([ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md),
[ADR 0156](adr/0156-the-audit-grilling-amends-0155.md)). A pipeline authenticates → `describe`s its input
[authz `can_read_data` + location/creds] → reads → transforms → `create`/`insert`s the next tier
[authz `can_create_table`/`can_write_data`] → a new Lance version is committed and the catalog announces it on
lineage. Publishing a version runs the project's quality gate and moves the table's `published` tag. What the catalog
enforces today, and the ADR 0156 rules still to land (the `rask.tier` judge, provenance in the spec Context, the tag
gate for readers), are in [`TIER-GOVERNANCE.md`](TIER-GOVERNANCE.md).

---

## 7. Where we are right now (status)

| Capability | Status |
|---|---|
| Control plane: namespaces/tables CRUD, `describe` → location + creds | ✅ done |
| Data plane (single-table): insert/append, merge_insert, update, delete, query, count, add_columns | ✅ done |
| Authn (OIDC, PyJWT, JWKS, alg allowlist, fail-closed) | ✅ done |
| Authz (OpenFGA, `can_*` per op, concentric + parent cascade, roles-as-subjects) | ✅ done, e2e-verified |
| Post-create ownership seeding (single `seed_ownership` helper) | ✅ done |
| Resilience (retry + fail-closed incl. transport errors; one retry layer; bounded) | ✅ done |
| Medallion model + tests (bronze/silver/gold + persona roles) | ✅ done (model + `model.fga.yaml`) |
| Tier movement (bronze→silver→gold) | the user's pipeline, on its own compute ([ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)); the catalog runs the publish-time quality gate, and the ADR 0156 tier rules are tracked in [`TIER-GOVERNANCE.md`](TIER-GOVERNANCE.md) |
| `project` type + 3-axis governance (teams × projects × tiers) | ✅ modeled (`model.fga`: project/warehouse/team/validator) + fga-tested |
| Orchestration | the lakehouse runs none ([ADR 0154](adr/0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md)); Dapr cron bindings drive maintenance and lineage's reconcile, and NATS/Dapr pub-sub carries the internal events |
| Lineage (OpenLineage ingest + graph queries over Apache AGE; producer-side emitter) | ✅ service built & deployed; read-side authz implemented + SvelteKit UI (see §9, `docs/LINEAGE.md`) |
| OTel observability (OTel Collector + GreptimeDB + Perses) | ✅ built, deployed, `make e2e-obs`-verified |

---

## 8. "Commit auth" — what it is and how it's already handled

A **commit** here = writing a new Lance version (append/merge/update/delete, through a catalog door or a stock
client's `CreateTableVersion`). In a governed catalog you want a commit to be **(a) authorized,
(b) audited, (c) lineage-tracked** — three *different* things:

| Concern | Question | Who answers it | Status |
|---|---|---|---|
| **(a) Authorization** | "May this caller commit to this table?" | **OpenFGA** — `authorize` checks `can_write_data` (or `can_commit`) on the table before the handler runs | ✅ already wired |
| **(b) Audit trail** | "What versions exist; what changed?" | **Lance versioning** — every commit is an immutable, time-travelable version | ✅ free from the format |
| **(c) Lineage** | "Where did this data come from?" | **OpenLineage** (emit on commit) → the in-service Apache AGE graph | ✅ built & deployed (see §9 + `docs/LINEAGE.md`) |

So **commit auth already exists**: committing goes through the same `authorize` dependency that gates every write,
checking `can_write_data` on `table:<id>`. A stock client commits through the spec's `CreateTableVersion`: the
catalog answers `managed_versioning=true`, judges the staged version (the operation's rung, file versions, bases,
footers, the primary key, protected columns) and finalizes it (ADR 0157, LH-471 #597). Decided and not yet built:
on silver and gold a commit must name its run and its inputs in the spec `Context` headers, which the catalog
stamps as `rask.*` transaction properties after checking `can_read_data` on each input, and from which it emits
the lineage edge itself (ADR 0156 C2, LH-225 #180). `docs/TIER-GOVERNANCE.md` tracks each rule.

---

## 9. Lineage (OpenLineage + graph DB) — a *separate* axis

This is what your "OpenLineage + graph database + dummies in between" intuition is really
about — and it's **provenance/observability, not access control**:

- **OpenLineage** = an open standard for emitting "a job *run* read inputs X, wrote outputs Y" events. A user's
  pipeline emits its own runs; the catalog emits an event for every write it commits, and on governed tiers ADR 0156
  makes that emission mandatory.
- **Backend** = where events land and become a queryable graph. Today that is rask's lineage service over Apache AGE;
  ADR 0158 keeps provenance (an authenticated intake, the durable commit feed and table-level edges) as a rask service
  and makes the full lineage graph bring-your-own, fed by a forwarder (LH-468, #592).

How it relates to commit auth: **complementary, not the same.** On a governed commit: OpenFGA says *may you*
(authz), Lance records *a new version* (audit), OpenLineage records *derived-from bronze@v3* (lineage).

```
commit  ──▶  OpenFGA check (may you?)        ← access control      [done]
        ──▶  Lance new version (what/when)   ← audit trail         [free]
        ──▶  OpenLineage event (from where?) ← lineage graph       [built]
```

**Built as a lightweight Marquez.** Rather than Marquez (Java/Dropwizard + relational
Postgres) or Neo4j, the `services/lineage/` service is a small FastAPI app that ingests OpenLineage
events at the standard `POST /api/v1/lineage` path and stores them in **Apache AGE** (a
Postgres graph extension), so lineage traversal is native openCypher. Producers emit via the
official `openlineage-python` client (`services/lineage/src/lineage/seed.py` emits a synthetic set for development).
Full design + API in [`docs/LINEAGE.md`](LINEAGE.md).

---

## 10. How to run / verify (dev)

```bash
# unit + integration tests (no network)
uv run pytest -q
uvx ruff check . && uvx ty check

# full auth stack end-to-end (Dagger services: catalog + Dex + OpenFGA + Postgres + MinIO)
make auth-chain                 # = dagger call auth-chain: anon 401 → alice create/read/write 200 → bob 403
```

Config is env-driven and estate-wide — ONE name per setting, declared once in
`packages/service-kit/src/service_kit/governed/settings.py`: `RASK_OIDC_ENABLED`, `RASK_FGA_ENABLED`,
`RASK_FGA_API_URL`, `RASK_FGA_ROOT_OBJECT` (default `warehouse:lance_catalog`),
`RASK_FGA_TIMEOUT_SECONDS`, etc.

---

## 11. Glossary

- **Control plane / data plane** — decide+locate (this service) vs move bytes (engines).
- **Namespace** — a logical container (a path prefix); a medallion *tier* is a namespace.
- **Table / dataset** — a Lance dataset; physically a `*.lance` **directory** (versioned).
- **Tuple** — an OpenFGA fact, e.g. `user:alice owner table:db1$users`.
- **`can_*` relation** — an action permission the app checks (model maps it to a rung).
- **Concentric** — owner ⊇ writer ⊇ reader.
- **Cascade** — a grant flows down the `parent` edge (catalog → namespace → table).
- **Create-on-parent** — creating a child is authorized against its parent.
- **Credential vending** — the catalog returns scoped storage creds per table (`describe` with `vend_credentials`).
- **Medallion** — bronze → silver → gold tiers, governed in the catalog's write path.
