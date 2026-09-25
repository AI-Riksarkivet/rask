# Storage and credential vending: what Lakekeeper does, and how rask should solve its rows on its own stack

Date: 2026-09-25. Lakekeeper checkout `/home/gabriel/Desktop/lakekeeper-ref` at `a58e4017` (2026-06-22).
rask at `10b6868b` plus the staged pylance-12 diff. The staged diff does not touch any vending file: `git diff --cached`
names no path under `core/vending.py`, `endpoints/credentials.py` or `lakehouse/objectfs.py`.

This was read-only. I wrote to the repo nowhere and ran no cluster writes. I made three measurements:

- `kubectl get` on the live object store.
- A string scan of the installed pylance 12.0.0 binary.
- One construct-only `DirectoryNamespace` probe. It ran from the scratch dir against `127.0.0.1:1`, so it touched no real store.

Every claim below carries a `path:line` cite. Lakekeeper paths are relative to `crates/lakekeeper/src/` unless the
path says otherwise.

---

## 0. The question asked: exactly what may a WRITE credential touch under a table location?

### Lakekeeper

**Tier selection.** `WriteData` maps to `StoragePermissions::ReadWriteDelete`. `ReadData` alone maps to `Read`.
This happens after one batched authz check, in both the table door and the generic-table door
(`server/tables.rs:1164-1170`, `server/generic_tables/credentials.rs:318-324`). Create and register always vend
`ReadWriteDelete` on the new location (`server/tables/create_table.rs:326-335`, `server/tables.rs:461-469`).

**Actions per tier** (`service/storage/s3.rs:940-959`):

| Tier | Actions |
|---|---|
| Read | `GetObject`, `GetObjectVersion` |
| ReadWrite | Read, plus `PutObject`, `AbortMultipartUpload`, `ListMultipartUploadParts`. This tier has no `DeleteObject`. |
| ReadWriteDelete | ReadWrite, plus `DeleteObject` |

**Resources** (`s3.rs:961-1032`):

- One object statement on `arn:aws:s3:::<bucket>/<escaped key>/*`. The key is `"<table-key>/"`, and IAM metacharacters in it are escaped (`s3.rs:976-977`, `:990`).
- `s3:ListBucket` on the bucket, gated by `StringLike s3:prefix = <key>/*` (`:992-1002`).
- `s3:GetBucketLocation` on the bucket (`:1003-1011`).
- If the warehouse has a KMS key, `kms:Decrypt` and `kms:GenerateDataKey` on that key (`:1014-1021`).

**What that means.** A Lakekeeper write credential may read, write and delete **every object under the table's
location**. That includes the whole `metadata/` subtree. Nothing is carved out by plane.

This is sound for Iceberg for one reason: the commit pointer (`metadata_location`) lives in Lakekeeper's Postgres. A
writer can drop metadata JSON files into the prefix, but only the catalog's commit transaction can make one of them
current. Deletion is discouraged only by a client hint. `push_s3_delete_disabled` sends `s3.delete-enabled=false`
under soft-delete (`s3.rs:139-156`, `:409-413`), but the credential itself still carries `s3:DeleteObject`.

Across stores, the no-delete `ReadWrite` tier is defined everywhere, yet no table door uses it:

- GCS ReadWrite = `objectViewer` + `objectCreator`, with no delete (`service/storage/gcs/sts.rs:147-158`).
- Azure ReadWrite has no `delete` bit (`service/storage/az/mod.rs:126-151`).
- R2 collapses both write tiers onto `object-read-write` (`s3.rs:675-680`).

### rask today

The write tier is granted to **either** `can_write_data` **or** `can_maintain`, and both get the same actions
(`services/catalog/src/catalog/api/v1/endpoints/credentials.py:79-107`). The actions are `_WRITE_ACTIONS` =
`GetObject`, `PutObject`, `DeleteObject` and `AbortMultipartUpload` (`services/catalog/src/catalog/core/vending.py:125-131`).
They apply on `<bucket>/<prefix>/*`, with `ListBucket` on `<prefix>/*` (`vending.py:459-475`).

Two narrowings exist:

- **Branch mode:** main drops to read, and write lands on `<prefix>/tree/<branch>/*` (`vending.py:457-484`).
- **Sanctioned bases:** these get READ only (`vending.py:485-526`).

So rask copied Lakekeeper's shape: prefix-wide read/write/delete. **On Lance the same shape is one rung wider than on
Iceberg.** Lance keeps the commit CAS in the object store. `_versions/{N}.manifest` is written put-if-not-exists
(`lance_docs/file_format.md:3232-3236`, `:4770`), and tags and branches are plain JSON under `_refs/`
(`file_format.md:2719-2735`, `:2796-2804`).

A prefix-wide `PutObject`/`DeleteObject` is therefore **commit, tag, branch, restore and version-delete authority**.
The FGA writer rung holds none of those. The earlier audit already names this defect
(`findings_lance_lakekeeper.md:50`). Lakekeeper confirms the diagnosis, because its model is only safe where the
catalog owns the pointer.

### What rask should vend (translation onto Lance)

Two things replace Lakekeeper's "catalog owns the pointer" property. The **plane allow-list** is one. The other is
rask's existing catalog-side fragment-commit door, which still uses Lance's own object-store CAS. This keeps the
LANCE-ONLY ruling's "CAS stays in the store" intact. It also avoids `managed_versioning`, which is refused at
`services/catalog/tests/test_a_version_door_that_mints_one_records_who_did_it.py:1-9`.

| Rung | Get / List | Put / AbortMPU | Delete | Never |
|---|---|---|---|---|
| read (`can_read_data`) | `<p>/*` | – | – | – |
| writer (`can_write_data`), the Lakekeeper **ReadWrite** tier | `<p>/*` | `<p>/data/*` and whatever else Lance's own `file_audit` log shows `write_fragments` creating (measure; see below) | **none** | `_versions/*`, `_refs/*`, `_transactions/*` |
| maintainer (`can_maintain`), the Lakekeeper **ReadWriteDelete** tier | `<p>/*` | `data/*`, `_deletions/*`, `_indices/*`, `_transactions/*`, `_versions/*` | the same content planes plus `_versions/*` (reclamation) | `_refs/*` |
| catalog root | everything | everything | everything | – |

Each row has a branch variant: the same planes under `<p>/tree/<b>/`.

Three conditions apply to this table:

- **Derive the writer allow-list by measurement, not from this table.** Lance's own
  `LANCE_LOG=lance::events::file_audit` records every file an operation creates or deletes
  (`findings_lance_lakekeeper.md:22`). The rule is **no Delete for writers**, which mirrors Lakekeeper's unused
  ReadWrite tier. The exact Put list must come from that log on pylance 12. `lance_docs` does not say where blob-v2
  payloads or `_mem_wal/` (`file_format.md:3363-3367`) land for a given write.
- **Check the policy size limit first.** Lakekeeper pins AWS's 2048-character plaintext cap on the session policy
  (`s3.rs:2343-2388`). A per-plane allow-list plus two statements per sanctioned base can approach it. Whether the
  deployed MinIO enforces the same cap is **unmeasured**.
- **Test it RED-first on the real store.** Write the policy-builder test first. Then drive the deployed MinIO:
  - A writer PUT to `_versions/9999.manifest` must answer 403.
  - A writer PUT to `_refs/tags/x.json` must answer 403.
  - A writer DELETE of `data/x.lance` must answer 403.
  - The same writer PUT to `data/x.lance` must answer 200 (the positive control).

This closes the new row "A write-tier vend reaches `_refs/` and `_versions/`". It also closes two things that row
causes:

- **N4 below.** A writer-committed poisoned base can only arrive by a client-side commit.
- **The ungoverned client-side commit.** Any version minted with no lineage event (`findings_lance_lakekeeper.md:50`).

---

## 1. Topic-by-topic

### T1. Tier model: the Delete axis is the one to split on

- **Lakekeeper:** three tiers, and the no-delete ReadWrite tier is defined for every store (`s3.rs:940-959`, `gcs/sts.rs:147-158`, `az/mod.rs:126-151`). Tables only ever get RWD or Read (`server/tables.rs:1164-1170`).
- **rask today:** two tiers. `can_maintain` and `can_write_data` receive byte-identical write credentials. The code comment says "The object store cannot hold that distinction — a rewrite and a write are both `PutObject`" (`credentials.py:81-91`).
- **rask should:** hold the distinction on the axis the store *can* express, which is Delete plus plane.
  - A logical writer never needs Delete. It puts new data files and commits through the catalog.
  - A physical maintainer does need Delete, for compaction residue and `cleanup_old_versions`.
  - Rewrite the `credentials.py:81-91` comment when the split lands.
- **Rows:** new (`findings_lance_lakekeeper.md:50`); LD29 (auto-cleanup under a writer's identity, `findings_lance_docs.md:290-300`). A writer without Delete makes LD29's "a writer without delete rights commits successfully" the *designed* state rather than a surprise.

### T2. Prefix anchoring, metacharacters, and location exclusivity

**Lakekeeper:**

- It **escapes** `*`, `?` and `$` to `${*}`, `${?}` and `${$}` (`s3.rs:1194-1208`). A test pins that a literal `${aws:username}` in a path must not become a live policy variable (`s3.rs:2199-2213`, `:2216-2247`).
- It anchors with a trailing `/` before the single `*` (`s3.rs:976-977`, `:2314-2341`).
- `Location` parsing rejects controls, Unicode Cf characters, `.`/`..`, empty segments, `?` and `#` (`crates/io/src/location.rs:291-357`, `:394-460`).
- `is_sublocation_of` anchors on `/` (`location.rs:193-205`).
- The load-bearing invariant: **no tabular location may equal, contain, or sit under another** in the same warehouse (`crates/lakekeeper-storage-postgres/src/tabular/mod.rs:545-583`). The same holds for warehouse storage profiles (`s3.rs:277-301`).
- The default layout is `<base>/<uuid>`, with no names in paths (`service/storage/storage_layout.rs:204-231`; `docs/docs/storage.md:52-66`, `:161-162`).

**rask today:**

- It *rejects* `*`/`?` in segments (`services/catalog/src/catalog/core/identifiers.py:54-70`), in prefixes and in bases (`vending.py:145-153`).
- It does **not** treat `{`/`}` or `$` specially. Lance's dir-V2 layout puts `<hash>_<ns>$<table>` in the prefix (`lance_docs/ns_catalog/catalog/dir/index.md:136-145`). `$` is the delimiter.
  - Example: a table named `{aws:username}` in namespace `x` yields a prefix containing `${aws:username}`. That is a live IAM policy variable on AWS; this is exactly Lakekeeper's escape test case.
  - How MinIO RELEASE.2025-04-22 treats `${…}` in a session-policy Resource is **unmeasured**.
- The test at `tests/unit/test_vending.py:344-347` states `*`/`?` "cannot be escaped". That is false for IAM (`${*}`/`${?}` are escapes), and for MinIO it is unmeasured.
- **Location exclusivity is missing at register.** An alias id can be registered at another table's prefix (`findings_lance_lakekeeper.md:53`).
- Shared sanctioned `data_base` directories give one table's read grant over every sibling's fragments (LD38, `findings_lance_docs.md:365-370`). LD04 is the same problem for `models/`.

**rask should:**

1. Refuse `{` and `}` in identifier segments at `require_safe_segments`. This works without depending on the store's escape support, and needs a RED test (`{aws:username}` → 400). Only switch to escaping `$`→`${$}` if a MinIO measurement proves `${$}` is honoured, because every rask prefix contains `$`.
2. Enforce Lakekeeper's exclusivity invariant on Lance terms, at register, undrop and rename: no location equal to, under, or above a live or trashed table's location. Settle races with a put-if-not-exists claim record, as `claim_bucket` already does.
3. For LD38 and LD04, give every table its own directory under a shared base (`<base>/<table-uuid>/`). Vend only that directory. This is Lakekeeper's `{uuid}` flat layout applied to bases.
4. Apply the same rule to **branches**, Git-style: refuse a branch name that is a `/`-segment prefix of an existing branch, or has one as its prefix. Lance lays `a/b` out *inside* `tree/a/` (`file_format.md:2763-2781`), so a vend for `a` reaches `a/b`. That is `findings_lance_lakekeeper.md:51` and LD40.

**Rows:** LH-056 (nested branch), XC-017 §B7 (register exclusivity), LD38, LD04, LD19, and new row N5 (`{`/`}`).

### T3. The catalog's own parent identity and the STS flavour

**Lakekeeper:**

- The parent credential is the **warehouse's** `storage-credential`: an access key, or `aws-system-identity` (`s3.rs:203-236`).
- System identity uses the AWS SDK default chain, `aws_config::from_env()` (`crates/io/src/s3.rs:227-236`). That chain includes IRSA web-identity, so the pod's projected token is exchanged by the SDK and no static key is needed. See `docs/docs/storage.md:312-420`.
- Three deployment switches guard the ambient identity: `enable_aws_system_credentials` defaults **false**; `s3_enable_direct_system_credentials` defaults **false**, so an assume-role is required; `s3_require_external_id_for_system_credentials` defaults **true** (`config.rs:375-383`, `:1034-1036`). They are enforced at `s3.rs:780-798` and `:883-895`.
- The per-table credential is then `AssumeRole` with an inline `Policy`. `RoleArn` is optional for S3-compat stores (`s3.rs:816-840`).
- **An absent credential means unsigned, not ambient.** The `None` branch builds an `SdkConfig` with no credentials provider at all (`crates/io/s3.rs:237-243`).

**rask today:**

- The vendor delegates from a static MinIO user key. It is the `rask-catalog` policy, wide on data and without admin rights (`chart/templates/minio-scoped-users.yaml:491-545`). The key is resolved from the Dapr secret store (`services/catalog/src/catalog/main.py:146-158`). That is compliant with the secrets rule, but it is a long-lived key.
- `WebIdentityVendor` exchanges the **caller's** Dex token at the store (`vending.py:649-720`). That makes the object store trust the IdP. Lakekeeper never does this: its store trusts only Lakekeeper.
- **Stale prose:** the `WebIdentityVendor` docstring calls web identity "the native flow for RustFS … does NOT support plain AssumeRole" (`vending.py:650-656`). The module docstring measured the opposite (`vending.py:17-28`).
- **Live store:** the store is **MinIO**, `minio/minio:RELEASE.2025-04-22T22-12-26Z`, measured by `kubectl get sts rask-minio`. Its env carries only `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`. The chart ships `minio.enabled: true` (`chart/values.yaml:2309-2313`). CLAUDE.md and `vending.py:6` say "RustFS (this project's default store)", which is false for the deployed estate.
- The `sts.rustfs.com/PolicyBinding` CRD is present (Kubernetes SA → store policies), but no `Tenant` exists. It belongs to the `htr-batch` RustFS deployment (someone else's, per D11's spirit), not rask.

**rask should:**

- Under D1 (machine identity = Kubernetes SA tokens) and "STS for zero trust", the Lakekeeper translation is to make the **catalog's own projected SA token** its STS parent credential:
  - Register the k3s SA issuer as a MinIO OpenID provider, with a RolePolicy mapping and no claim-based policy.
  - The catalog calls `AssumeRoleWithWebIdentity(WebIdentityToken=<catalog SA token>, Policy=<per-table policy>)`.
  - The result is one hop and **no static S3 key in the catalog at all**.
- **Delete** the caller-token mode. There is no backward compatibility to keep. The store should trust only the catalog, as in Lakekeeper.
- Gate the switch on four measurements against the deployed MinIO:
  1. MinIO accepts a k3s SA JWT: the issuer discovery and JWKS are reachable.
  2. RolePolicy works with no `policy` claim.
  3. The inline `Policy` on `AssumeRoleWithWebIdentity` is **enforced**. The existing `/validate` probe answers this.
  4. The session is visible in MinIO's audit log.
- Keep Lakekeeper's "absent means unsigned" rule. A dict without keys must never reach the ambient chain; see T13.

**Rows:** LH-160, XC-002 (the parent key's delivery path), D1, CP-007, and new row N12 (stale prose, "RustFS default").

### T4. STS endpoint versus the vended data endpoint (LH-177)

**Lakekeeper:**

- `endpoint` is the S3 data endpoint. It is used by the server's own IO and vended to clients (`s3.rs:74-80`, `:515-518`; `crates/io/s3.rs:222-224`).
- `sts_endpoint` is **server-only**. It is used only to mint (`s3.rs:88-99`, `:804-814`; `io/s3.rs:157-160`, `:246-258`).
- The control-plane URLs a client needs (the refresh endpoint and the signer) are built from the **request's** own view: `base_uri` or `X-Forwarded-*` (`request_metadata.rs:470-509`).
- OneLake is the explicit precedent for mint host ≠ client host:
  - The Get-User-Delegation-Key call goes to the global host (`az/onelake_profile.rs:439-452`).
  - The vended config publishes the client-facing private-link host as `adls.account-host` (`onelake_profile.rs:551`; `docs/docs/storage.md:733-740`).
- For the tenant-level private-link case: "the URL Lakekeeper builds doesn't change, only DNS does" (`storage.md:736`).

**rask today:**

- The vendor has **one** endpoint, `sts_endpoint`. It uses it both for the STS client and as the `endpoint` it vends (`vending.py:590-591`, `:608`, `:631-642`; `main.py:146-150`). The catalog's own IO uses `s3_endpoint` (`core/config.py:82`).
- The chart happens to set both to the same `lance.s3Endpoint`, `http://rask-minio:9000` (`chart/templates/services.yaml:128`, `:166`; `_helpers.tpl:733-735`). That is why nothing broke.
- On AWS proper, the boot validator *requires* `LANCE_S3_STS_ENDPOINT` (`config.py:624-636`), so every vended credential would carry the STS host as its S3 endpoint. The same happens on any store whose STS is served on its own port.

**rask should:**

1. Split the two. Keep `LANCE_S3_STS_ENDPOINT` server-only. Vend a separate client-facing data endpoint, defaulting to the store's configured endpoint.
2. Make that endpoint a **per-store/per-warehouse property**. The warehouse record already carries a non-secret `endpoint` (`services/catalog/src/catalog/services/warehouses.py:119-124`).
3. Answer the owner question with Lakekeeper's model. Vending is not "in-cluster only by design": the data endpoint is a deployment property that must be resolvable by the clients the deployment serves. The in-cluster/external difference belongs in **split-horizon DNS** on one hostname, not in per-client logic (`storage.md:733-737`).
4. Emit `aws_endpoint` rather than bare `endpoint`. The bare key loses to `AWS_ENDPOINT_URL` in about half of processes (LD27, `findings_lance_docs.md:271-276`).

**Rows:** LH-177, LD27, XC-027 (the edge must be TLS if the endpoint is external).

### T5. The vendor must be resolved per warehouse, not per process

- **Lakekeeper:** every vend resolves the **warehouse's** profile and the **warehouse's** secret (`server/generic_tables/credentials.rs:102-116`, `server/tables.rs:614-628`). The STS cache key includes hashes of both (`service/storage/cache.rs:28-62`).
- **rask today:** `app.state.vendor` is a lifespan singleton bound to the estate's STS endpoint and the estate's key (`main.py:146-170`). The vend door never reads the warehouse record (`credentials.py:108-160`).
  - A warehouse that declares its own `endpoint` plus `credential_ref` (LH-067, `warehouses.py:119-124`; `services/warehouse_credentials.py:41-61`) still gets `AssumeRole` against the **estate** store, with the **estate** key.
  - That produces a policy naming a bucket the estate store may not hold, and a vended endpoint for the wrong store. The server-side data plane does resolve per-base credentials (`services/dataplane.py:255-290`); the vend door does not.
  - It is latent: "Absent means the estate default, which is every warehouse today" (`warehouses.py:119-121`).
- **rask should:** resolve `(sts_endpoint, vend_endpoint, credential_ref)` from the table's warehouse record on each vend. Build or cache a vendor per store, keyed on the resolved configuration, as Lakekeeper's cache key does. Refuse (fail closed) if a referenced credential cannot be resolved; never fall back to the estate key. The vend probe (T7) must use the same per-warehouse vendor.
- **Rows:** LH-067 follow-up / LD38, and new row N2.

### T6. TTL, caching, refresh, and re-authorisation on refresh

**Lakekeeper:**

- TTL defaults to 3600 s and is configurable per profile (`s3.rs:101-104`). Azure clamps it with compile-time floor and ceiling invariants (`az/mod.rs:55-124`).
- STS results are cached with **single-flight** coalescing, and errors are never cached (`cache.rs:138-210`, tests `:248-363`).
- A cached credential is served only for `min(remaining/2, 1h)` (`service/storage/mod.rs:126-131`; `cache.rs:80-102`). A client can therefore receive a credential with only half its life left.
- The expiry is vended (`s3.rs:550-569`), together with a **refresh endpoint** (`s3.rs:570-577`; `gcs/mod.rs:435-440`; `az/mod.rs:498-509`).
- The refresh door re-runs the full authz and emits an authz event on **every** refresh (`server/tables.rs:563-642`, authz at `:584-606`). A revocation therefore takes effect at the next refresh. The cache key omits the principal (`cache.rs:30-38`), so one credential may be shared across callers, but only after each caller passed authz.

**rask today:**

- TTL is 900 s (`config.py:296`; `chart/values.yaml:1010`).
- There is **no server-side cache**. Every vend is an `AssumeRole` plus a root-cred manifest open, plus a branch open if a branch is named (`credentials.py:119-160`).
- The describe path puts `expires_at_millis` inside `storage_options`, as the spec requires (`api/v1/endpoints/tables.py:466-475`; `lance_docs/ns_catalog/spec.yaml:2883-2891`). The management door returns it as a sibling field (`vending.py:59-68`).
- Client-side refresh is a hand-rolled `VendedCredentialCache` that refreshes only between Lance calls (`packages/service-kit/src/service_kit/lakehouse/vended_credentials.py:91-129`).
- `vending_ttl_seconds` allows `ge=60` (`config.py:296`). AWS STS `AssumeRole` rejects `DurationSeconds` below 900, which is public AWS API behaviour; MinIO's minimum is **unmeasured**.

**rask should:**

- Refresh is **Lance's own storage-options provider**. It re-describes with `vend_credentials=true` before `expires_at_millis`, per base too (`findings_lance_lakekeeper.md:14`, `:67`). It re-enters the catalog's authz on each refresh, which matches Lakekeeper's per-refresh re-authorisation. Retire `VendedCredentialCache` once the writers open through `namespace_client`/`namespace_impl`.
- Add a server-side single-flight STS cache, with errors not cached and a serve window of `min(remaining/2, …)`. Key it on `(subject, location, tier, branch, sanctioned-bases hash, store config hash)`. Keep the **subject** in the key, unlike Lakekeeper, so the attribution join in T11 stays exact.
- Bound `vending_ttl_seconds` to `[900, 43200]` once MinIO's range is measured.

**Rows:** LH-129 (refresh inside a long write), CP-001, and new rows N8 (vend cache) and N11 (TTL bound).

### T7. Proving the store enforces the policy, at registration rather than on demand

**Lakekeeper:**

- `validate_access` runs on warehouse create and on storage update, unless `skip_storage_validation` is set (`service/storage/mod.rs:508-596`). It does three things:
  1. It proves the server credential can write, read and delete.
  2. It **vends a real credential** for a sub-location and proves it can write, read and delete there (`mod.rs:602-706`).
  3. It proves the same credential **cannot write the parent** (`mod.rs:763-813`).
- It surfaces both failures together, because an over-permissive store is a security signal (`mod.rs:694-701`).
- The live integration tests prove cross-location read, write and delete denial against real MinIO, AWS, GCS and Azure (`mod.rs:1742-1891`).
- This **corrects** the earlier audit's RASK-AHEAD line, "Lakekeeper attacks only its offline signer and checks STS vending for presence" (`findings_lance_lakekeeper.md:37`). That line is wrong. Lakekeeper runs a parent-write-denial probe on every warehouse create. Its cite, `server_generic_table_signing.rs:618-647`, does not exist in this checkout: `grep -r my_table_secret` finds nothing.

**rask today:**

- The same probe exists, **plus** a conditional-put CAS check Lakekeeper lacks (`services/catalog/src/catalog/services/vend_probe.py:1-41`; `api/v1/endpoints/warehouses.py:1072-1170`).
- It runs **only when an admin calls** `POST /v1/warehouses/{id}/validate` (`warehouses.py:1029-1069`). It is not run at create (`grep _run_scope_probe` finds only the `/validate` caller).
- It also uses the estate singleton vendor (T5).

**rask should:**

- Run `_run_scope_probe` inside warehouse create and store registration, and fail the create on a scope or CAS failure. Skip it only for `mode_b`, and record `skip` as not-`pass` (the existing rule, `vend_probe.py:13-16`).
- Re-run it on a maintenance cron, and alert on `enforced == false`. A store upgrade can silently change policy enforcement.
- Keep rask's additions: the CAS check, and the same-bucket sibling e2e (`tests/e2e-py/test_credential_isolation_e2e.py:196-317`).

**Rows:** XC-017 (the zero-trust list), and new row N6.

### T8. Remote signing: there is nothing to translate

- **Lakekeeper:** per-request S3 SigV4 signing (`server/s3_signer/sign.rs:58-264`).
  - GET/HEAD maps to `ReadData`; PUT/POST/DELETE, and `DeleteObjects` with every key in the XML body, map to `WriteData` (`sign.rs:185-188`, `:626-701`).
  - Every signed location must sit under the table location (`sign.rs:492-529`).
  - Signing can be disabled per profile (`sign.rs:89-104`; `s3.rs:161-166`).
  - Even here the unit is the table location, not a plane.
- **rask on Lance:** `lance_docs/guide.md:2380-2440` documents only static and session keys (plus env) for S3. There is no signer option. **I did not measure** whether pylance 12 exposes a signer hook.
- **rask should:** not build a signer. The per-plane session policy (§0) plus the catalog commit door give the per-path control that remote signing would have given.
- **Rows:** none.

### T9. KMS and SSE

- **Lakekeeper:** when the profile has `aws_kms_key_arn`, it adds `kms:Decrypt` and `kms:GenerateDataKey` on that key to the session policy (`s3.rs:1014-1021`). It advertises SSE-KMS in both the table and catalog config (`s3.rs:415-421`, `:520-530`), and its own writes set the SSE headers (`crates/io/src/s3/s3_storage.rs:490-545`).
- **rask today:**
  - `EncryptionAtRest` vends `aws_server_side_encryption` and `aws_sse_kms_key_id` (`vending.py:71-93`; `lakehouse/objectfs.py:85-118`).
  - But `build_session_policy` never adds a KMS statement (`vending.py:381-527`). On AWS proper the session policy intersects the role, so KMS is denied and every SSE-KMS direct write fails.
  - It is latent, because the chart default is empty (`chart/values.yaml:1012-1019`). MinIO/KES behaviour is **unmeasured**.
- **rask should:** add the KMS statement when `kms_key_id` is set, with a unit test. Verify on the target store before enabling SSE-KMS.
- **Rows:** new row N10 (low).

### T10. Endpoint normalisation, path style, and `allow_http`

- **Lakekeeper:**
  - It requires the endpoint to be `http`/`https` with no path, and strips a trailing bucket (`s3.rs:1080-1121`). The STS endpoint scheme is checked too (`s3.rs:1123-1138`).
  - `s3.path-style-access` is vended only when set (`s3.rs:501-504`).
  - The TLS trust store is webpki plus native roots (`io/s3.rs:35-63`).
- **rask today:** both vendors call `lance_storage_options(...)` **without** `allow_http` or `virtual_hosted` (`vending.py:631-638`, `:710-717`). So every vend carries `allow_http=true` (default at `objectfs.py:33`) and `virtual_hosted_style_request=false`, whatever the endpoint scheme or `LANCE_S3_VIRTUAL_HOSTED` (`config.py:88`) says. The earlier audit (`findings_lance_lakekeeper.md:87`) covers the `allow_http` half. **The same drift exists for `virtual_hosted`**, and no row I found names it.
- **rask should:** derive both inside the one builder, from the vended endpoint and the store's record. Validate the endpoint shape at store registration, as Lakekeeper does.
- **Rows:** LH-096, XC-007, XC-027.

### T11. Attributing a store request to a principal

- **Lakekeeper:** the role session name is constant, `"lakekeeper-sts"` (`s3.rs:818`). The Lakekeeper-side assume uses `"lakekeeper-assume-role"` (`io/s3.rs:261`). Session tags are static per profile (`s3.rs:105-108`, `:842-858`; ABAC example `storage.md:462-528`). The STS cache is shared across principals (`cache.rs:30-38`). **Lakekeeper cannot attribute an object write to a principal.**
- **rask today:** the session name is constant too, `"lance-catalog-vend"` (`vending.py:618`, `:699`). The vend audit records the subject, resource and tier (`credentials.py:173-181`), but not the STS `AccessKeyId`. The describe-path vend emits **no** issuance audit at all (`tables.py:449-476`).
- **rask should be ahead here, cheaply:**
  - Record the issued temporary `AccessKeyId` in the vend audit line. It is not a secret. MinIO's audit log records the access key on each request, so a join on it gives per-object-request principal attribution.
  - Emit the same audit from the describe path.
  - This needs the subject-keyed cache from T6.
  - The session name cannot carry the D5 key `<idp-id>~<claim>`: `~` is outside STS's `RoleSessionName` charset `[\w+=,.@-]`.
- **Rows:** LIN-002 (provenance), XC-003 (audit store), and new row N9.

### T12. Typed secrets and redaction (LH-072)

- **Lakekeeper:**
  - Secret fields are `veil::Redact`, so their `Debug` output never shows the value (`s3.rs:215-256`; `gcs/mod.rs:109-124`; `az/credentials.rs:7-28`; tests at `mod.rs:1344-1385`).
  - The vend response separates the refreshable `creds` map from general `config` (`mod.rs:116-124`). Creds go out as `storage-credentials: [{prefix, config}]`, **per prefix** (`generic_tables/credentials.rs:118-125`).
  - Creds are duplicated into `config` "due to backwards compat reasons" (`gcs/mod.rs:442-446`; `az/mod.rs:526`).
  - The UI can learn the credential *type* without its material (`mod.rs:1096-1165`).
- **rask today:** `VendedCredentials.storage_options: dict[str, str]` mixes secret and config values, with no redacting type (`vending.py:59-68`).
- **rask should:**
  - **The wire stays one map.** The Lance spec has exactly one `storage_options` "passed directly to Lance" (`spec.yaml:2883-2891`). So the answer to LH-072's either/or is: a typed, redacting container in process (`SecretStr` fields, redacted `repr`) that flattens only at the wire boundary.
  - Lakekeeper's per-prefix credential list already has a Lance form, `base_<id>.*` keys (`findings_lance_lakekeeper.md:15`). Use it for per-base credentials rather than a second response shape.
  - The FE-011 "credentials" settings row can follow Lakekeeper's type-without-material echo.
- **Rows:** LH-072, FE-011 (low).

### T13. Fail closed: no fallback to the ambient chain

- **Lakekeeper:**
  - An STS failure fails the load. `generate_table_config` propagates with `?` (`s3.rs:539-541`), mapping to 412 `ShortTermCredentialError` (`service/storage/error.rs:283-285`) or 424/500 (`error.rs:225-240`).
  - The only credential-free outcomes are ones someone asked for: client-managed access, or both mechanisms disabled on the profile (`s3.rs:456-495`).
  - A `None` credential is unsigned, never ambient (`io/s3.rs:237-243`).
- **rask today:**
  - Maintenance degrades to the ambient key on any non-denial failure (`services/maintenance/src/maintenance/services/credentials.py:24-25`, `:101-132`, `:221-247`).
  - `VendedCredentialCache` returns `None` and logs "signed by the process credential" (`vended_credentials.py:120-121`).
  - The lander treats absent options as ambient (`findings_lance_lakekeeper.md:64`).
- **rask should:** adopt Lakekeeper's two rules together.
  1. A vend failure on a write path is a typed refusal plus a metric, never an ambient write.
  2. A dict without keys must not reach the default chain. Emit `aws_provider_scheme`-style explicit auth for vended dicts, verified on MinIO first (`findings_lance_lakekeeper.md:64`).
- **Rows:** CP-007, LH-129.

### T14. Which doors vend, and at which tier

- **Lakekeeper:** `loadTable`, `createTable`, `registerTable`, `loadCredentials` and generic-table `loadCredentials` all vend. Load vends the **highest tier the caller holds**; create and register vend RWD (see §0).
- **rask today:**
  - `describe` vends **read only** (`tables.py:449-476`).
  - `POST /management/v1/table/{id}/credentials?tier=` is non-spec, and no stock client calls it (`credentials.py:45-51`; `findings_lance_lakekeeper.md:66`).
  - `declare_table` **drops** the spec's `DeclareTableRequest.vend_credentials` (`spec.yaml:3828-3833`; `tables.py:244-300`, which never reads it).
- **rask should:**
  - Order the work so Lakekeeper parity becomes *safe*. Land the plane split (§0) first. Then `describe` with `vend_credentials=true` can vend the highest tier the caller holds, as Lakekeeper's load does, and `declare` can vend the writer tier to the declarer.
  - Stock lance-ray and pylance writers then work through `namespace_impl` with **no rask subclass**. The subclass was the audit's workaround for the over-wide write tier (`findings_lance_lakekeeper.md:66`).
  - Until the split lands, keep describe at read tier.
- **Rows:** new rows N7 (declare) and the audit's "stock clients get no credential" row; also LH-129 and CP-029.

### T15. Branch vending under D3 (recommended, not yet confirmed)

- **Lakekeeper:** Iceberg branches are refs inside one table location, and `WriteData` covers the whole location (`server/tables.rs:1164-1170`; `s3.rs:990`). This is exactly D3 option (a), "a table writer writes every branch", with branch *creation* as a separate management action.
- **rask today:**
  - A branch vend narrows the credential: read on main, write on `tree/<b>/*` (`vending.py:444-484`; `credentials.py:61-70`, `:119-120`). A branch that does not exist is refused.
  - LH-056 asks that a `can_write_data` holder on main must not write another branch.
- **rask should, under D3(a):**
  - Void that clause of LH-056. Record the ruling in `docs/DECISIONS.md` in the implementing commit.
  - Keep branch vending as an **opt-in narrowing** for least privilege, for example a Ray job scoped to its branch.
  - Keep T2's nested-name refusal, because an opt-in narrowing must still be correct.
  - Per-branch trash and undrop stay gated on LH-178, unchanged.
- **Rows:** LH-056, LH-178, D3.

### T16. The Ray and compute lane (LH-129, CP-001, CP-029)

- **Lakekeeper:** the engine authenticates itself (OIDC or K8s SA) and pulls table-scoped credentials plus a refresh endpoint from the catalog on each load. Nothing is baked into a job spec.
- **rask today:**
  - Ray jobs sign with `S3_KEY`/`S3_SECRET` from pod env (`rows_08.md` LH-129).
  - The static `rask-ray-compute` key is `arn:aws:s3:::*`-wide (`rows_10.md` CP-001).
  - CP-029 proposes a compute *submit door that vends*.
- **rask should:**
  - The job's only bootstrap credential is its **projected SA token file** (D1). It presents that token to the catalog vend path.
  - The job drives `lr.write_lance` / `lance.dataset` with `namespace_impl` / `namespace_client`, so Lance's provider refreshes mid-write (T6).
  - Then narrow the `rask-ray-compute` policy away.
  - **Re-scope CP-029's credential half.** A submit door that vends would bake a 900 s triple into the job body, the `runtime_env` anti-pattern CP-001 already removed. On Lakekeeper's model the submit door passes table ids and the job's identity binding, never credentials. Keep CP-029's idempotent-outcome and plan-document halves.
- **Rows:** LH-129, CP-001, CP-029, LH-160, XC-002.

### T17. The outbox stager's tier

- **Lakekeeper:** not applicable. The nearest principle is the unused no-delete ReadWrite tier.
- **rask today:** `POST /v1/outbox/credentials` vends `tier="write"`: Get, Put, **Delete** and AbortMPU over the whole outbox prefix (`api/v1/endpoints/outbox_credentials.py:88`). Five producers stage there (`outbox_credentials.py:31-34`), so a stager credential can delete every other producer's staged events.
- **rask should:** vend a put-only tier for staging. Allow Put and AbortMPU, plus Get only if the idempotency check needs it; allow no Delete. The relay, not the stager, deletes.
- **Rows:** CP-007 (it shipped this door), and new row N3b.

### T18. The Lance-native credential vendor in pylance 12

- **Measured:** the installed `lance/namespace.py:292-326` (pylance 12.0.0) documents a native `DirectoryNamespace` credential vendor. Its properties include:
  - `credential_vendor.enabled`
  - `credential_vendor.permission` = read, write or admin
  - `aws_role_arn`, `aws_external_id` and `aws_role_session_name`
  - `aws_duration_millis`, with a default of 1 h and a range of 15 min to 12 h

  It is marked "Requires the corresponding credential-vendor-* feature."
- The shipped `lance.abi3.so` contains **zero** occurrences of `credential_vendor` or `credential-vendor`. The method control: sibling property names `dir_listing_enabled`, `manifest_enabled` and `vend_input_storage_options` are all present. So the published wheel appears to be built **without** that feature. This is inferred from binary strings; a definitive test needs an `s3://` describe with the vendor enabled.
- **rask should:** keep its own vendor. Even if a future wheel ships the native vendor, its `permission` is a **static per-namespace-instance** setting, so it cannot express per-caller FGA tiers without one namespace per tier. It would still need a per-plane policy (§0). Revisit when the wheel changes.
- **Rows:** none; this is context for the §0 and T14 decisions.

---

## 2. Corrections to earlier audits and to in-repo prose

1. `findings_lance_lakekeeper.md:37`, "Lakekeeper attacks only its offline signer and checks STS vending for presence", is **wrong**. See T7 (`service/storage/mod.rs:508-813`, `:1742-1891`).
2. `findings_lance_lakekeeper.md:92` cites `crates/lakekeeper-integration-tests/tests/server_generic_table_signing.rs:618-647`, which **does not exist** at `a58e4017`. The sibling-prefix protection in Lakekeeper is the `/`-anchoring at `s3.rs:976-977` and `io/src/location.rs:193-205`.
3. `findings_lance_lakekeeper.md:53` cites `tabular/mod.rs:599-708`. In this checkout the function is at `crates/lakekeeper-storage-postgres/src/tabular/mod.rs:545-583`.
4. The rask prose "RustFS (this project's default store)" is false for the deployed estate (T3):
   - `vending.py:6`
   - `vending.py:13-15`
   - CLAUDE.md's State-surface line
5. `vending.py:650-656` (web identity is RustFS's native flow) contradicts `vending.py:17-28` (measured: RustFS answers `AssumeRole` and refuses web identity).
6. `credentials.py:5-7` and `chart/values.yaml:1009` name a `static` vending mode that does not exist (`vending.py:56`, `:723-764`).
7. `tests/unit/test_vending.py:344-347`, "`*`/`?` cannot be escaped", is false for IAM (`${*}`, `${?}`, `${$}`; `s3.rs:1194-1208`). For MinIO it is unmeasured.

## 3. Proposed new rows, or folds into existing ones

| id | title | where | fold into |
|---|---|---|---|
| N1 | The vendor vends its STS endpoint as the S3 data endpoint; on AWS, or any store with a separate STS port, every credential points at the STS host | `vending.py:590-642`; `main.py:146-150`; `config.py:103`, `:624-636` | LH-177 |
| N2 | The vend door and `/validate` use one process-wide vendor, so a warehouse with its own `endpoint`/`credential_ref` is vended by the estate store and key | `main.py:146-170`; `credentials.py:108-160`; `warehouses.py:119-124` | LH-067 follow-up / LD38 |
| N3 | The writer tier carries `DeleteObject`; `can_maintain` and `can_write_data` are byte-identical | `vending.py:125-131`; `credentials.py:79-107` | the audit's `_refs`/`_versions` row |
| N3b | The outbox stager credential can delete every producer's staged events | `outbox_credentials.py:88` | CP-007 |
| N4 | A writer-committed base with `*`/`?`, a bucket-root path, or `..` makes `build_session_policy` **raise**, so every reader's vend 500s. Unsanctioned bases are dropped, but these raise, and the raise is pinned by `test_vending.py:344-350`. Lakekeeper escapes instead (`s3.rs:976`). Fix: drop plus warn, like unsanctioned. | `vending.py:486-490`, `:492-503` | the `_refs`/`_versions` row (only a client-side commit can plant one) |
| N5 | `{`/`}` are allowed in segments, so `${…}` can appear in a policy Resource | `identifiers.py:54-70`; `dir/index.md:136-145` | LD19 |
| N6 | The scope and CAS probe runs only on demand, never at warehouse create or store registration | `warehouses.py:1029-1170` | XC-017 |
| N7 | `declare_table` drops the spec's `vend_credentials` | `tables.py:244-300`; `spec.yaml:3828-3833` | the audit's stock-client row |
| N8 | No server-side STS cache or single-flight: every vend is an `AssumeRole` plus a manifest open | `credentials.py:119-160` | LH-129 |
| N9 | The vend audit omits the temporary `AccessKeyId`, and the describe-path vend has no issuance audit, so store-side requests cannot be joined to a principal | `credentials.py:173-181`; `tables.py:449-476` | LIN-002 / XC-003 |
| N10 | SSE-KMS is vended without a KMS statement in the session policy | `vending.py:71-93`, `:381-527` | new (low) |
| N11 | `vending_ttl_seconds ge=60` is below AWS STS's 900 s minimum (MinIO unmeasured) | `config.py:296` | new (low) |
| N12 | Stale "RustFS default" and web-identity prose (§2, items 4-6) | as cited | new (low; fix with T3) |
| N13 | The vendors ignore `LANCE_S3_VIRTUAL_HOSTED`, as they ignore the endpoint scheme for `allow_http` | `vending.py:631-638`, `:710-717`; `config.py:88` | LH-096 |

## 4. What I did not check (bounded explicitly)

- **Not read in full:** `az/mod.rs` tests (from line 580), and `az/onelake_profile.rs`. For OneLake I read only the endpoint and SAS host logic (`:436-556`) and scanned the rest.
- **Tests only scanned:** in `storage_layout.rs` I read lines 1-450 in full and scanned only the test names from 450 on.
- **Not read in the io crate:** `adls*`, `gcs*`, `memory.rs`, `error.rs`, `lib.rs` bodies and `Cargo.toml`, and `crates/io/tests/integration_tests.rs`. I only grepped the last one for sibling-deletion safety. These are Azure, GCS and in-memory IO plus Lakekeeper-internal IO, none of which is rask's stack.
- **Partial reads in the io crate:** `s3_storage.rs` (lines 1-130 and 490-545 only), `location.rs` (lines 1-520), `s3_location.rs` (lines 1-140).
- **Read beyond the domain as needed:** `server/s3_signer/sign.rs` lines 1-820; the tests beyond that are unread.
- **No MinIO behaviour was measured:**
  - policy-variable and escape handling
  - session-policy size cap
  - `DurationSeconds` range
  - web identity with a k3s SA issuer
  - KMS actions
  - audit-log fields

  Each is marked where it gates a recommendation.
- **Not measured on pylance 12:**
  - which prefixes `write_fragments` puts to
  - the storage-options provider's refresh (taken from `findings_lance_lakekeeper.md:14`, `:67`)
  - whether a remote signer hook exists
- **Not re-verified live:** LH-177's vended endpoint `http://rask-minio:9000`, taken from the row; the chart render at `services.yaml:128,166` agrees.
- **Out of scope:** Lakekeeper's authn, authz and events domains, except where a vend door depends on them (T3, T6, T16).
