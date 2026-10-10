# 0014. Control-plane vs data-plane split (the prod cut)

**Decision.** *Authorize the manifest commit and the provisioning ops; let bytes go direct to the store under
scoped, expiring vended creds — never through the server.* Four planes:

| Plane | Operations | Authorized by |
|---|---|---|
| **Admin / provisioning** | create tenant/team, create warehouse (provision bucket, register `base_uri`, stamp 2.2 + stable-row-ids), create/drop namespace, manage FGA model/tuples | platform admin (`project` / `warehouse` / `namespace` admin relations) |
| **Control / coordination** | the manifest-version commit (the single serialization point), rename, declare/deregister, branch/tag, restore, clone, credential vending, DDL | table-scoped FGA (`can_commit`/`can_promote`/…) — **authorize the commit call, not the bytes** |
| **Data** | `write_fragments` (client→bucket direct), scans/query, insert/merge/update/delete, MV refresh, blob read | data-scoped FGA (`can_write`/`can_read`) — bytes flow client↔store under vended, expiring creds |
| **Eventing** | lineage outbox → Dapr publish → consumer → AGE | trusted internal channel (Dapr → NATS) |

**Rationale.** This is exactly the Lakekeeper/Polaris cut, and the FGA model already encodes it. What a prod
control plane still lacks: a managed admin API/UI to *provision* tenants + warehouses and manage grants
(today grants are enforcement-only, no managed surface) and the physical bucket-per-warehouse to back it —
see #3-A.
