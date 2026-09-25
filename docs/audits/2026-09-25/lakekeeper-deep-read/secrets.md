# Lakekeeper deep read: SECRETS, and what rask should build on its own stack

Date 2026-09-25. Read-only: nothing in `/home/gabriel/Desktop/rask` was edited and no cluster call was made.
Lakekeeper paths are relative to `/home/gabriel/Desktop/lakekeeper-ref/`. `LK-chart/` means
`.../scratchpad/reaudit/lakekeeper-charts/charts/lakekeeper/`. rask paths are relative to the repo root.
"Audit" means `findings_lance_lakekeeper.md` (cited by line). "Recon" means `findings_reconciliation.md` (cited by step id).

## 0. Headline

Lakekeeper gets one thing right that rask should copy: **the storage credential is stored once and never
handed out.** Every consumer receives something derived from it, either an STS session scoped to one
table prefix at the permission level authz granted, or a remotely signed request. The catalog is the only
thing that reads the stored credential. Its secret-store seam is also good: by-id, immutable,
rotate-by-new-id, a TTL cache with single-flight loads and invalidation that wins against in-flight loads.

Lakekeeper is **not** the reference for how its own root secrets get delivered. The Postgres encryption
key, the Vault userpass password, the DB password and the OpenFGA client secret all arrive through
environment variables. Config is read only from the environment. The Vault login is a static password,
and the encryption key is one symmetric key with no key id and no rotation path. The owner's rule
("never secret through envs; ESO, Dapr secret store, or STS") is stricter than what Lakekeeper ships,
and rask is already ahead of it on authentication to the store: ESO uses OpenBao Kubernetes auth.

What rask should build, on Lance + lance-namespace, Dapr, OpenBao/ESO, OpenFGA and Dex:
1. **Exactly one static storage secret.** It is the catalog's STS parent pair. It lives in OpenBao,
   only the catalog's identity can read it, and it is re-read on a TTL.
2. **Every other storage access is an STS vend.** The caller authenticates with a projected ServiceAccount
   token (D1). Lance's own storage-options provider does the refresh, and nothing falls back to an ambient credential.
3. **Per-warehouse credentials follow the rotate-by-new-reference model.** Records name OpenBao paths, and a TTL
   single-flight cache holds what is resolved. This needs **wiring first**, because today the warehouse record's
   `credential_ref` is written by no door and read by no open or vend path (§T1, new finding).
4. **Every process authenticates to OpenBao with a Kubernetes ServiceAccount.** That means a per-app SA, a
   per-app OpenBao role and policy, and a policy-bound, renewable token behind the Dapr sidecar.
5. **Three delivery paths and no fourth.** A sidecar'd pod uses the Dapr store. A pod with no sidecar gets an
   ESO-written Secret as a mounted file that it re-reads. Object storage is reached with STS. The environment
   carries only file paths.

## 1. What Lakekeeper actually does (facts, from the files)

### 1.1 The seam: `SecretStore` (crates/lakekeeper/src/service/secrets.rs)
- The trait has three storage verbs and nothing else: `get_secret_by_id_impl`, `create_secret_impl` and
  `delete_secret_impl` (secrets.rs:107-117). **There is no update.** On top of them sit
  `require_storage_secret_by_id`, `create_storage_secret` and `delete_secret` (secrets.rs:53-105).
- Secrets are addressed by an opaque `SecretId(Uuid)` (secrets.rs:120-157). The warehouse row stores only
  that id (`storage_secret_id uuid`, crates/lakekeeper-storage-postgres/migrations/02_warehouse.sql:15).
- `Secret<T>` carries `created_at`/`updated_at` (secrets.rs:159-177). `SecretInStorage` is a marker trait
  that "Prohibits us to store unwanted types in the storage" (secrets.rs:179-183). Only
  `CachedSecret::StorageCredential` exists (secrets.rs:42-45).
- **Cache.** It is moka, capped at `cache.secrets.capacity`, with the TTL `time_to_live_secs` jittered
  downward (secrets.rs:31-40). The defaults are capacity 500 and TTL 600 s (crates/lakekeeper/src/config.rs:941-953;
  docs/docs/configuration.md:406-420). The docs give the rationale: "Since Lakekeeper never updates
  secrets, long TTLs can significantly increase resilience against secret store outages"
  (configuration.md:408).
- **Single-flight read-through.** Concurrent misses coalesce onto one backend fetch and decrypt. A missing
  secret is **not** negative-cached. A loader error is returned and is not cached (secrets.rs:237-287).
- **Deletes win against in-flight loads.** Invalidation goes through the loader's per-key compute lock
  (`Op::Remove`), "otherwise a delete racing an in-flight load lets the loader re-`Put` the removed secret
  until TTL (up to 600s of a decryptable stale credential)" (secrets.rs:194-208). A regression test pins
  it (secrets.rs:318-353).
- Hit, miss and size metrics are emitted (secrets.rs:185-235).

### 1.2 Backend A: Postgres, encrypted in the DB (crates/lakekeeper-storage-postgres)
- Table `secret(secret_id uuid pk, secret bytea, created_at, updated_at)` (migrations/03_secret.sql:1-9).
- **Write:** `pgp_sym_encrypt($1, $2, 'cipher-algo=aes256')`, where `$2 = CONFIG.pg_encryption_key`
  (src/secrets.rs:127-135). **Read:** `pgp_sym_decrypt(...)` with the same key (src/secrets.rs:64-76).
  This needs the `pgcrypto` extension (configuration.md:76-88).
- **One key, no key id, no rotation path.** The key is a plain `String` config field
  (src/config.rs:13-16). Its default is the published literal `"<This is unsafe, please set a proper key>"`
  (src/config.rs:6-9, 35-38). The binary only **warns** when the default is in use (crates/lakekeeper-bin/src/serve.rs:130-139).
  The chart says so directly: "If you loose the key, you loose access to all secrets"
  (LK-chart/values.yaml:370-373).
- Error hygiene: a parse error on a decrypted secret is logged by id only, "as it might contain sensitive
  information" (src/secrets.rs:92-102).
- Config comes **only from env** through figment `Env::prefixed("LAKEKEEPER__")` (src/config.rs:108-125).

### 1.3 Backend B: Vault KV v2 (crates/lakekeeper-secrets-kv2)
- **How the process authenticates to the store: userpass**, with `user` and `password` from config
  (src/config.rs:44-51). Config is env-only (src/config.rs:53-70). Login uses `UserpassLogin` (src/lib.rs:174-179).
  The docs say "Currently, we only support the `userpass` authentication method" (configuration.md:101-111).
- **Token lifecycle.** A background task re-logs in at `lease_duration - 10 s` and retries every 1 s on
  failure (src/lib.rs:181-222). The client token sits in an `RwLock<VaultClient>` (src/lib.rs:132-142).
- **Layout.** The key is `secret/<uuid>` under a configured mount (src/lib.rs:265-267). The id is `Uuid::now_v7()`
  (src/lib.rs:96). A read is `read_metadata` followed by `read_version(current_version)`, because "there is no
  atomic get for metadata and secret" (src/lib.rs:30-89). A delete is `delete_metadata`, which destroys every
  version (src/lib.rs:115-129).
- **Health.** `sys/health` feeds `HealthExt` (src/lib.rs:225-251). `Debug` redacts the password
  (src/lib.rs:253-263). `KV2Config` derives `veil::Redact` on `password` (src/config.rs:44-51).
- **CI runs the real backend.** It starts a Vault dev server, enables userpass, writes a policy on `secret/*` and
  creates a user (.github/workflows/unittests.yml:209-222, 319-351). Integration tests then create, read and
  delete against it (src/lib.rs:285-352).

### 1.4 What is stored, and the management-API lifecycle
- The stored thing is the warehouse's `StorageCredential`. For S3 there are three shapes. `AccessKey` holds a
  key id, a secret and an optional `external_id`. `AwsSystemIdentity` holds only an `external_id` and no secret
  at all. `CloudflareR2` holds a key pair plus an API token (crates/lakekeeper/src/service/storage/s3.rs:203-256).
  Every secret field is `#[redact(partial)]` (s3.rs:215-256).
- **The API accepts credential material in the request body** (`storage_credential` on create and update-storage,
  `new_storage_credential` on update-credential) (crates/lakekeeper/src/api/management/v1/warehouse/mod.rs:110, 219, 337-341).
- **It validates before storing.** `storage_profile.validate_access(credential)` runs, in parallel with an overlap
  check, before the secret is written (mod.rs:461-477, 1191-1197, 1317-1322).
- **Responses never echo the credential.** They carry only `storage_credential_type` (mod.rs:1728-1749).
  `resolve_credential_type` looks the secret up only to report its type, and logs a failure without failing
  (mod.rs:1751-1774).
- **Rotation is by new id plus a pointer swap.** Update creates a new secret, swaps `storage_secret_id` in the
  catalog transaction, commits, and then **best-effort** deletes the old secret ("never fail the request if the
  deletion fails") (mod.rs:1217-1254 for storage, 1315-1363 for credential). The SQL is
  `UPDATE warehouse SET storage_profile = $1, storage_secret_id = $2` (crates/lakekeeper-storage-postgres/src/warehouse.rs:828-858).
- **Two lifecycle gaps (Lakekeeper's, not to copy).**
  - (a) The secret is written *outside* the catalog transaction. The Postgres backend uses its own write pool
    (src/secrets.rs:127-136), and KV2 is another system entirely. A create or update that later fails leaves an
    orphaned secret (mod.rs:482-516).
  - (b) `delete_warehouse` deletes the row and the authz object but **never the secret** (mod.rs:675-729;
    warehouse.rs:553-561 is a bare `DELETE FROM warehouse`, and `secret` has no FK to it).
- **Events cannot carry the credential.** `UpdateWarehouseCredentialRequest` is `Deserialize` only
  (mod.rs:334-341), so it cannot be serialized onto a bus. The in-process event carries `old_secret_id`, an id
  (crates/lakekeeper/src/service/events/types/warehouse.rs:83-90). A grep of
  `service/events/publisher.rs` and `service/events/backends/audit.rs` found no warehouse-storage or credential
  payload. (Bounded: grep only; the publisher was not read in full.)

### 1.5 Consumers never see the stored secret: derived credentials
- **The permission level comes from authz.** `can_write` gives `ReadWriteDelete`, `can_read` gives `Read`,
  otherwise nothing (crates/lakekeeper/src/server/tables.rs:1164-1172).
- **Vended STS.** It calls `AssumeRole` with an inline session policy scoped to `arn:aws:s3:::<bucket>/<table-key>/*`
  and a prefix-conditioned `ListBucket` (s3.rs:940-1010). The TTL is `sts_token_validity_seconds` (s3.rs:816-819).
  It supports an `external_id` and session tags (s3.rs:836-858). Clients get the triple plus an expiry and a
  refresh endpoint (s3.rs:532-578).
- **Remote signing** is the alternative: the client gets a signer URI, never a key (s3.rs:580-594). The signer
  loads the stored secret server-side (crates/lakekeeper/src/server/s3_signer/sign.rs:238, grep only).
- **R2** mints down-scoped temporary credentials through Cloudflare's API from the stored token (s3.rs:656-756).
- **STC cache.** The key is (request, storage-profile hash, **credential hash**) (crates/lakekeeper/src/service/storage/cache.rs:28-62),
  so a rotated parent credential is a new key by construction. An entry is served for half its remaining
  lifetime (cache.rs:80-102; configuration.md:362 gives "capped at 1 hour"). Loads are single-flight and errors are
  never cached (cache.rs:138-210).
- **The zero-static-key parent (AWS system identity).** The pod's own cloud identity (IRSA, via the SA
  annotation example at LK-chart/values.yaml:355-360) signs the STS call. It is **off by default**
  (`ENABLE_AWS_SYSTEM_CREDENTIALS=false`). It requires an `assume-role-arn`, and by default an `external-id`
  (configuration.md:44-48; enforced at s3.rs:776-798 and 888-895).

### 1.6 Delivery into the pod (the chart): what does and does not reach env
- **Everything root-level is an env var**, either from `secretKeyRef` or from an `envFrom` of a chart-rendered
  Secret:
  - `LAKEKEEPER__PG_ENCRYPTION_KEY` (LK-chart/templates/_helpers.tpl:223-238)
  - `LAKEKEEPER__KV2__USER` and `__PASSWORD` (_helpers.tpl:240-261, or literally in secret-config-envs.yaml:87-96)
  - `LAKEKEEPER__PG_PASSWORD` (_helpers.tpl:196-221; literal at secret-config-envs.yaml:37-39)
  - the OpenFGA client secret (_helpers.tpl:272-294; secret-config-envs.yaml:111-119)
  - the envFrom (_helpers.tpl:104-110)
- **Key generation is lookup-or-random.** If no `encryptionKeySecret` is given, the chart looks up the live
  Secret, else `randAlphaNum 40`, and marks it `helm.sh/resource-policy: keep` (LK-chart/templates/config/db-encryption-secret.yaml:1-21).
  values.yaml warns that this is incompatible with Argo CD (values.yaml:374-376). production.md steers to a
  pre-created secret (docs/docs/production.md:18).
- **Production checklist** (production.md:15-18): set a long random `PG_ENCRYPTION_KEY`, use "distinct storage
  locations / prefixes and distinct credentials that only grant access to the prefix used for a Warehouse", and
  pre-create the encryption-key secret.
- **The ServiceAccount.** The chart creates a dedicated SA whose Role grants only `batch/jobs get/list/watch`.
  It binds `system:auth-delegator` only when k8s authn is on (LK-chart/templates/serviceaccount.yaml:1-80).
- **What therefore never appears in env in Lakekeeper:** per-warehouse storage credentials (they live in the
  secret backend and arrive through the API), vended STS triples (minted per request), and the AWS system
  identity (the SDK reads the projected token FILE). **What does appear:** every credential Lakekeeper needs to
  reach its own stores.

## 2. Where Lakekeeper is NOT the reference (do not import)

| Lakekeeper choice | Why rask must not copy it | rask's stack answer |
|---|---|---|
| Root secrets as env vars (§1.6) | Violates the owner's rule verbatim | Dapr secret store, ESO-to-file, or STS |
| Vault userpass with a static password from env (§1.3) | A static secret used to fetch secrets. rask's ESO hop is already better | OpenBao **Kubernetes auth** per ServiceAccount (D1's model applied to the store) |
| `pgp_sym_encrypt` with one app-held key, no key id (§1.2) | This is Lakekeeper's Postgres stack choice. rask has no relational DB for this, and an app-held key is one more env secret | OpenBao's own barrier is the at-rest encryption. Its root of trust is the unseal (XC-006) |
| Lookup-or-random minting in the chart (§1.6) | Rotates on every cluster-less render, and material rides the Helm release object | Generate inside OpenBao (create-if-absent) and consume through ESO or Dapr |
| Warn-only on a default key (serve.rs:132-139) | Too weak | rask already **refuses the render** (chart/templates/prod-credentials.yaml:39-73) |
| API accepts credential bodies (§1.4) | Material would transit catalog request bodies and logs | Records **name** an OpenBao path, and material is written to OpenBao out-of-band (Audit line 31: "add no API that accepts credentials") |
| Secret orphaned on rollback and on warehouse delete (§1.4) | A leak | Tie path lifetime to the record (§T11) |

## 3. Topics: Lakekeeper, rask today, what rask should do, rows

### T1. The secret-store seam and the warehouse reference (a NEW finding here)
- **Lakekeeper:** the warehouse row holds `storage_secret_id`, and every consumer resolves it through
  `require_storage_secret_by_id`. Examples: table create (crates/lakekeeper/src/server/tables/create_table.rs:293),
  view load (server/views/load.rs:104) and the signer (s3_signer/sign.rs:238), all grep only.
- **rask today:**
  - The pieces exist. `WarehouseResponse.credential_ref` names a secret (services/catalog/src/catalog/schemas.py:769-773).
    The registry merge treats `credential_ref` as caller-owned (services/catalog/src/catalog/services/warehouses.py:115-124).
    A resolver `warehouse_credentials.resolve` fetches through the Dapr door and fails closed
    (services/catalog/src/catalog/services/warehouse_credentials.py:41-61). Tests pin the resolver
    (services/catalog/tests/test_a_warehouse_names_its_credential_never_carries_it.py:35-136).
  - **But the record field is disconnected from both ends.**
    - (a) No door sets it. `CreateWarehouseRequest` has `endpoint` and no `credential_ref` (schemas.py:714-748), and
      the create door builds the record with `endpoint` only (services/catalog/src/catalog/api/v1/endpoints/warehouses.py:205-224).
    - (b) Nothing reads it. `_resolve_warehouse_root` returns only `(root_uri, endpoint)` (services/catalog/src/catalog/api/dependencies.py:43-125),
      and `build_namespace_for_root` says "Same impl and object-store CREDENTIALS as the default connection ...
      CREDENTIALS ARE NOT OVERRIDABLE HERE" (services/catalog/src/catalog/core/namespace.py:36-58).
    - A grep of `services/` and `packages/` for `credential_ref` outside tests finds only schemas.py:773 and
      warehouses.py:124 on the warehouse path.
  - The only consumed per-credential reference is the operator env map `LANCE_MULTIBASE_BASE_CREDENTIAL_REFS`,
    and it applies to data bases (services/catalog/src/catalog/core/config.py:462-495; services/catalog/src/catalog/services/dataplane.py:263-291).
  - So a warehouse that names a second store's `endpoint` opens against that endpoint **with the estate's
    credential**. The resolver tests pin the innermost call and nothing pins the door (memory:
    "a gate on the innermost call proves nothing"). Recon P2.9 already carries the "warehouse endpoint
    redirects opens" refusal half, which this compounds.
- **rask should:**
  - Make `credential_ref` settable at the create and re-POST door, next to `endpoint`, and **require** one
    whenever `endpoint` is not the estate's. A foreign endpoint with the estate key must be refused, never defaulted.
  - Thread it through `_resolve_warehouse_root` → `namespace_for_root` (key the cache on `(root, endpoint, ref)`)
    → `build_namespace_for_root` → the vendor. A second store needs its own STS parent, so the vendor must be
    per-store, not a lifespan singleton.
  - Probe before commit, like Lakekeeper's `validate_access`. rask already has a stronger probe in
    `POST /warehouses/{id}/validate` (endpoints/warehouses.py:1029-1060): run it on create when a ref is set.
  - RED test at the outer door: a warehouse with `endpoint=X, credential_ref=R` opens with R's pair, and
    `endpoint=X` without R is refused.
- **Rows:** new (LH-067 residue: "warehouse credential_ref is written by no door and read by no open/vend path").
  Adjacent to LH-177 (what vending means off-cluster) and Recon P2.9.

### T2. Caching and rotation of store-fetched secrets
- **Lakekeeper:** secrets are immutable and rotation is a new id (§1.1, §1.4). The cache TTL is 600 s jittered with
  single-flight loads, no negative caching, and delete-wins invalidation (secrets.rs:31-40, 194-287). The STC cache
  keys on the parent credential's hash (cache.rs:28-62).
- **rask today:**
  - `warehouse_credentials.resolve` is `@lru_cache(maxsize=256)` with **no TTL**. Its docstring names `cache_clear()`
    as "the seam a rotation would use", and nothing calls it (warehouse_credentials.py:16-19, 41).
  - The estate S3 secret is spliced into the `lru_cache`d Settings **once** in the lifespan
    (packages/service-kit/src/service_kit/governed/secrets.py:147-194; services/catalog/src/catalog/main.py:66-79, 120).
  - The STS vendor captures the parent pair at construction and builds its boto3 client once
    (services/catalog/src/catalog/core/vending.py:593-608; main.py:146-157). So a rotated parent key is invisible
    until the pod restarts (Audit line 78).
  - **New detail:** the request-path resolver inherits the **boot** retry budget. `fetch_required_secrets` calls
    `fetch_dapr_secret` with its defaults (10 attempts, backoff up to 15 s, "worst case ≈2 min")
    (secrets.py:65-84, 117-128), and `resolve` uses those defaults (warehouse_credentials.py:53). A store outage
    therefore stalls every warehouse open for about 2 min, where ingest and viewer deliberately pass `retries=1`
    on their request paths (services/ingest/src/ingest/objectstore.py:179-183; services/viewer/src/viewer/api/v1/endpoints/objects.py:102).
    Lakekeeper returns the loader error immediately and does not cache it (secrets.rs:267-276).
  - rask has no STS session cache at all: every vend is a fresh `AssumeRole` (vending.py:611-646). A grep for
    `lru_cache|TTLCache|cachetools` in the vending and credential doors returns nothing.
- **rask should:**
  - Replace the `lru_cache` with a bounded TTL cache (300-600 s, jittered downward) keyed `(store, ref, field)`,
    with single-flight per key, no negative caching, request-path retries of at most 1, and fail closed.
  - Move the estate parent pair out of `Settings` into a credential holder the vendor and the `storage_options()`
    builders read, with the same TTL. Rebuild the STS client when the holder's value changes.
  - Adopt rotate-by-new-reference for warehouses: write the new material at a new OpenBao path, CAS the record's
    `credential_ref` (the registry is already ETag-conditional, services/catalog/src/catalog/services/warehouses.py:100-113),
    and delete the old path after commit. A new ref is a new cache key, so the change is immediate, and the TTL
    bounds any in-place overwrite.
  - If an STS cache is added, include the parent credential's identity or version in the key (Lakekeeper's
    `credential_hash`), cap it at half the session TTL, and never cache errors.
- **Rows:** new (Audit line 78, "A rotated S3 key in OpenBao is invisible until the pod restarts"; Recon P4.6).
  XC-001 covers the file-mount half only.

### T3. Encryption at rest and the "encryption key" analogue
- **Lakekeeper:** app-level `pgp_sym_encrypt` with one env-held key, a default literal and no rotation (§1.2).
- **rask today:** material lives in OpenBao KV v2 (`enginePath: secret`, chart/templates/dapr-component.yaml:370-375).
  The app holds no envelope key. The equivalent root of trust is OpenBao's seal. In dev that is
  `server -dev` (chart/templates/openbao.yaml:103), and in prod it is a manual unseal with no auto-unseal (XC-006).
- **rask should:** build no app-level symmetric encryption. Treat the unseal key as the estate's
  `PG_ENCRYPTION_KEY`: auto-unseal through KMS or transit when a prod estate exists, a sealed-status alert, and never an env var.
- **Rows:** XC-006 (parked LOW by the 2026-09-21 ruling), XC-031 (install runbook: unseal ordering).

### T4. How each process authenticates to the secret store
- **Lakekeeper:** KV2 userpass with a static password from env, re-logging in at lease minus 10 s (§1.3). Postgres
  backend: DB credentials from env (§1.6).
- **rask today:**
  - ESO authenticates with **OpenBao Kubernetes auth** (chart/templates/external-secrets.yaml:19-24). The role is
    provisioned with an enumerated read-only policy and `ttl=1h` (chart/templates/openbao.yaml:337-390).
  - **Every daprd authenticates with one static `vaultToken`.** In dev mode that is the root dev token as a literal
    value (dapr-component.yaml:356-357). Otherwise it is `<release>-openbao-token` by `secretKeyRef`, created out of band
    (dapr-component.yaml:359-368).
  - Per-app isolation is therefore a Dapr `Configuration` deny-list with `defaultAccess: allow`
    (chart/templates/observability.yaml:99-146), not OpenBao policy.
  - The seed Job holds the root token as literal env (`BAO_TOKEN`, openbao.yaml:176).
  - Workloads run as the `default` SA (`security.serviceAccounts.enabled: false`, chart/values.yaml:727-728), so no
    per-app OpenBao identity is possible yet (Audit line 49).
  - Whether Dapr's Vault component can do Kubernetes auth or renew its token was **not verified in-session**. No
    components-contrib source was available locally, and Audit line 81 cites vault.go:399-419: token or token-file,
    read once, never renewed.
- **rask should:** this is the Lakekeeper k8s-authn model applied to the store rather than to the catalog.
  1. One SA per workload (flip `serviceAccounts.enabled`, `automountServiceAccountToken: false` except where a projected
     token is mounted), and remove the Dapr `secretReader` binding on `default` (Audit line 49).
  2. One OpenBao Kubernetes-auth role per SA, with a policy on that app's own paths only (`secret/<identity>-*`).
     The seed authenticates the same way instead of `BAO_TOKEN=root` (Recon P4.2).
  3. For daprd: a policy-bound periodic token per app, minted by ESO (`VaultDynamicSecret`) or by a login using the app's
     SA, delivered by `vaultTokenMountPath`. One `lance-secrets` Component per app-id, so isolation lives in OpenBao
     policy. Re-read on rotation, or restart deliberately. Measure which one Dapr does before designing around it.
  4. Keep the deny-list only as defence in depth.
- **Rows:** new (Audit lines 49 and 81; Recon P4.2, P4.3), LH-168 (Component-scope drift), XC-005 (seed and ESO ordering), LH-160.

### T5. Delivery: what may never appear in env
- **Lakekeeper:** everything root-level appears in env (§1.6). This is the anti-reference.
- **rask today:**
  - The ratchet stands at `SECRET_ENV_BASELINE = 23` and `WITH_SIDECAR_BASELINE = 1`, with one `envFrom` exception
    (greptime) and `compute` recorded as `_UNREACHABLE_STORE` (tests/unit/test_secret_env_delivery_only_shrinks.py:1-30, 45, 50, 133, 225).
  - Sidecar'd lakehouse pods read from the store at boot (secrets.py:1-11).
  - Zones take the lineage token as a mounted file re-read per call (XC-001 row text).
  - The Ray head still carries 6 `secretKeyRef` entries plus `S3_KEY` as a value (deploy/ray-lance-demo.yaml:67, 74-86, 102-129),
    and the job reads `S3_KEY`/`S3_SECRET` from env (scripts/ray_stage_job.py:84-89).
  - `docs/DECISIONS.md` has no ruling on `secretKeyRef`: a grep for `secretKeyRef` returns 0 (XC-002 evidence, re-grepped).
- **rask should:**
  - State the rule as three delivery paths, which is the posture the owner described:
    1. a pod with a sidecar reads the Dapr secret store;
    2. a pod without a sidecar gets an ESO-written Secret as a **mounted file**, narrowed with `items`, re-read per use, with the env carrying the path;
    3. anything reaching object storage holds an STS session.
  - Recon P0.2 reads the owner's verbatim rule as requiring ESO-to-file. Write that in DECISIONS in the implementing
    row's commit, and make the gate count every manifest the estate applies (chart + `deploy/*.yaml`, both value shapes).
  - Third-party images (greptime, age, minio, openfga) either take `*_FILE` or a mounted config, or are recorded as named residue.
- **Rows:** LH-160, XC-002, XC-001, XC-004, LH-161, LH-129.

### T6. Where secret material is generated
- **Lakekeeper:** lookup-or-random in the chart with `resource-policy: keep` (db-encryption-secret.yaml:1-21). The
  docs say to pre-create (production.md:18).
- **rask today:** the same pattern for service tokens and the Ray auth token (chart/templates/_helpers.tpl:82-92, 1462-1480).
  `dapr.appToken` is rendered from a chart value (chart/templates/dapr-app-token.yaml:29-35). The render refuses
  published dev values on a real deployment (prod-credentials.yaml:39-73).
- **rask should:** generate inside OpenBao with an idempotent create-if-absent step in the seed hook, or ESO's
  Password generator plus PushSecret. Consumers receive the value only through ESO or Dapr. The chart never renders material,
  and on a real deployment with no store the render `fail`s. Most `service-token-*` disappear once D1 lands (projected SA tokens).
- **Rows:** XC-004, new (Audit line 79; Recon P4.4, P4.5), XC-005.

### T7. Consumers get derived, scoped, short-lived credentials (the core zero-trust move)
- **Lakekeeper:** an STS session per table location at an authz-derived permission, or remote signing, with a
  refresh endpoint and an expiry. The stored secret never leaves the catalog (§1.5).
- **rask today:**
  - `StsVendor` does `AssumeRole` with a per-table, per-tier, per-branch session policy (vending.py:547-646). The tier
    comes from FGA (vending.py:33-34). Mode B vends nothing and serves data server-mediated (vending.py:29-31), which
    is rask's analogue of remote signing.
  - Consumers that **bypass** it:
    - the Ray lane uses a static key from env (CP-001, LH-129);
    - maintenance falls back to the ambient credential whenever a vend is absent (services/maintenance/src/maintenance/services/credentials.py:101-126);
    - stock Lance clients opening through the namespace get no write-tier vend (Audit line 66);
    - ingest's estate-default and secretless stores run on the ambient chain (CP-007).
  - `VendedCredentialCache` refreshes only between Lance calls (packages/service-kit/src/service_kit/lakehouse/vended_credentials.py:91-111; Audit line 67).
- **rask should:**
  - Make the catalog the only holder of the parent. Every other workload authenticates to the catalog with a
    projected SA token (D1), which is exactly Lakekeeper's k8s authenticator. It gets storage only through the spec
    `DescribeTable(vend_credentials)` path or the credentials door.
  - Let Lance's own `LanceNamespaceStorageOptionsProvider` do mid-operation refresh (`namespace_client=` /
    `namespace_impl=`). Build no hand-rolled cache (Audit line 14, 67).
  - Emit `aws_provider_scheme: token` on every explicitly credentialed dict. Turn "nothing vended" on a write path into
    a typed refusal plus a metric (Audit line 64; verify on the deployed store first, as the audit notes).
  - Derive `allow_http` from the endpoint scheme in the one builder (Audit line 87).
- **Rows:** LH-129, CP-001, CP-007, CP-029 (compute's credential-vending submit door), LH-177, LH-056 (branch-blind vending), new (Audit lines 64, 66, 67, 87).

### T8. A parent credential with no static key (Lakekeeper's "system identity")
- **Lakekeeper:** `AwsSystemIdentity` is the pod's cloud identity, via IRSA's projected token file. It is off by
  default, and it is guarded by a required `assume-role-arn` and `external-id` (§1.5).
- **rask today:** the parent is a static pair from OpenBao (main.py:146-157; vending.py:593-597). RustFS answers
  `AssumeRole` but **not** `AssumeRoleWithWebIdentity`, and that was measured, with a control, on the pinned tag
  (vending.py:17-28). So on the default store a web-identity parent is impossible, and one static pair is irreducible.
  `WebIdentityVendor` exists for stores that support it (vending.py:649-721).
- **rask should:**
  - Own the irreducible key honestly. Exactly **one** bootstrap storage secret, the catalog's STS parent, lives on an
    OpenBao path that only the catalog's role can read (split `secret/lance` per consumer, Recon P4.3). It is refreshed
    by TTL (T2) and never leaves the catalog process.
  - On a store that supports web identity (AWS, MinIO), let the parent itself be the catalog's projected SA token through
    `WebIdentityVendor`, the Lakekeeper system-identity translation. That makes the estate zero-static-key there.
  - Keep Lakekeeper's guard: a system/web-identity parent is opt-in, and the role is pinned.
- **Rows:** CP-001, LH-160, new (Recon P4.3 bundle split, P8.x workload credential door).

### T9. Typing and redaction of secret-bearing values
- **Lakekeeper:**
  - `veil::Redact` on every credential struct (s3.rs:215-256) and on `KV2Config` (kv2 config.rs:44-51).
  - A hand-written redacting `Debug` (kv2 lib.rs:253-263).
  - Secret parse errors are logged without content (postgres secrets.rs:92-102).
  - The credential-carrying request type cannot be serialized (§1.4).
- **rask today:** `Settings.s3_secret_access_key: SecretStr` (secrets.py:140-144). `VendedCredentials.storage_options: dict[str, str]`
  mixes key, secret, token and config in one untyped mapping (vending.py:59-68), and there is no redacting container in
  service-kit or the catalog (LH-072). The vend log lines deliberately never print the credential (vended_credentials.py:112-128).
- **rask should:** keep the **wire** spec-shaped, since `storage_options` is flat with `expires_at_millis` inside it
  (Audit line 66 cites spec.yaml:2885-2891). Internally, carry a typed model whose secret fields (`aws_access_key_id`,
  `aws_secret_access_key`, `aws_session_token`) are `SecretStr` and whose config fields are plain. It needs an explicit
  `as_storage_options()` at the Lance boundary and a `__repr__` that redacts. Add a unit test that `repr()` and a structured log of a vend contain no secret.
- **Rows:** LH-072.

### T10. Should a management API accept credentials?
- **Lakekeeper:** yes, in create and update bodies, validated by a live probe before storing (§1.4).
- **rask today:** records name references only, and there is no credential field "by type" (schemas.py:769-773; test at
  test_a_warehouse_names_its_credential_never_carries_it.py:35-43). The Audit ruling is "add no API that accepts credentials"
  (Audit line 31). Estate Settings shows a "credentials" row with no store behind it (FE-011).
- **rask should:** keep references only. The operator flow is: write the material to OpenBao (CLI or ESO PushSecret), set
  `credential_ref` at the warehouse door, and let the door probe (`/validate`) before the CAS write. FE-011's
  "credentials" row becomes a reference picker plus a probe result, never a form that takes a secret.
- **Rows:** FE-011, T1's new row.

### T11. Secret lifecycle hygiene (orphans)
- **Lakekeeper:** leaks the secret on warehouse delete and on a failed create (§1.4b, §1.4a). Old secrets are deleted best-effort after a swap.
- **rask today:** material is written out-of-band, so a deleted warehouse record leaves whatever OpenBao path it named.
  Nothing links a path to its referrers. (Not measured: no OpenBao listing was taken.)
- **rask should (LOW):** a naming convention (`secret/wh-<warehouse-id>-v<n>`) plus a maintenance sweep that reports
  unreferenced `wh-*` paths. Report them; do not auto-delete, because deleting a secret is not undoable.
- **Rows:** new, LOW.

### T12. Health of the secret store
- **Lakekeeper:** Vault `sys/health` feeds the `/health` chain (kv2 lib.rs:225-251). The docs make `/health`
  dependency-driven (production.md:17).
- **rask today:** a boot fetch fails closed with retries (secrets.py:65-128). No runtime readiness signal for the store
  was found by grep. (Bounded: the service-kit health modules were not read.)
- **rask should:** once the TTL cache (T2) exists, expose its last-refresh outcome as a metric and a sealed or unreachable
  alert (ties to XC-006's alert). A failed refresh keeps serving the cached value until TTL, then fails closed.
- **Rows:** folds into the T2 new row and XC-006.

### T13. API-key principals
- **Lakekeeper:** none. It accepts JWT bearers only, from OIDC or Kubernetes SA tokens, and "opaque tokens are not yet
  supported" (docs/docs/authentication.md:9; configuration.md:185-236). A grep for `x-api-key|api_key` in
  crates/lakekeeper/src finds only the OpenFGA client key.
- **rask today:** `x-api-key` is read nowhere, while an audit doc still prescribes it (LH-079).
- **rask should:** withdraw the api-key principal. Machines authenticate with projected SA tokens (D1) and humans with Dex
  OIDC, so there is no key store and no rotation model to build.
- **Rows:** LH-079.

### T14. CI exercises the real secret path
- **Lakekeeper:** a Vault dev server in CI, with a scoped policy and a userpass identity (unittests.yml:209-222, 319-351).
- **rask today:** the hermetic governed lanes boot the catalog with S3 credentials in env. The store-only boot runs only on
  the live estate (Audit line 85).
- **rask should:** in Dagger, run `bao server -dev` plus a standalone daprd with a scoped `secretstores.hashicorp.vault`
  component. Boot the catalog with `LANCE_SECRETS_FROM_DAPR=true` and no S3 secret in env, then assert three things: it
  serves; a peer app-id is refused the catalog's path; and boot fails closed without the key. Add T2's rotation RED test on the same stack.
- **Rows:** new (Audit line 85), LH-160.

### T15. Events and work orders never carry material
- **Lakekeeper:** the credential request type cannot be serialized, and events carry `old_secret_id` (§1.4).
- **rask today:** `WorkOrder.credential_ref` names a credential and never carries one
  (packages/service-kit/src/service_kit/lakehouse/work_order.py:15, 146, 190). A test enforces it
  (tests/unit/test_ray_submissions_carry_no_secret_estatewide.py, name only). `RASK_CREDENTIAL_REF` has no consumer (LH-129).
- **rask should:** keep that. When LH-129 lands, `RASK_CREDENTIAL_REF` becomes the table id the Ray job vends for through
  the namespace provider (T7), not an OpenBao path the job reads.
- **Rows:** LH-129.

## 4. Row map (summary)

| Row | Topic | Verdict from this read |
|---|---|---|
| LH-160 | T5, T4, T8 | Lakekeeper is the anti-reference on delivery. Finish per T5's three paths. `compute` goes into scopes after the bundle split. |
| XC-002 | T5 | Record ESO-to-file as the reading of the owner's rule (Recon P0.2), and move the Ray head's six entries to a mounted file plus STS. |
| XC-001 | T2, T5 | File mounts re-read per use cover K8s-Secret rotation. OpenBao-side rotation needs T2's TTL holder (new row). |
| XC-004 | T6 | Generate in OpenBao; no value-rendered `dapr.appToken`. |
| XC-005 | T4, T6 | The seed becomes an idempotent upgrade hook that authenticates by k8s auth, not a root token. |
| XC-006 | T3, T12 | The unseal key is the estate's "encryption key". Auto-unseal plus a seal alert when prod exists. |
| LH-161 | T5 | Third-party `envFrom`: override or record the ruling. Lakekeeper offers nothing here. |
| LH-168 | T4 | Per-app Components or policy make scope drift visible. Add a render-vs-live diff. |
| LH-072 | T9 | Typed internal container with `SecretStr` fields, spec-flat on the wire. |
| LH-079 | T13 | Withdraw api-keys (Lakekeeper is JWT-only). |
| LH-129, CP-001 | T7, T8, T15 | Vend through the namespace provider with a projected SA token. No static key in env. |
| CP-007 | T7 | Registered stores declare references. Refuse the ambient chain. |
| CP-029 | T7 | compute's submit door vends via the catalog; it never forwards a key. |
| LH-177 | T1, T7 | A vend must name an endpoint the caller can reach or refuse. Per-warehouse endpoint and credential wiring (T1) is a prerequisite. |
| LH-056 | T7 | Branch-scoped vending (policy prefix issues per Audit line 51). |
| FE-011 | T10 | A reference picker plus probe, never a secret form. |
| XC-025 | T5, T6 | Dex's client secret out of the ConfigMap, into ESO. |
| XC-031 | T3 | Runbook: unseal, then seed, then ESO ordering. |
| **NEW-a** | T1 | Warehouse `credential_ref` is set by no door and consumed by no open/vend path. A foreign `endpoint` is opened with the estate key. |
| **NEW-b** | T2 | Store-fetched secrets are cached forever (`lru_cache`, a boot splice, the vendor's captured pair), and the request-path resolver uses the ~2 min boot retry budget. |
| **NEW-c** | T4 | Every daprd shares one static OpenBao token, and pods run as `default` SA (Audit lines 49 and 81). Per-SA OpenBao roles. |
| **NEW-d** | T6 | Chart-minted lookup-or-random secrets (Audit line 79). |
| **NEW-e** | T14 | CI never runs the store-only boot (Audit line 85). |
| **NEW-f** | T11 | Orphaned OpenBao paths are not reported (LOW). |

## 5. What I did not check (explicit scope bounds)
- **Lakekeeper Azure and GCS credential types** and their SAS / GCP-STS vending (`service/storage/az`, `gcs`): not read.
- **Lakekeeper `server/s3_signer/sign.rs`:** only the grep hit at :238. The events publisher and audit backend: grep only.
  `crates/lakekeeper/src/service/authn.rs` (the k8s authenticator): not read, because D1 belongs to another domain agent.
- **docs/docs/configuration.md:** lines 1-300 read in full. Lines 300-858 through grep context, which includes the whole
  caching section (348-471). docs/docs/storage.md and authentication.md: not read beyond the cited lines.
- **Dapr `secretstores.hashicorp.vault`** auth methods, token renewal and `vaultTokenMountPath` re-read behaviour: **not
  verified**. The source is not available locally, and I rely on Audit line 81's cite (vault.go:399-419). Measure before
  designing T4 step 3.
- **Live state:** no kubectl or OpenBao read was taken. The live ESO state, the OpenBao policies as deployed, and whether
  `externalSecrets.enabled` is on in the running release were not observed.
- **rask consumers of the secret store other than the catalog** (viewer `objects.py`, ingest `objectstore.py`, annotator
  `lakehouse.py`, `service_kit.media.config`): the call sites were grepped, and only ingest's retry comment was read.
- **The T1 finding rests on grep plus reading the open path** (dependencies.py, namespace.py, the create door). It was not
  driven end to end, so no RED test was run and no live warehouse with a second endpoint was exercised.
- **The staged pylance-12 diff** (`git diff --cached --stat`) touches no secret or vending file. Nothing in it changes this analysis.
