# Production install runbook

The ordered first install of the rask lakehouse into a production cluster. Each step names what to do, the exact value
or command, how to check it before the next step, and the guard that refuses the render when the step is skipped.

The install is GitOps: the estate is the released chart plus a values file in the platform's git, applied by Argo CD.
No step edits live state by hand. The chart takes no credential as a value, renders no platform service, and refuses
a production render until every platform endpoint is named and every out-of-chart provisioning is attested
([architecture/deployment.md](../architecture/deployment.md) § Prerequisites, [ADR 0153](../adr/0153-the-app-chart-is-the-product.md)).

**Conventions.** The release is `rask` (every name below, `rask-sa-catalog` and so on, is `<release>-…`; the repo's own
checks render release `rask`). `<ns>` is the namespace the release installs into; it is part of every ServiceAccount
subject (`system:serviceaccount:<ns>:rask-sa-<component>`), so every offline render below passes `-n <ns>`. `X.Y.Z` is
the lakehouse release (tag `lakehouse-vX.Y.Z`). `<env>.yaml` is the environment's own values file, layered over
`deploy/values-prod.yaml`.

Where the repository states no procedure for a platform service (how the platform installs its NATS, writes into its
Vault, configures its S3), this runbook says **the platform's procedure** and names only what rask requires of the
result.

## 0 · Cluster capabilities

| Capability | rask's requirement | Check | If missing |
|---|---|---|---|
| Gateway API Gateway | A conformant implementation serving Core matches and `URLRewrite`; a Gateway rask's routes attach to (`route.parentRefs`), allowing 64 MB request bodies (Gateway policy, ADR 0165) | `kubectl get gateway -A` lists the Gateway you will name | `route.yaml`: `route.parentRefs must name the Gateway …` |
| A NetworkPolicy-enforcing CNI | `deploy/values-prod.yaml` sets `networkPolicy.enabled: true` (default-deny); a CNI that ignores NetworkPolicy leaves the estate open with no error | The negative probe in step 7 | Nothing refuses: the policies render and are silently not enforced |
| The cluster's ServiceAccount issuer | Its `iss` is what `auth.serviceAccountIssuer` names (default `https://kubernetes.default.svc.cluster.local`); OpenFGA, the S3 STS, the bus signer and the OTLP gateway verify pod tokens against it | `kubectl get --raw /.well-known/openid-configuration` and compare `issuer` | Every FGA, store, bus and telemetry login is refused at runtime |
| A CSI VolumeSnapshotClass | Only with `backups.volumeSnapshot.enabled`, which snapshots the development store's claim; with the platform's S3 there is nothing to snapshot (`chart/templates/backup-snapshot.yaml`), so set it `false` (step 4) | `kubectl get volumesnapshotclass` | The CronJob runs and finds no claim |

## 1 · Platform services

Install each on its own release, by the platform's procedure. rask names each by reference; development installs the
tested version of each from `deploy/fixtures/<name>` (each fixture's `fixture.env` or chart pins it).

| Service | What rask requires of it | rask value that names it | Tested version / reference |
|---|---|---|---|
| **External Secrets Operator** | Serves `external-secrets.io/v1`; its ServiceAccount is the one `externalSecrets.serviceAccount` / `externalSecrets.namespace` name (default `external-secrets` / `external-secrets`) | — | `deploy/fixtures/external-secrets/fixture.env` |
| **Vault or OpenBao** | https; KV v2 at `externalSecrets.kvMount` (`secret`); Kubernetes auth at `externalSecrets.kubernetesAuthPath` (`kubernetes`) against this cluster | `openbao.externalAddr`, `openbao.ca.configMap` for a private CA, `openbao.egress` | deployment.md § the secret store |
| **NATS JetStream** | NATS 2.15.2; JetStream on a file store; a cluster of at least `nats.streamReplicas` (3) nodes; TLS on the client port, `tls { handshake_first: true }` or `auto`; operator mode with one APP account holding JetStream and every rask user and an AUTH account whose auth callout is the platform's **signer**; the signer checks a pod's token by TokenReview for `nats.auth.audience` (`rask-bus`) and answers with a session of at most `nats.auth.sessionSeconds` signed by a key scoped to the user's row of ConfigMap `rask-nats-permissions` | `nats.url` (`tls://`), `nats.ca.configMap`, `nats.egress` | `deploy/fixtures/nats`; [OPERATORS.md § 7](../OPERATORS.md) |
| **OpenFGA** | OpenFGA HA, https, OIDC authn against the cluster's ServiceAccount issuer for audience `rask-openfga`, admitting the subjects `lance.openfgaSubjects` computes (step 2); its Postgres is the platform's, and rask names none | `auth.fgaApiUrl`, `auth.fgaEgress` | deployment.md § OpenFGA and its Postgres |
| **S3 + STS** | Trusts the cluster's ServiceAccount issuer for `objectStore.identity.audience` (`rask-object-store`; `sts.amazonaws.com` on AWS) and grants each workload's subject the policy step 2 renders; answers STS `AssumeRoleWithWebIdentity` at the same address (the chart sets `*_S3_STS_ENDPOINT` to `objectStore.externalEndpoint`); delivers every object write of every rask bucket (`s3:ObjectCreated:*`) to the bus subject `store.events`, kept through a bus outage | `objectStore.externalEndpoint` (https), `objectStore.egress`, `objectStore.identity.roleArns.*` on AWS | `.claude/skills/rask-rustfs` (what rask demands of a store) |
| **The IdP** | An https OIDC issuer; issues tokens with audience `auth.oidc.audience` (`rask-lakehouse`) to the clients it trusts, through `auth.oidc.audienceScope`; registers the console's public client (`console.oidc.clientId`, `rask-console`: PKCE, no secret, redirect `https://<host>/auth/callback`) | `auth.oidc.issuer`, `auth.oidc.idpId`, `auth.oidc.egress` (and `connectors`/`subjectClaims`/`providers` as its shape needs) | deployment.md § Development stand-ins, Dex fixture |
| **OTLP gateway** | OTLP/HTTP, https; admits a token of `observability.identity.audience` (`rask-otlp`) signed by the cluster's issuer; routes the `lance.audit` instrumentation scope to a store that refuses unauthenticated writes and deletes | `observability.export.endpoint`, `observability.export.egress` | [ADR 0215](../adr/0215-each-service-exports-its-telemetry-to-the-platforms-gateway-as-itself.md); `deploy/fixtures/observability` |
| **Alert evaluator** | Loads `deploy/alerting/rules.yml` (Prometheus rule-file format) | — | Prove the file with `make alert-rules-check` before loading |
| **Lineage graph backend** (optional) | An OpenLineage HTTP endpoint, https | `services.lineage.forward.url`, `services.lineage.forward.egress` | Marquez in development (`deploy/fixtures/marquez`) |
| **Argo CD** | Can read the chart registry and the platform's git | — | `scripts/gitops_from_empty.sh` pins v3.5.7 for its proof |

Verify each by its own procedure before step 2. rask's own reading of each comes in step 7.

## 2 · Render the provisioning documents (offline)

The chart renders, in every mode, the documents the platform provisions from. Render them offline from the release's
chart with the namespace you will install into. The attestation flags are set here only so the render completes;
they are attested in git in step 3, after the provisioning they stand for.

```bash
bash scripts/helm.sh template rask oci://ghcr.io/ai-riksarkivet/charts/rask --version X.Y.Z -n <ns> \
  -f deploy/values-prod.yaml -f <env>.yaml \
  --api-versions external-secrets.io/v1/ExternalSecret \
  --set signing.provisioned=true --set nats.auth.provisioned=true --set objectStore.events.provisioned=true \
  --show-only templates/secret-store-identities.yaml \
  --show-only templates/object-store-identities.yaml \
  --show-only templates/nats-permissions.yaml \
  --show-only templates/catalog-declaration.yaml > provisioning.yaml
```

`<env>.yaml` needs the step 4 addresses filled for this render to pass. To list the subjects the platform's OpenFGA
admits, render once more with `--set openfga.enabled=true --show-only templates/openfga-authn.yaml` and read the
ConfigMap `rask-openfga-authn`, key `subjects` (deployment.md; on the prod overlay: `rask-sa-catalog`, `rask-sa-hooks`).

| Document | Keys | Who provisions from it |
|---|---|---|
| `rask-secret-store-identities` | `role-<name>.json`, `policy-<name>.json` for `rask-catalog`, `rask-lineage`, `rask-maintenance` and ESO's `lance-infra` (`externalSecrets.role`) | The secret store's operator |
| `rask-object-store-identities` | `rask-sa-catalog.json`, `rask-sa-lineage.json`, `rask-sa-maintenance.json` (one canned policy per workload) | The object store's operator |
| `rask-nats-permissions` | `permissions.json`: the audience, `sessionSeconds`, each session user's ServiceAccount, publish and subscribe lists | The bus signer's operator |
| `rask-openfga-authn` | `subjects` | The OpenFGA operator |

## 3 · Provision identities and secrets, then attest

Every row is the platform's procedure; the "Applied as" column gives the form the development seed uses
(`chart/templates/openbao-seed.yaml`) where one exists. Nothing here is a chart value.

| # | Provision | Applied as | Check | Guard if absent |
|---|---|---|---|---|
| 3.1 | Each store role and policy from `rask-secret-store-identities`: every app role bound to its ServiceAccount in `<ns>`, audience `rask-openbao`, `token_ttl` 15m / `token_max_ttl` 1h; ESO's role bound to ESO's ServiceAccount | `bao policy write <name> policy-<name>.json`; `bao write auth/kubernetes/role/<name> @role-<name>.json` | `kubectl create token rask-sa-catalog -n <ns> --audience rask-openbao`, logged in at `auth/kubernetes/login` as `rask-catalog`, reads `signing-key-rask-sa-catalog` and is refused `signing-key-rask-sa-maintenance` (OPERATORS.md § 6 "Reading it back"); possible only once the ServiceAccount exists, so repeat it in step 7 | None at render; each service fails its start closed after about two minutes, naming its role |
| 3.2 | For each signing identity (`rask-sa-catalog`, `rask-sa-maintenance`): `signing-public-<key>` (field `keys`: its public NKEY) **before** `signing-key-<key>` (field `seed`: its NKEY user seed). `nk -gen user -pubout` prints a pair | `bao kv put secret/signing-public-<key> keys=<public>`, then `bao kv put secret/signing-key-<key> seed=<seed>` | Both read back under the store's admin login | `signing-provisioned.yaml`: `EVENT SIGNING KEYS NOT PROVISIONED …` (lists the names) |
| 3.3 | `commit-id-key` (field `key`): the catalog's key for deriving each announcement's run id; the dev seed writes 32 random bytes as 64 hex characters | `bao kv put secret/commit-id-key key=<64 hex>` | It reads back; the catalog's policy in 3.1 names `secret/data/commit-id-key` | None at render; the catalog's read fails at runtime |
| 3.4 | `postgres` (field `password`): read by ESO into Secret `rask-infra-credentials` | `bao kv put secret/postgres password=<…>` | ExternalSecret `rask-infra-credentials` Ready after step 6 | None at render; the ExternalSecret stays not Ready |
| 3.5 | `nats-user-monitor` and `nats-user-object-store` (fields `jwt`, `seed`), each issued by the bus's operator with its row of `nats.auth.users`; the signer admits each session user (`admin`, `catalog`, `lineage`, `maintenance`, `maintenance-worker`) by `rask-nats-permissions` | The platform's procedure (OPERATORS.md § 7 "Production") | The bus's `/connz?auth=true` in step 7 | `nats-provisioned.yaml`: `NATS USERS NOT PROVISIONED …` (lists the users) |
| 3.6 | The object store: the root bucket `objectStore.bucket` (`lance-catalog`) and any `objectStore.buckets`; the issuer trust for `objectStore.identity.audience`; each `rask-sa-<component>.json` as the policy for subject `system:serviceaccount:<ns>:rask-sa-<component>` (on AWS: an IAM OIDC provider and one role per account, named in `objectStore.identity.roleArns.<component>`) | The platform's procedure | After step 6, no `store_session_renewal_failed` in the catalog's log | None at render; every store session is refused |
| 3.7 | Store events: every rask bucket delivers `s3:ObjectCreated:*` to `store.events` as the bus user `object-store`. Warehouse buckets the catalog creates later need the same (step 7 reads it) | The platform's procedure | Maintenance's hourly audit (step 7) | `object-store-events-provisioned.yaml`: `OBJECT STORE EVENTS NOT PROVISIONED …` |
| 3.8 | OpenFGA admits the subjects from step 2, audience `rask-openfga`, issuer `auth.serviceAccountIssuer`; its datastore holds the `openfga` database | The platform's procedure | The `rask-openfga-model` Job's log in step 7 | None at render; the Job exits 1 on a 401/403 |

No secret holds an object-store key, a telemetry credential, an IdP client secret or a store root (deployment.md).

**Attest** in `<env>.yaml`, committed to the platform's git once 3.2, 3.5 and 3.7 hold:

```yaml
signing: {provisioned: true}
nats: {auth: {provisioned: true}}
objectStore: {events: {provisioned: true}}
```

## 4 · The values file

Copy `deploy/values-prod.yaml` from the tag `lakehouse-vX.Y.Z` into the platform's git and layer `<env>.yaml` over it.
values-prod already sets auth on, OpenFGA, Postgres, the dev store, Dex and the dev OpenBao off, NetworkPolicy on,
two replicas and PDBs, the catalog's memory tier and write cap, and `observability.enabled`.

**Every value the production render refuses empty:**

| Value | Example shape | Guard |
|---|---|---|
| `openbao.externalAddr` | `https://vault.internal:8200` | `_helpers.tpl` `lance.platformStoreAddr` (required, https only) |
| `nats.url` | `tls://nats.internal:4222` | `lance.natsUrl` (required); `prod-credentials.yaml` (`tls://` only) |
| `objectStore.externalEndpoint` | `https://s3.<region>.amazonaws.com` | `lance.s3Endpoint`; `prod-credentials.yaml` (https only) |
| `auth.fgaApiUrl` | `https://fga.internal` | `lance.openfgaUrl`; `prod-credentials.yaml` (https only) |
| `auth.oidc.issuer` | the IdP, https | `_helpers.tpl` (required; https without Dex) |
| `auth.oidc.idpId` | `corp` | `auth.oidc.idpId must name the idp id …` |
| `observability.export.endpoint` | the gateway, https | `lance.otlpEndpoint`; `prod-credentials.yaml` |
| `observability.identity.audience` | default `rask-otlp`; match the gateway | `lance.otlpAudience` (required) |
| `route.parentRefs` | `[{name: edge, namespace: kgateway-system, sectionName: https}]` | `route.yaml` |
| `route.hostnames` | `[lance.example.com]` | `route.yaml` (real deployment) |
| The egress of each platform endpoint (`lance.platformEgress`): `networkPolicy.apiServerEgress`, `auth.fgaEgress`, `openbao.egress`, `observability.export.egress`, `objectStore.egress`, `auth.oidc.egress`, `nats.egress`, and `services.lineage.forward.egress` when `forward.url` is set | `[{to: [{ipBlock: {cidr: <addr>/32}}], ports: [{protocol: TCP, port: <port>}]}]`, at the port a policy sees; the API server's endpoints from `kubectl get endpointslice -n default -l kubernetes.io/service-name=kubernetes` | `network-policy.yaml`: one message naming every empty one; `_helpers.tpl` refuses a rule with no `to` peers or a peer that admits everything |
| The three attestations | step 3 | `signing-provisioned.yaml`, `nats-provisioned.yaml`, `object-store-events-provisioned.yaml` |
| Images | none: the released chart pins `image.repository` and each image's digest (`scripts/pin_chart_release.py`); set `image.tags`/`image.digests` only to run another build, never `dev` | `_helpers.tpl` (`image.repository must be set`); `prod-credentials.yaml` refuses a `:dev` first-party image |

**Values to set for this estate, not refused when left:**

| Value | Set to |
|---|---|
| `auth.bootstrapAdmin` | The first estate admin, `<idp-id>~<subject>`; empty seeds no human grant (deployment.md § The first estate admin) |
| `catalog.declared` | The estate's projects, warehouses and service grants (OPERATORS.md § 2) |
| `nats.ca.configMap`, `openbao.ca.configMap` | ConfigMaps in `<ns>` holding a private CA, when the bus or store uses one |
| `vending.endpoint` | The store's address as clients outside the cluster reach it, https, when it differs from `objectStore.externalEndpoint` |
| `forwardedAllowIps` | The Gateway data plane's pod CIDR (`*` is refused) |
| `auth.serviceAccountIssuer` | The cluster's issuer from step 0, when not the default |
| `console.oidc.clientId` | The console's public client at the IdP |
| `services.lineage.forward.url` | The graph backend, https, if lineage forwards |
| `backups.volumeSnapshot.enabled` | `false` with the platform's S3: values-prod sets `true`, and the CronJob it renders snapshots the development store's claim, which a production estate has none of (`chart/templates/backup-snapshot.yaml`) |
| `externalSecrets.role` | ESO's role name, if the store's operator names it other than `lance-infra` |

## 5 · Render and gate offline

1. Render the environment's values exactly as Argo CD will:

   ```bash
   bash scripts/helm.sh template rask oci://ghcr.io/ai-riksarkivet/charts/rask --version X.Y.Z -n <ns> \
     -f deploy/values-prod.yaml -f <env>.yaml \
     --api-versions external-secrets.io/v1/ExternalSecret > render.yaml
   ```

   It exits 0 only with every guard in the table at the end satisfied.
2. Read the render: no `rask-openbao`, `rask-postgres`, `rask-openfga` or `rask-rustfs` object, and no `rask-infra-credentials` ExternalSecret; four Deployments
   (`rask-catalog`, `rask-lineage`, `rask-maintenance-worker`, `rask-console`); the SecretStore `rask-vault` at your
   `openbao.externalAddr`; `deployment.environment.name=prod` on every service; a `rask-allow-*-egress` policy per
   platform endpoint carrying your addresses.
3. `make prod-render-check` (`scripts/prod_render_check.sh`) asserts the overlay's posture (NetworkPolicy, PDBs,
   spread, memory tier, externalized stores, ServiceAccounts). It overrides every address with its own `--set`
   dummies, so it proves `deploy/values-prod.yaml` against the chart, not `<env>.yaml`; step 1's render is what proves
   yours.
4. Verify the release's signatures before trusting it (deployment.md § Releases):

   ```bash
   cosign verify --certificate-identity \
     https://github.com/AI-Riksarkivet/rask/.github/workflows/release.yml@refs/tags/lakehouse-vX.Y.Z \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com <image or chart>@<digest>
   ```

5. Release size: Argo CD applies the render and stores no Helm release object, so Helm's 1 MiB release ceiling
   (`.claude/skills/rask-helm` § 1) binds only an install made with `helm install`; on that path measure the packed
   release as that section describes before applying.

## 6 · Install through Argo CD

Commit `<env>.yaml` and the copied `deploy/values-prod.yaml` to the platform's git, then the Application (the shape
`scripts/gitops_from_empty.sh` proves, with the chart from the release registry and the values from git):

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata: {name: rask, namespace: argocd}
spec:
  project: <project>
  destination: {server: https://kubernetes.default.svc, namespace: <ns>}
  sources:
    - repoURL: ghcr.io/ai-riksarkivet/charts
      chart: rask
      targetRevision: X.Y.Z
      helm:
        releaseName: rask
        valueFiles: [$values/<path>/values-prod.yaml, $values/<path>/<env>.yaml]
    - repoURL: <the platform's git repository>
      targetRevision: <branch>
      ref: values
  syncPolicy: {automated: {prune: true, selfHeal: true}}
```

Registering the OCI registry and the git repository with Argo CD is the platform's procedure.

**What runs, and in what order.** Each chart Job carries an Argo CD hook (`lance.reconciledJob`) beside its Helm hook:

| Job | Argo CD | Helm | What it does |
|---|---|---|---|
| `rask-openfga-model` | `Sync` | `post-install,pre-upgrade` | Writes the image's authorization model into the newest store named `lance-catalog` (or `auth.fgaStoreId`); with no store yet it exits 0 and the catalog creates the store and writes the model at boot; a 401/403 exits 1 |
| `rask-nats-stream-r<rev>` | `Sync` | none (ordinary) | As bus user `admin`, declares the streams (`LINEAGE`, `DLQ`, `CATALOG_CONTROL`, `MAINTENANCE_TASKS`, `MAINTENANCE_SCHEDULES`, `STORE_EVENTS`, `KV_MAINTENANCE_CLAIMS`) and durable consumers, converging each to its declared config |
| `rask-nats-stream-retire-r<rev>` | `PostSync` | `post-install,post-upgrade` | Removes the consumers of this release git no longer declares |

No object-store provisioning Job renders in production (`objectStore.enabled: false`): the buckets and policies are
step 3's. No OpenFGA migrate Job renders (`openfga.enabled: false`): the schema is the platform OpenFGA's.

## 7 · Read back

The install is done when each row is observed, not when Argo CD reports Synced.

| # | Read | Expect |
|---|---|---|
| 7.1 | The Application | `Synced` and `Healthy`; `kubectl -n <ns> get deploy` shows `rask-catalog` 2/2, `rask-lineage` 2/2, `rask-console` 2/2, `rask-maintenance-worker` Ready; `kubectl -n <ns> get externalsecret` Ready |
| 7.2 | Secret store logins | Each service's log has `secret_store_logged_in`; repeat 3.1's per-role read-back now that the ServiceAccounts exist. A signer (catalog, maintenance) is Ready only while its own key is in its public list |
| 7.3 | The bus | On the bus's monitoring port, `/connz?auth=true` lists each rask connection named by its bus user and none anonymous; the stream Job succeeded; the signer's own log names each decision |
| 7.4 | OpenFGA | `rask-openfga-model`'s log says `wrote authorization model …` or `… already holds this image's model …` (or, on an empty store, that the catalog writes it at boot) |
| 7.5 | The declaration converged | The catalog logs `declaration_converged` naming `project:default` and each declared object; `catalog.declaration_converge.failed` stays at 0; `GET /api/catalog/management/v1/projects/default` and `GET /api/catalog/management/v1/warehouses/<id>` answer `managed_by: "git"` (OPERATORS.md § 2) |
| 7.6 | A governed write end to end | Signed in as `auth.bootstrapAdmin` with a token for `auth.oidc.audience`: create a namespace in a declared warehouse (`POST /api/catalog/v1/namespace/<warehouse>$<ns>/create`) and a table in it (`POST /api/catalog/v1/table/<warehouse>$<ns>$<table>/create`, Arrow IPC body; `docs/catalog-openapi.json`); `GET /api/lineage/events` then holds the commit. The same create with no bearer is refused 401. Drop the scratch table and namespace afterwards |
| 7.7 | Telemetry at the gateway | The gateway's store holds traces and metrics with `service.namespace=rask` and `deployment.environment.name=prod` from catalog, lineage and maintenance-worker; no export refused at the gateway |
| 7.8 | The audit trail | The audit store holds `lance.audit` records for the declaration's grants (`access_tuple_write`, subject `system:declaration`); an unauthenticated write to that store is refused |
| 7.9 | Lineage forwarding (if `forward.url`) | The graph backend shows 7.6's event; `LineageForwardStalled` and `LineageForwardRefused` stay quiet |
| 7.10 | Store events | After maintenance's first hourly audit, `MaintenanceBucketWithoutStoreEvents` and `MaintenanceBucketStoreEventsUnknown` stay quiet; a warehouse bucket the catalog created in 7.5 that fires one is configured by 3.7's procedure |
| 7.11 | Network isolation | From a pod in `<ns>` outside the secret store's client list, a request to the store's address times out (the negative probe `chart/templates/network-policy.yaml` names); from `rask-catalog` the same request answers |
| 7.12 | Alerts | The evaluator has loaded `deploy/alerting/rules.yml` and none fires |

## 8 · After the install

- **Backups.** OpenFGA's Postgres and the S3 data are the platform's to back up (rask ships no database
  backup). The control-root records (projects, warehouses, declared grants, trash, protection) have their own backup,
  `backups.controlRoot` with `dest` outside the control root's bucket; restore is
  [RUNBOOK-restore.md](RUNBOOK-restore.md).
- **Signing-key rotation** on the platform's store has no procedure in the repository yet (OPERATORS.md § 6); the rule
  stands: the public list before the seed, at most once per 7 days, one previous key kept.
- **On call:** [RUNBOOK-oncall.md](RUNBOOK-oncall.md); its "The estate is not what git declares" covers 7.5's failures.
- **Every later change** is a commit to `<env>.yaml` or a new `targetRevision`; Argo CD applies it and the catalog
  converges `catalog.declared` within a tick (`catalog.declarationConverge.periodSeconds`, 60 s).

## The refusals at a glance

A production render (`rask.isRealDeployment`: `openbao.enabled=false`, or images from a registry off the node)
refuses each of these, in the order a fresh `<env>.yaml` meets them:

| Template | Refuses | Fixed in |
|---|---|---|
| `external-secrets.yaml` | A cluster that does not serve `external-secrets.io/v1` | Step 1 (offline: `--api-versions`) |
| `signing-provisioned.yaml` | `signing.provisioned` false with the platform's store | Step 3.2 |
| `nats-provisioned.yaml` | `nats.auth.provisioned` false with the platform's store | Step 3.5 |
| `object-store-events-provisioned.yaml` | `objectStore.events.provisioned` false with the platform's S3 | Step 3.7 |
| `_helpers.tpl` | Empty `observability.export.endpoint`, `observability.identity.audience`, `objectStore.externalEndpoint`, `openbao.externalAddr` (or http), `nats.url`, `auth.fgaApiUrl`, `auth.oidc.issuer` (or http), `auth.oidc.idpId`, `image.repository` | Step 4 |
| `route.yaml` | Empty `route.parentRefs`; empty `route.hostnames` | Step 4 |
| `prod-credentials.yaml` | The dev store, dev S3 or Dex on; auth off; a `:dev` first-party image; an http FGA, S3, vending, OTLP or lineage-forward address; a `nats.url` that is not `tls://` | Steps 4 and 5 |
| `network-policy.yaml` | Any `lance.platformEgress` value left empty under `networkPolicy.enabled` | Step 4 |
