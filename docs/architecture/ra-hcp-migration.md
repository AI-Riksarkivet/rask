---
title: ra-hcp → rask — storage harvest (slimmed)
description: rask's storage is already backend-agnostic, so we harvest ONLY ra-hcp's frontend S3 browser — not its backend.
icon: lucide/git-merge
status: new
---

# ra-hcp → rask — storage harvest (slimmed)

!!! abstract "Supersedes the 9-phase plan"

    The original "absorb all of ra-hcp" plan is **retired**. Decision (2026-06-23,
    audit-backed): rask's storage is **already backend-agnostic**, so we do **not**
    merge ra-hcp's backend. We harvest **only** ra-hcp's frontend S3 bucket-browser
    into the `storage-frontend` MFE, backed by rask's own agnostic `volumes-api`.

## The verdict that drove the slim-down

A storage-agnosticism audit (multi-agent, read-only) confirmed `packages/storage`
speaks **plain S3** — MinIO / rustfs / AWS / Ceph / HCP all work with **env vars only,
zero code changes** (MinIO and rustfs are the targeted backends, both verified). The
coupling was *lexical* (env-var names were `HCP_*`), not
functional, and is now fixed. So ra-hcp's backend buys rask nothing, and every part
of it is an active coupling risk:

| Dropped | Why (audit finding) |
|---|---|
| HCP backend service (`hcp_api`) | rask storage is already agnostic — nothing to gain |
| MAPI (~2.5k LOC admin API) | pure Hitachi-vendor admin; **zero S3 relevance** |
| async `aioboto3` adapters | hold an `AsyncExitStack` + live client — **can't pickle into Ray Data**; rask's sync `Source`/`Sink` surface stays untouched |
| JWT-stores-plaintext-password auth | rask is no-auth / localhost / trusted-network |
| Redis cache + second config tree | `service-kit` is contractually dependency-light |
| SDK / CLI / ETL | not needed for a UI harvest |

**A rask `project` is an app-workspace axis, not an HCP tenant.** No tenant/namespace
model is imported. If per-project storage isolation is ever wanted, it's per-project
S3 *prefixes* governed by rask RBAC — never an HCP tenant.

## Swap to MinIO / rustfs today — no code change

- `RASK_S3_ENDPOINT_URL=http://minio:9000` (or `S3_ENDPOINT_URL`, or legacy `HCP_ENDPOINT` — all accepted via `AliasChoices`)
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` = your backend keys (set directly → `derive_hcp_creds` no-ops)
- `RASK_S3_INSECURE=1` for a self-signed endpoint; do **not** set `HCP_USERNAME/HCP_PASSWORD`
- The Helm chart already ships an **in-cluster MinIO** (`minio.enabled`), so the agnostic backend is deployed, not hypothetical. rustfs is the other verified target.

## Done

- **Agnostic config** — `Settings.s3_endpoint_url` / `s3_insecure` via
  `AliasChoices(RASK_S3_ENDPOINT_URL, S3_ENDPOINT_URL, HCP_ENDPOINT)`; `storage.s3_client`
  resolves the endpoint/CA/insecure canonical-first and passes `region_name`; docstrings
  de-HCP'd. `derive_hcp_creds` kept as the one opt-in, no-op-unless-`HCP_USERNAME/PASSWORD` bit.
- **Read endpoints** on `volumes-api`, all over the agnostic `storage.s3_client`:
  - `GET /api/v1/volumes/objects` — delimiter-scoped listing (prefixes + objects)
  - `GET /api/v1/volumes/object` — S3 HEAD metadata (size / content-type / mtime / etag)
  - `GET /api/v1/volumes/object/download` — full bytes + content-type + download disposition
- **`packages/tracker` + `packages/validate`** — the only ra-hcp Python worth keeping
  (a transfer ledger + an image validator — the medallion governance pieces). Landed as
  leaf libs.
- **`storage-frontend` bucket-browser** — the MFE has a read-only browser wired to
  `/objects` (list + prefix nav), an **object-detail dialog** over `/object` (HEAD
  metadata) and **download** via `/object/download`, all styled with `@rask/ui` and
  routed through the gateway proxy. It imports **zero** ra-hcp backend code; the data
  comes from `volumes-api`. Upload/delete remain a later, backend-gated step.

## Left

- **Upload / delete** — the read-only browser is complete; write operations are the
  remaining, backend-gated step.

## Optional, later

If multipart / presigned URLs / versioning / batch-delete are ever needed, the audit
documented the path: vendor-copy ra-hcp's **async `ObjectStore` abstraction** (protocol +
adapters + factory + a `CredentialStrategy` boundary) into `packages/storage` as a *sibling*
async surface (the sync Ray surface untouched), with MAPI quarantined in an optional,
default-off brick. Defer until there's a concrete consumer — the current sync surface +
the three read endpoints cover the read-only browser.

> Durable summary; memory: `project-ra-hcp-migration-plan`.
