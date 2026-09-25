# Lakekeeper Helm chart, deep read, compared with rask's chart

**Date:** 2026-09-25. **Mode:** read-only. No repo file was changed. The cluster was touched with `kubectl get` only (KUBECONFIG=/etc/rancher/k3s/k3s.yaml), and no helm command was run.

**Path abbreviations used in citations**
- `LK/` = `lakekeeper-charts/charts/lakekeeper/` (the chart under study, chart 0.12.0 / app 0.13.3, `LK/Chart.yaml:4-5`)
- `LKsrc/` = `/home/gabriel/Desktop/lakekeeper-ref/` (Lakekeeper's server source, opened only to explain what a chart line depends on)
- `rask/` = `/home/gabriel/Desktop/rask/`

## 0. Method, and what was not checked

- **All 42 files under `LK/` were read in full:**
  - `Chart.yaml`, `values.yaml` (780 lines), `README.md`, `README.md.gotmpl`, `Changelog.md`
  - the 8 `ci/*.yaml` scenarios
  - all 16 files under `templates/` (`_helpers.tpl`, `_pods.tpl`, `_validations.tpl`, `serviceaccount.yaml`, `db-migration.yaml`, `tests/bootstrap.yaml`, the 6 `catalog/` templates and the 4 `config/` templates)
  - the 13 `opa-bridge/v0.11/policies/**.rego` files
- **Read in full on the rask side:**
  - `rask/.claude/skills/rask-helm/SKILL.md`, `rask/chart/Chart.yaml`, `Chart.lock` and `.helmignore`
  - these templates: `serviceaccount.yaml`, `security-sa.yaml`, `secrets.yaml`, `dapr-app-token.yaml`, `ray-auth-token.yaml`, `openbao-auth-delegator.yaml`, `auth-consistency.yaml`, `infra-credentials.yaml`, `prod-credentials.yaml`, `openfga-migrate.yaml`, `openfga-model.yaml`
  - `rask/packages/service-kit/src/service_kit/probes.py`
- **Read in sections, not whole:** rask's `values.yaml` (3,499 lines), `_helpers.tpl` (1,868 lines), `bootstrap-admin.yaml` (lines 1-130), `openbao.yaml`, `external-secrets.yaml` (lines 1-60), `dapr-component.yaml` (lines 340-380), `services.yaml`, `fleet.yaml`, `frontends.yaml`, `age-postgres.yaml`, `kueue-queues.yaml`, and `values-prod.yaml` / `values-local.yaml` (by grep). The other ~40 rask templates were **not** read.
- **Why no render was run.** The task constraints say "no helm", so nothing was rendered. Every claim about what a template renders comes from reading it. In particular, the claim that `LK/templates/_validations.tpl` never executes rests on Helm's documented rule that `_`-prefixed files are not rendered, plus corroboration in §13. It was not observed in a render.
- **Live reads (`kubectl get` only):** ServiceAccounts, (Cluster)RoleBindings, deployment `serviceAccountName`, ExternalSecrets / SecretStores, and the SA issuer discovery document plus its ClusterRole/Binding. I did **not** run `kubectl auth can-i`, which creates a review object. Where "can read Secrets" is stated, it rests on the bindings read here and on the earlier audit's measurement.

## HEADLINE

1. **Lakekeeper's chart answers D1 directly, on a model rask can copy.** The whole of machine identity, as a chart, is:
   - one dedicated ServiceAccount per deployable;
   - `system:auth-delegator` bound to that SA through the same helper the pod uses, and only when Kubernetes authn is on;
   - an expected audience;
   - legacy tokens off;
   - a principal key of `kubernetes~<sub>` / `oidc~<sub>`.

   Its own install test bootstraps the catalog using the chart SA's token. rask's live estate is the opposite:
   - lakehouse pods run as the `default` SA, which is bound to Dapr's `secret-reader` Role and to `system:auth-delegator`;
   - fleet and zone pods share an automounted `rask` SA;
   - on the prod-values path, the auth-delegator binding names a different SA from the one OpenBao runs as.
2. **Lakekeeper also mints a secret by lookup-or-random, and does it more safely than rask.** Its minted encryption key carries `helm.sh/resource-policy: keep`, the chart documents the Argo CD caveat, and production is steered to a pre-created Secret. rask mints ~7 bearer credentials this way (the ray token and every `service-token-*`) with no `keep`, and states that GitOps is the chart's first-class consumer. On rask's stack the fix is: generate the material inside OpenBao, deliver it through Dapr or an ESO file, and retire most of it via D1.
3. **Do not copy Lakekeeper's secret DELIVERY.** Every Lakekeeper secret reaches the process as an environment variable: an `envFrom` Secret, or `secretKeyRef`. That is exactly what the owner's rule bans. Copy the other half instead: every credential has an existing-reference alternative, and nothing but the catalog ever talks to OpenFGA.
4. **Migrations: both charts independently hit the same hook-vs-`--wait` deadlock and solved it the same way** (a plain Job with a per-revision name). Lakekeeper then goes further than rask in three ways:
   - the consumer waits on migration **state** (`wait-for-db -dm`), not on a TCP port;
   - the server refuses to start against a schema **newer** than itself;
   - one `migrate` step migrates the DB **and** the authorization model.

   rask uses `nc -z`, lets OpenFGA crash-loop until migrated, and runs lineage DDL on every replica at boot. Two rask templates also carry stale hook-weight prose.
5. **Neither chart has a `values.schema.json`, and Lakekeeper shows what that costs.** It has four silent defects:
   - two CI scenarios set `authn:` where the chart reads `auth:`, so the "authorization" CI never enables k8s authn;
   - three documented values have no reader;
   - `_validations.tpl` is a partial, so its guard never runs.

   rask has the opposite-direction gate (a template names a value that no values file defines). A scan found 7 rask values that no template, test or script reads.
6. **Places where rask is ahead of Lakekeeper (do not copy Lakekeeper here):**
   - **Probes.** Lakekeeper serves liveness and readiness from one dependency-aggregating `/health`, so a Postgres outage restarts every catalog pod. It has no startup probe.
   - **Default hardening.** Lakekeeper's pod/container security contexts default to `{}`, and hardening appears only in a CI scenario.
   - **Where the authz model lives.** Lakekeeper ships Rego policies as chart files, while rask reads the FGA model from the image.
7. **Operators.** Lakekeeper ships **no** CRDs and **no** operators. Its two subcharts are optional conveniences, and its README sends production to a separately installed CloudNativePG. That confirms rask-helm §2, D11 and P7.1/P7.2. Its answer to a withdrawn third-party image (Bitnami) was to own a mirror, which is D10's option space for XC-075.

---

## 1. ServiceAccounts and automount

**What Lakekeeper does**
- It creates one SA by default. `serviceAccount.create: true` (`LK/values.yaml:343-346`) renders it with `automountServiceAccountToken: {{ .Values.serviceAccount.automount }}` (`LK/templates/serviceaccount.yaml:1-14`).
- The name comes from a single helper (`LK/templates/_helpers.tpl:96-102`). The Deployment, the migration Job and the test pod all use it (`LK/templates/catalog/catalog-deployment.yaml:73`, `LK/templates/db-migration.yaml:64`, `LK/templates/tests/bootstrap.yaml:12`).
- `automount` defaults to **true** (`LK/values.yaml:348-349`). That is defensible for Lakekeeper because its pod genuinely calls the Kubernetes API: TokenReview when k8s authn is on (§3), watching Jobs for the OpenFGA migration wait (`LK/templates/_pods.tpl:9-18`), and reading ConfigMaps for Cedar (`LK/templates/serviceaccount.yaml:34-41`).
- The SA's annotations are the documented carrier for cloud workload identity. The IRSA example is at `LK/values.yaml:355-360`.
- Weakness: all RBAC sits inside `if .Values.serviceAccount.create` (`LK/templates/serviceaccount.yaml:1,80`), so bring-your-own-SA also means bring-your-own-RBAC. Not worth copying.

**What rask does today**
- The chart's own SA `rask` is rendered with no `automountServiceAccountToken` at all (`rask/chart/templates/serviceaccount.yaml:1-12`; `rask/chart/values.yaml:24-27` has no automount key), so it defaults to automount.
- Per-workload SAs with `automountServiceAccountToken: false` exist (`rask/chart/templates/security-sa.yaml:1-29`), but only behind `security.serviceAccounts.enabled: false` (`rask/chart/values.yaml:727-728`). Prod turns them on (`rask/chart/values-prod.yaml:192-193`).
- **Live (kubectl get, 2026-09-25):**
  - `rask-catalog`, `rask-lineage`, `rask-maintenance`, `rask-maintenance-worker`, `rask-medallion-producer`, the three stage runners, `rask-openbao`, `rask-dex`, `rask-viewer`, `rask-search`, `rask-annotator` and `ray-lance-head` have no `serviceAccountName`, so they run as `default`.
  - `rask-compute`, `-gateway`, `-ingest`, `-flows`, `-notifications` and all seven `rask-web-*` run as `rask` (`rask/chart/templates/fleet.yaml:59` and `frontends.yaml:139-143` pick `rask.serviceAccountName` unconditionally, or when the flag is off).
  - No live SA sets automount. Only `rask-sa-dapr-sweep` of the per-workload set exists.

**What rask should do on its own stack**
- Make per-service SAs **unconditional**. Delete the `security.serviceAccounts.enabled` flag: under the no-backward-compat ruling a default-off hardening flag is a dual path, and XC-061 makes the same argument about `infraContexts`.
- Set `automountServiceAccountToken: false` on every SA, **including** `rask` (or delete `rask` once each fleet service has its own).
- Where a pod needs a token, mount a **projected** `serviceAccountToken` with an explicit `audience` and `expirationSeconds`. Only then is the one token a pod holds useless against anything but its intended door (§3).
- Keep Lakekeeper's single-helper discipline. Every binding names the SA through the same helper the pod spec uses (§2 shows what happens without it).

**Rows:** new (the "lakehouse pods run as `default`" finding of `findings_lance_lakekeeper.md`, row 3); P8.6 (the D1 foundation in `findings_reconciliation.md:151`); XC-061.

## 2. RBAC

**What Lakekeeper does**
- A namespaced Role grants only `batch/jobs get,list,watch`, commented "Get jobs to detect if migrations finished". `configmaps get` is added only when authz is `cedar` (`LK/templates/serviceaccount.yaml:16-41`, RoleBinding `:43-58`).
- `system:auth-delegator` is bound **only** when `auth.k8s.enabled && auth.k8s.createClusterRoleBinding` (`LK/templates/serviceaccount.yaml:60-79`, `LK/values.yaml:541-543`). Its subject is the pod's own SA via the helper (`:75-77`), and its cluster-scoped name embeds the namespace (`{{ .Release.Namespace }}:<fullname>-token-review`, `:66`).
- Minor over-grant: the jobs Role is unconditional, although it is only needed when the OpenFGA subchart uses `migrationType: job`, and the default is `initContainer` (`LK/values.yaml:609`).

**What rask does today**
- `openbao-auth-delegator.yaml:14-26` binds `system:auth-delegator` to `{{ .Values.openbao.serviceAccountName | default "default" }}`.
  - `openbao.serviceAccountName` is defined in **no** values file. Grep over `rask/chart` and `rask/tests` finds only the template line.
  - Live, the subject is `default/default` (`kubectl get clusterrolebinding rask-openbao-auth-delegator` → `[{"kind":"ServiceAccount","name":"default","namespace":"default"}]`).
- The vendored Dapr subchart's `dapr_rbac.secretReader` is `enabled: true, namespace: default` (`rask/chart/charts/dapr-1.18.1.tgz` → `dapr/charts/dapr_rbac/values.yaml:1-3`). It binds `secrets: get` to SA `default` (`dapr/charts/dapr_rbac/templates/secret-reader.yaml:3-32`).
  - rask sets no override (grep `secretReader|dapr_rbac` over `rask/chart/values*.yaml` → no hits).
  - Live: RoleBinding `dapr-secret-reader` has role `secret-reader` and subject `default`.
- **Consequence (live).** The `default` SA holds both `secrets:get` in `default` and `system:auth-delegator`, and the catalog, lineage, maintenance, the medallion, OpenBao and Dex run as `default` with an automounted token. The earlier audit measured `can-i get secrets` answering yes (`findings_lance_lakekeeper.md` row 3). That was not re-measured here.
- **NEW, on the prod-values path.** With `security.serviceAccounts.enabled: true` (`values-prod.yaml:192-193`), OpenBao runs as `rask-sa-openbao` (`openbao.yaml:85-87`), and that SA has `automountServiceAccountToken: false` (`security-sa.yaml:18`). The auth-delegator binding still names `default` (`openbao-auth-delegator.yaml:25`).
  - The seed configures the kubernetes auth backend with only `kubernetes_host` (`openbao.yaml:355-356`). There is no `token_reviewer_jwt`, and OpenBao has no local token.
  - Per Vault/OpenBao's documented fallback, it will then use the **login** JWT (the external-secrets SA's) as the reviewer JWT. That SA holds no auth-delegator either.
  - So ESO login would fail with 403 on exactly the prod combination: `externalSecrets.enabled` (the intended prod path, commented at `values-prod.yaml:270-271`) together with `serviceAccounts.enabled`.
  - I did not render or run this. The Vault fallback behaviour is from its documentation, not measured.

**What rask should do on its own stack**
1. Set `dapr.dapr_rbac.secretReader.enabled: false`. No sidecar needs it: the earlier audit found every pod sets `dapr.io/disable-builtin-k8s-secret-store`, and that was not re-verified here.
2. Bind auth-delegator through the helper the OpenBao pod uses, never through a free-standing value with a `default` fallback.
3. Give the ClusterRoleBinding a namespaced name, the Lakekeeper way (`LK/templates/serviceaccount.yaml:66`). Today's name, `{{ lance.fullname }}-openbao-auth-delegator`, is `<release>` only, so two releases named `rask` in two namespaces collide.
4. Add a render gate: no (Cluster)RoleBinding that grants `secrets` or `tokenreviews` may name an SA that a first-party app pod runs as, except the verifier SAs D1 names deliberately (§3). Mutation-check it by re-enabling `secretReader`.

**Rows:** new (default SA plus secretReader); new (the auth-delegator subject mismatch on the prod path); P8.6; XC-017 (§B control list); XC-036 (release/namespace portability).

## 3. Machine identity (D1): Kubernetes ServiceAccount tokens, as Lakekeeper ships it

**What Lakekeeper does**
- **Values** (`LK/values.yaml:528-543`):
  - `auth.k8s.enabled` lets Kubernetes SAs authenticate, and it is compatible with OIDC: "multiple IdPs (OIDC and Kubernetes) can be enabled simultaneously".
  - `auth.k8s.audience` is the expected `aud`. It "must be specified" when OIDC and k8s authn are both on, so the two JWT kinds cannot be confused.
  - `legacyEnabled` defaults false. It would accept non-expiring `iss: kubernetes/serviceaccount` tokens.
  - `createClusterRoleBinding` is the TokenReview right, bound as in §2.
- **Rendered env:** `LAKEKEEPER__ENABLE_KUBERNETES_AUTHENTICATION`, `…_AUDIENCE` and `…_ACCEPT_LEGACY_SERVICEACCOUNT` (`LK/templates/config/secret-config-envs.yaml:71-80`).
- **Server side** (context for the chart):
  - The authenticator is built at boot and fails **closed** if the k8s API is unavailable, with the rationale "we can't authenticate service-account tokens at all, which would silently degrade authn" (`LKsrc/crates/lakekeeper/src/service/authn.rs:161-183`).
  - The IdP ids are `oidc` and `kubernetes` (`authn.rs:73-74`). These are the `<idp-id>` of D5, so a machine principal is `kubernetes~<sub>`.
- **The chart's own install test is a machine bootstrapping the catalog** with the chart SA's token as a bearer: `curl -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" …/management/v1/bootstrap` (`LK/templates/tests/bootstrap.yaml:12,20-32`). So the first admin can be a workload, identified by its SA, and nothing asserts identity in a header.
- **The CI scenario meant to exercise this is dead.** `ci/authorization.yaml` and `ci/opa.yaml` set `authn.k8s.enabled: true` (`LK/ci/authorization.yaml:3-5`, `LK/ci/opa.yaml:3-5`), while every template reads `auth.k8s.*`. The scenario therefore runs with no authn at all, and the bootstrap test passes unauthenticated. This is a gate that cannot fail.

**What rask does today**
- The chart renders no projected `serviceAccountToken`, no audience and no TokenReview binding for any rask door. The only `auth-delegator` is OpenBao's (grep `TokenReview|auth-delegator|serviceAccountToken|projected|audience` over `rask/chart/templates`: only `openbao-auth-delegator.yaml` and `security-sa.yaml` comments match).
- Machine callers present chart-minted static bearers instead: `service-token-<identity>` (`rask/chart/templates/infra-credentials.yaml:58-105`, minted by `_helpers.tpl` `lance.dedicatedServiceToken`, see §6), the shared Dapr app token (`dapr-app-token.yaml:15-35`), and the Ray auth token.
- **Measured live, which bears on the P5.3(a) probe:**
  - The cluster's SA issuer is `https://kubernetes.default.svc.cluster.local`, with `jwks_uri` at `https://10.16.51.53:6443/openid/v1/jwks` (`kubectl get --raw /.well-known/openid-configuration`).
  - ClusterRoleBinding `system:service-account-issuer-discovery` binds only group `system:serviceaccounts`, and the ClusterRole grants `get` on `/.well-known/openid-configuration` and `/openid/v1/jwks`.
  - So **any pod presenting its own SA token can fetch the JWKS, but an unauthenticated fetch is not granted.** A third-party verifier that fetches discovery anonymously (MinIO STS, and OpenFGA's OIDC authn) needs a chart-rendered binding to `system:unauthenticated` plus trust in the kube CA. rask's own Python verifier does not need either.

**What rask should do on its own stack** (D1 ruled: SA tokens, Lakekeeper's model)
- **Callers.** Every first-party pod mounts one projected token per door it calls, for example `audience: rask-catalog` and `audience: rask-lineage`, with `expirationSeconds` of 600-3600. It re-reads the file per request, since the kubelet rotates it; this is the same file-reread mechanism XC-001 already shipped for the lineage token. Bearer only; D1 and P8.6 rule out `x-api-key`.
- **The verifier: pick one of two, both named in D1.**
  - **(i) Lakekeeper-identical TokenReview.** Bind `system:auth-delegator` to the catalog's and lineage's own SAs, gated on k8s authn being on, and mount a projected token whose audience is the API server. TokenReview also catches a deleted SA or pod before the token expires.
  - **(ii) The SA issuer as a second trusted issuer in `service_kit`'s OIDC verifier**, the way Lakekeeper runs OIDC and k8s side by side. This needs no ClusterRole beyond the default discovery binding (measured above) and no per-request API call. Revocation lags by up to `expirationSeconds`.
  - Recommendation: **(ii) for rask's own doors**, which keeps a powerful cluster role off the data plane, with short expirations. Use **(i)** only if P5.3(c) shows the must-fail case (a different SA with the same audience) needs API-side checks.
- **Rules either way:**
  - An audience per door, so a token minted for the catalog is refused at lineage.
  - Refuse legacy tokens, as Lakekeeper does by default.
  - Fail closed at boot if the issuer or JWKS is unreachable, per `authn.rs:161-165`.
  - The principal is `kubernetes~system:serviceaccount:<ns>:<sa>` (D5).
- **Third-party verifiers (MinIO STS, OpenFGA OIDC authn).** The chart must render a ClusterRoleBinding of `system:service-account-issuer-discovery` to `system:unauthenticated`, and hand the kube CA to those servers. This is the P5.3(a) precondition, now measured as **unmet** today.
- **What D1 retires, and what it does not.** SA tokens replace the `service-token-*` values as **bearers** (the privileged-subject door, `dapr_auth.service_principal`), and they retire the "two writers must agree byte-for-byte" hazard at `_helpers.tpl:1427-1432`. Per `findings_reconciliation.md:175`, the lineage **HMAC** signing keys (LH-064) remain secrets, and they should be minted in OpenBao (§6).
- **Carry Lakekeeper's test forward, done correctly.** An install/e2e check in which a Job's projected-token identity bootstraps or calls the catalog, **plus a must-fail control** using another SA's token with the same audience. Pre-register the reading rule.

**Rows:** P8.6 / D1 (LH-079 closes as bearer-only); LH-064 (identity comes from the verified token, the HMAC remains); CTL-021 (a sidecar-invoked hop can carry an `Authorization` bearer the SA token fills; whether daprd forwards `Authorization` on service invocation is **unverified**); LH-129 and CP-001 (the Ray job exchanges its SA token at STS via AssumeRoleWithWebIdentity instead of the static `rask-ray-compute` key; `LK/values.yaml:355-360` is the same "SA carries the storage identity" idea); CP-007 (ingest's ambient credentials); P5.3 (probe (a) partly measured here).

## 4. Principal key (D5)

**What Lakekeeper does**
- The IdP ids are `oidc` and `kubernetes` (`LKsrc/crates/lakekeeper/src/service/authn.rs:73-74`).
- The OPA bridge maps a Trino user to `concat("", ["oidc~", trino_user_id])` (`LK/opa-bridge/v0.11/policies/trino/user.rego:4-5`) and sends that as `"identity": {"user": …}` on every check (`LK/opa-bridge/v0.11/policies/lakekeeper/check.rego:23-24` and every `require_*`).

**What rask does today:** the bootstrap Job grants `user:{subject}` from the raw `auth.bootstrapAdmin` Dex `sub` (`rask/chart/templates/bootstrap-admin.yaml`, lines 119-120: `subject = {{ .Values.auth.bootstrapAdmin | quote }}`; `user = subject if ":" in subject else f"user:{subject}"`), and `auth.bootstrapAdmin: ""` is the default (`values.yaml:975`). LH-063 cites `governed/deps.py:181,208` for the service-side `token.sub`.

**What rask should do:**
- Key every principal as `<idp-id>~<claim>`, where the idp-id is an operator-named stable id such as `dex` or `kubernetes`.
- `auth.bootstrapAdmin` should be written in that form, and the chart should `fail` on a bare `sub`.
- The same key must appear everywhere a subject is stored: FGA tuples, notification inbox actor ids and lineage `onBehalfOf`, as `findings_reconciliation.md:180` lists.

**Rows:** LH-063 (D5).

## 5. Secret delivery

**What Lakekeeper does**
- **Everything reaches the process as an env var:**
  - All config, secret or not, goes into one chart-rendered Secret, `<fullname>-config-envs` (`LK/templates/config/secret-config-envs.yaml:11-128`). That includes the DB password when inline (`:37-39`), the OpenFGA `apiKey`/`clientSecret` (`:111-119`), the KV2 password (`:92-94`) and the license key (`:43-45`). It is consumed by `envFrom` (`LK/templates/_helpers.tpl:107-110`, `LK/templates/catalog/catalog-deployment.yaml:88-92`).
  - Each credential **also** has an existing-Secret alternative, delivered as `env.valueFrom.secretKeyRef`: DB user/password (`_helpers.tpl:169-221`), the PG encryption key (`:223-238`), KV2 (`:240-261`), license (`:263-270`) and OpenFGA client credentials (`:272-294`).
  - The OPA sidecar gets its IdP client secret as env, from values or an existing Secret (`LK/templates/catalog/catalog-deployment.yaml:151-191`).
- **Guidance only, no enforcement:** "We strongly recommend using a Kubernetes secret instead" (`LK/values.yaml:28-31`), and "[WARNING] to avoid storing the password in plain-text within your values…" (`:484-487`).
- **Storage credentials never touch the chart.** They live in Lakekeeper's own secret backend, either Postgres encrypted with a key (`LK/values.yaml:363-379`) or Vault KV2 (`:381-399`).

**What rask does today**
- The design is already stricter than Lakekeeper (`rask-helm` §7, `rask/.claude/skills/rask-helm/SKILL.md:140-151`): first-party sidecar pods read from the Dapr `lance-secrets` store, and several no-sidecar consumers read an ESO-synced file.
- **Residue that is Lakekeeper-shaped (values → Secret → env):**
  - `dapr-app-token.yaml:34-35` renders `stringData.token: {{ .Values.dapr.appToken }}` (XC-004).
  - `secrets.yaml:26` renders `HF_TOKEN: {{ .Values.secrets.hfToken }}`.
  - `infra-credentials.yaml:32,40-41,109,118` renders the AGE password, the MinIO root pair, a password-bearing OpenFGA DSN and the Dex client secret when ESO is off.
  - LH-160 counts 30 env-delivered secrets.
- **The Dapr secret store itself authenticates to OpenBao with a static token.** In dev that is the root token as a plaintext Component value (`dapr-component.yaml:356-357`: `vaultToken: {{ .Values.openbao.devToken }}`, where `devToken: root`). In prod it is a hand-created `<release>-openbao-token` Secret (`dapr-component.yaml:359-368`).

**What rask should do on its own stack**
- Do not import Lakekeeper's delivery. Import its **completeness**: no credential without an external-reference path. Then go one step further than Lakekeeper: on `rask.isRealDeployment`, the render **fails** whenever a credential would come from `.Values`, extending `prod-credentials.yaml` from "refuse well-known values" to "refuse any value-sourced credential".
- **Delivery by pod class:**
  - sidecar pods use the Dapr store at use time;
  - no-sidecar pods (zones, Ray, Jobs) use an ESO-written Secret **mounted as a file** and re-read on use;
  - storage uses STS with web identity (§3);
  - third-party images that can only take env are the single written exemption (LH-161, XC-002).
- **Translate Lakekeeper's k8s-SA identity to the secret store too.** OpenBao's kubernetes auth (which rask already provisions for ESO, `openbao.yaml:352-388`) should authenticate each **app** SA to its own role and policy (`secret/<identity>/*`). Then the Dapr `scopes:` list is backed by an OpenBao policy, and no static `vaultToken` exists.
  - **Unverified and must be checked first:** whether Dapr 1.18's `secretstores.hashicorp.vault` supports Kubernetes auth directly. Its documented auth is `vaultToken` / `vaultTokenMountPath`. If it is token-only, the path is an OpenBao-agent-issued token file consumed through `vaultTokenMountPath`.

**Rows:** XC-004, XC-002, LH-160, LH-161, XC-025 (Dex client secret in a ConfigMap), P4.4 (`findings_reconciliation.md:84`); new (the Dapr store's static `vaultToken`).

## 6. Lookup-or-random minting and the GitOps caveat

**What Lakekeeper does**
- `LK/templates/config/db-encryption-secret.yaml:1-21`:
  - only when no `encryptionKeySecret` is supplied, it renders `<fullname>-postgres-encryption`;
  - `encryptionKey` comes from `lookup` of the live Secret, else `randAlphaNum 40 | b64enc` (`:16-18`);
  - it carries `helm.sh/resource-policy: "keep"` (`:10-11`).
- `LK/values.yaml:370-377` documents the hazard: "If you lose the key, you lose access to all secrets… we use helm's lookup function… This is incompatible with some kubernetes tools such as ArgoCD (argo-cd#5202). Please ensure that you have the `encryptionKeySecret` field set if helm's lookup is not supported in your tool."
- There is one minted secret in the whole chart. It is kept on uninstall, and production is told not to use it.

**What rask does today**
- The same pattern, used for more material and without `keep`:
  - `rask.rayAuthToken` (`rask/chart/templates/_helpers.tpl:77-93`: explicit → `lookup` → `randAlphaNum 32`) feeds `<fullname>-ray-auth-token` (`ray-auth-token.yaml:49-60`).
  - `lance.dedicatedServiceToken` (`_helpers.tpl:1420-1481`: supplied → `lookup` of `<release>-infra-credentials` → `randAlphaNum 40`, memoised on `.Values`) feeds every `service-token-*` in `infra-credentials.yaml:75-104` and the OpenBao seed.
  - Neither Secret carries `helm.sh/resource-policy: keep`. The only `keep` annotations in the chart are OpenBao's PVC (`openbao.yaml:47-50`) and the explorer corpus (`explorer.yaml:31`).
- rask states its GitOps posture outright: "GITOPS IS THE FIRST-CLASS CONSUMER of this chart" (`_helpers.tpl:1205`). It also already knows `lookup` is empty under `helm template` (`age-postgres.yaml:86-89`).
- The earlier audit rendered twice and got different tokens every time (`findings_lance_lakekeeper.md` row 33). That was not re-run here, per the no-helm constraint.
- Checksums inherit the instability. `checksum/infra-credentials` hashes the rendered template (`minio.yaml:83`, `frontends.yaml:111`), which under a cluster-less renderer changes on every sync and rolls those pods.

**What rask should do on its own stack**
- **The system of record for generated credentials is OpenBao, never the chart.** The seed generates idempotently inside the store (`bao kv get … || bao kv put … <urandom>`), and consumers read through Dapr or an ESO file.
  - With ESO, an ESO `Password` generator with PushSecret into OpenBao is the ESO-native alternative. It is **unverified** against the ESO version rask installs (2.10.0 per `findings_reconciliation.md:26`).
  - With the chart no longer rendering credential material, the lookup / memo / "guessable-value" machinery at `_helpers.tpl:1420-1481` can be deleted, not maintained.
- **What remains to mint once D1 lands:** only the lineage HMAC keys (LH-064), the Ray auth token (or let KubeRay 1.6 mint its own Secret, `rayservice.yaml` authOptions, and have ESO or the Dapr store read it; **unverified** which is cleaner), and the Dapr app token (XC-004).
- **Interim, if any lookup-mint survives:** add `helm.sh/resource-policy: keep` to its Secret, as Lakekeeper does, and add a render test that no minted value appears in a `helm template` output.

**Rows:** new (`findings_lance_lakekeeper.md` row 33); XC-004; XC-001 (checksum instability); LH-064 (HMAC key minting).

## 7. Config versus secret, and roll-on-change checksums

**What Lakekeeper does**
- Non-secret config (OIDC URI, PG host, port) lives in the same Secret as credentials (`LK/templates/config/secret-config-envs.yaml:20-40,47-58`).
- The pods roll on `checksum/secret-config-envs` (`LK/templates/catalog/catalog-deployment.yaml:27`, and the migration Job `LK/templates/db-migration.yaml:33`), and on OPA config and policy checksums when OPA is enabled (`catalog-deployment.yaml:28-31`).
- Nothing reacts to a rotation of an **externally** supplied Secret, which is XC-001's unsolved half. Lakekeeper leaves it unsolved too.

**What rask does today**
- Non-secret config sits in a ConfigMap (`rask/chart/templates/configmap.yaml:8` ranges `.Values.config`), with `checksum/config` on the fleet and controlplane (`fleet.yaml:42`, `controlplane.yaml:21`).
- Secret checksums: `checksum/dapr-app-token` (`_helpers.tpl:220`), `checksum/infra-credentials` (`minio.yaml:83`, `frontends.yaml:111`) and a value-hash `checksum/frontend-session` (`frontends.yaml:126`).
- XC-001 mechanism (2), a file mount re-read per request, is shipped for the lineage token. That is **ahead** of Lakekeeper.

**What rask should do:** keep the ConfigMap/Secret split, and do not merge them the Lakekeeper way. Finish XC-001 by moving each remaining `secretKeyRef` consumer to a re-read file rather than adding a reloader. A checksum cannot see an ESO write, as rask's own `test_the_infra_checksum_IS_a_constant_under_external_secrets` proves (per the XC-001 row text).

**Rows:** XC-001, LH-160.

## 8. Migrations and bootstrap: Jobs, hooks and the `--wait` deadlock

**What Lakekeeper does**
- **Two modes, chosen by `helmWait`** (`LK/values.yaml:19-21`: "If this is false, helm install --wait will not work"):
  - Hook mode (the default): `helm.sh/hook: post-install,post-upgrade`, weight `-100`, `before-hook-creation` (`LK/templates/db-migration.yaml:15-18`).
  - Plain-Job mode: the hook is disabled (`:19-21`). The Job is always named `-db-migration-{{ .Release.Revision }}` (`:4`), because a Job spec is immutable, and has an optional `ttlSecondsAfterFinished` (`:26-28`, `LK/values.yaml:266-271`).
  - Argo annotations `argocd.argoproj.io/hook: Sync` and `sync-wave: "0"` are always set (`:13-14`), and the same is applied to the OpenFGA subchart's migrate (`LK/values.yaml:601-605`).
  - **All 8 CI scenarios set `helmWait: true`** (`LK/ci/*.yaml:1`), so CI exercises the plain-Job mode, not the default.
- **Consumers wait on migration STATE, not on a port.**
  - The catalog's init container runs the catalog binary's own `wait-for-db -dm -r 100 -b 2` (`LK/templates/_pods.tpl:19-44`, called with `awaitMigration: true` from `catalog-deployment.yaml:82`). The migration Job's init runs `-d` only (`db-migration.yaml:73`).
  - The binary distinguishes `Complete` (start), not-yet (wait), and `Ahead`: "Database has been migrated by a NEWER Lakekeeper… Refusing to start" (`LKsrc/crates/lakekeeper-bin/src/wait_for_db.rs:44-82`).
  - `serve` re-checks with zero retries unless `--force-start` (`LKsrc/crates/lakekeeper-bin/src/main.rs:436-448`).
- **One migrator per state.** `lakekeeper migrate` runs DB migrations, then `authorizer::migrate` (the OpenFGA model), then post-migration hooks (`LKsrc/crates/lakekeeper-bin/src/main.rs:412-433`). The server does not migrate on serve unless a debug flag is set (`main.rs:284-289`).
- **Bootstrap is an authenticated API call to the catalog** (`POST /management/v1/bootstrap`, `LK/templates/tests/bootstrap.yaml:21-32`), never a direct write to OpenFGA.

**What rask does today**
- **The same deadlock fix, reached independently.** "BOOTSTRAP JOBS — the `helm install --wait` deadlock fix" moves Jobs that other resources wait on out of hooks, naming them `-r{{ .Release.Revision }}` (`rask/chart/templates/_helpers.tpl:533-562`). It is used by `openfga-migrate.yaml:14`, `openbao.yaml:139`, `nats-stream-job.yaml:17`, `minio-buckets.yaml:23` and `dapr-inject-sweep.yaml:61`. Jobs that depend on booted apps stay hooks: `bootstrap-admin.yaml:42-44` (weight 5), `openfga-model.yaml:34-36` (weight 0), `greptimedb-ttl-job.yaml:23-25` and `minio-scoped-users.yaml:84-89`.
- **Readiness is a TCP check.** `openfga-migrate`'s init runs `until nc -z <age> <port>` (`openfga-migrate.yaml:35-40`). The OpenFGA server has no wait at all, so it crash-loops until migrated ("OpenFGA stops crash-looping and goes Ready", `_helpers.tpl:549-550`), and the subchart's own wait is disabled (`values.yaml:3090-3091`).
- **The lineage graph DDL runs in every replica's lifespan** ("every replica runs it at boot", `rask/services/lineage/src/lineage/services/postgres.py:9-10`, serialised by an advisory lock).
- **The FGA model has two writers.** One is the `openfga-model` hook, which runs `service_kit.governed.auth.write_model` from the catalog image (`openfga-model.yaml:58-78`, good: one copy of the model, from the image). The other is the catalog's boot-time `provision()`, which still writes when the model differs (XC-011 row: `fga.py:574-575`).
- **`bootstrap-admin` writes tuples directly to OpenFGA** over plain HTTP with **no credential**: `httpx.get(f"{api}/stores", timeout=5)` (`bootstrap-admin.yaml:82,105,129`). The subject is a raw `sub` (§4), and the script is inline Python stored in the release on every revision (rask-helm §1).
- **Stale prose (a comment-rule violation, since the claim is now false):**
  - `bootstrap-admin.yaml:34` says "weight 5 = after the openfga-migrate hook at -5";
  - `openfga-model.yaml:14` says "WEIGHT 0: after `openfga-migrate` (-5)";
  - but `openfga-migrate.yaml:6-10` says it is "NOT a hook".
  - Also stale: `Chart.yaml:75-82` ("STILL 0.3.9 … bump … is a follow-up"), while `Chart.yaml:83` and `Chart.lock` are at 0.3.12; and `values.yaml:3068-3069` ("that bump is blocked on Chart.lock").

**What rask should do on its own stack**
- **Readiness on state.** A consumer's init waits until the store reports **migrated to the version this image expects**, and **refuses** when the store is ahead. For OpenFGA that means waiting for the migrate Job (or `openfga migrate --verify` if the binary offers it; **unverified**), not `nc -z`. The Lance catalog itself needs no migration, since the tables are self-describing; the stateful stores that do are the AGE graph and the FGA model.
- **One writer per state.**
  - The FGA model is written only by the model Job. The catalog's `provision()` **compares and refuses** on a mismatch (pin `RASK_FGA_MODEL_ID`) instead of writing.
  - Lineage DDL moves to a migrate Job (or stays idempotent in the lifespan, but gains the `Ahead` refusal), so a rolled-back image cannot serve a newer schema.
  - This `Ahead` refusal is the precedent for **LH-150**'s "refuse" option: refuse a boot whose delimiter disagrees with stored tuples, as Lakekeeper refuses a DB migrated by a newer binary. The reconciliation's plan to delete the delimiter settings (`findings_reconciliation.md:39`) makes that moot, which is the stronger fix.
- **Bootstrap through the catalog.** Replace the direct tuple writes with an authenticated catalog door (a `/management/v1/bootstrap` equivalent). The Job presents its projected SA token (§3), and the catalog, as the only OpenFGA writer, writes `owner` / `admin` keyed `<idp-id>~<claim>`. That removes the last reason OpenFGA must accept unauthenticated writes (§14). Record the bootstrap outcome on the catalog side; XC-011 is proposed to close on the content gate.
- **Fix the three stale comments** in the commit that touches those files.

**Rows:** XC-011, LH-150, XC-031 (the FGA seed as a hook rather than a runbook step), XC-005 (seed/ExternalSecret ordering: the Lakekeeper principle is "consumers wait on state", so a missing new ESO key should make consumers wait, not delete the Secret); new (OpenFGA crash-loop plus `nc -z`); new (stale hook-weight and 0.3.9 prose).

## 9. Hooks and the identities they run as

**What Lakekeeper does:** its only hook Job (the migration) runs under the release's **plain** SA (`LK/templates/db-migration.yaml:64`). No hook creates an SA, Role or Binding, so no hook can outlive its own identity.

**What rask does today:** `kueue-queues.yaml` creates `rask-kueue-setup` as SA, ClusterRole and Binding **hooks** at weight `-5` (`kueue-queues.yaml:16-35`), which produces LH-189's 401. The other hooks use the plain `-sa-jobs` SA (`bootstrap-admin.yaml:66-68`, `openfga-model.yaml:47-49`), which is correct.

**What rask should do:** make it a rule, with a render gate, that a hook resource never has kind `ServiceAccount`, `Role`, `ClusterRole`, `RoleBinding` or `ClusterRoleBinding`. D11 / P7.1 deletes `kueue-queues.yaml` outright (`findings_reconciliation.md:134`), which closes LH-189 with it.

**Rows:** LH-189, XC-049, CP-047 (via P7.1 / D11).

## 10. Probes

**What Lakekeeper does**
- Liveness **and** readiness both hit `/health` on 8181, with initialDelay 1 s, period 5, timeout 5 and failureThreshold 5 (`LK/templates/catalog/catalog-deployment.yaml:106-125`, `LK/values.yaml:162-177`). There is no startup probe.
- `/health` aggregates every provider's health (DB, secrets, authz) and answers 503 when any is unhealthy (`LKsrc/crates/lakekeeper/src/api/router.rs:166-172,225-232`; `LKsrc/crates/lakekeeper/src/service/health.rs:11-47`).
- So a Postgres or OpenFGA outage fails **liveness** in about 25 s and restarts every catalog pod.
- The OPA sidecar also uses one `/health` for both probes (`catalog-deployment.yaml:213-226`).

**What rask does today** (ahead of Lakekeeper)
- `/livez` is dependency-free and runs on the event loop. `/readyz` is lifecycle-gated with an optional hard-dependency check (`rask/packages/service-kit/src/service_kit/probes.py:1-73`).
- A startup probe with a 300 s boot budget, plus explicit timeouts (`_helpers.tpl:1007-1030,1056-1096`).

**What rask should do:** nothing from Lakekeeper here. Keep the split, and do not import the aggregate-health-as-liveness pattern.

**Rows:** none. This is recorded so no one "aligns" rask with the reference.

## 11. Security contexts

**What Lakekeeper does**
- `podSecurityContext: {}` and `containerSecurityContext: {}` by default (`LK/values.yaml:89-111`).
- The image uid/gid (65532/65534) is always forced, and a user context is merged with `runAsUser`/`runAsGroup` **omitted**, so it cannot override them (`LK/templates/_helpers.tpl:26-35`). The same helper hardens the `check-db` init and the migration Job, because they include it (`_pods.tpl:19-20`, `db-migration.yaml:76`).
- The OPA sidecar sets `readOnlyRootFilesystem: false` (`catalog-deployment.yaml:136-139`; Changelog 0.10.1 gives the Chainguard base-image reason, `LK/Changelog.md:43`).
- The test pod has no security context and runs `curlimages/curl:latest` (`LK/templates/tests/bootstrap.yaml:13-16`).
- Hardening is exercised only in `LK/ci/secure-values.yaml:5-15`.

**What rask does today** (ahead): `lance.securityContext` / `rootlessSecurityContext` / `rootSecurityContext` are unconditional for first-party app containers and Jobs (`_helpers.tpl:1113-1181`), with a shrink-only ratchet over the render (XC-061 text). `infraContexts` is still a default-off flag (`values.yaml:729-752`).

**What rask should do**
- Adopt the one good Lakekeeper idea, with teeth: a CI/kind lane whose namespace **enforces** `pod-security.kubernetes.io/enforce=restricted`. Admission, not a render test, then proves the hardening, which is exactly the step `values.yaml:729-733` defers.
- Delete the `infraContexts` flag, for the same reason as §1.

**Rows:** XC-061, XC-017.

## 12. CRDs and operators: does Lakekeeper ship them? No.

**What Lakekeeper does**
- It has two dependencies (`LK/Chart.yaml:18-28`):
  - groundhog2k `postgres`, a plain StatefulSet chart and **not** an operator, on by default but labelled "[WARNING] embedded Postgres is NOT recommended for production" (`LK/values.yaml:401-406`);
  - `openfga`, gated on `internalOpenFGA: false` (`LK/values.yaml:587-589`).
- It has no CRDs, no `crds/` directory and no operator.
- For production, the README sends users to an external DB and **recommends the CloudNativePG operator installed separately** (`LK/README.md.gotmpl:31`).
- **Image withdrawal:** when Bitnami withdrew tags, Lakekeeper mirrored the images to its own quay org (`LK/Changelog.md:114`, `LK/README.md.gotmpl:9-20`). That needed `global.security.allowInsecureImages: true` to bypass the Bitnami chart's image check (`LK/values.yaml:631-638`), and it later moved chart vendor altogether (Changelog 0.8.0, `:94`). Its upgrade notes admit "No automatic migration will be provided" (`README.md.gotmpl:18`).

**What rask does today**
- Nine subcharts, including three controllers or operators (kuberay-operator, cloudnative-pg, kueue) plus the Dapr control plane and the NVIDIA device plugin (`rask/chart/Chart.yaml:35-104`).
- Kueue's 11 CRDs are templated into the release. The CNPG CRDs are vendored out-of-band (`rask/chart/.helmignore`, `crds-bootstrap/`).
- rask-helm §2-§3 already says operators do not belong in the app chart (`SKILL.md:62-94`). D11 is ruled, and P7.1/P7.2 are planned.

**What rask should do:** follow the reference. The app release ships no operator and no CRD:
- Kueue leaves (D11/P7.1);
- infra goes to its own release(s) (P7.2);
- CNPG stays an out-of-band prerequisite, which is Lakekeeper's exact stance.

For withdrawn third-party images (XC-075, D10), the reference answer is **own the bytes**. Lakekeeper mirrored them. rask's analogue is a Dagger-built or Dagger-mirrored image in the estate registry, digest-pinned (XC-032). Do **not** copy Lakekeeper's `allowInsecureImages` bypass.

**Rows:** XC-045, XC-049, LH-189, CTL-017, XC-075 / D10, XC-032, XC-070 (the amd64-only `apache/age` is the same own-the-bytes question), LH-108 / XC-008 (Lakekeeper's production DB is CNPG, run separately).

## 13. Values schema and dead values

**What Lakekeeper does:** no `values.schema.json`, and four silent defects follow from it.
1. `ci/authorization.yaml` and `ci/opa.yaml` set `authn.k8s.enabled`, which nothing reads (§3).
2. `catalog.terminationPeriod: 60` ("how many seconds to wait after SIGTERM before SIGKILL", `LK/values.yaml:198-200`) is read by no template. The Deployment sets no `terminationGracePeriodSeconds` (`catalog-deployment.yaml:48-257`), so pods get the kubelet's 30 s, not the advertised 60.
3. `catalog.dbMigrations.enabled: true` ("if `false`, you will need to ensure `lakekeeper migrate` runs…", `LK/values.yaml:233-236`) is read by no template. `db-migration.yaml` has no gate, so the Job always renders.
4. `externalDatabase.type` (`LK/values.yaml:456-459`) is read by nothing.

(Items 2-4: grep over `LK/templates` for `terminationPeriod|dbMigrations.enabled|externalDatabase.type` finds no hits.)

Separately, `LK/templates/_validations.tpl:1-4` guards `authz.backend ∈ {allowall, openfga}` with top-level `required`. Helm does not render `_`-prefixed files, and nothing includes it (grep: no `include`/`template` of it). The guard is therefore dead. It is corroborated because `cedar` is advertised (`LK/values.yaml:545-551`) and specially handled (`serviceaccount.yaml:34-41`), and would be refused if the guard ran. The working guard is the same idiom inside a rendered template (`LK/templates/config/secret-config-envs.yaml:1-9`).

**What rask does today**
- No schema (`ls rask/chart/values.schema.json` → absent; `rask-helm` §6, `SKILL.md:130-131`).
- It has the **inverse** gate: `rask/tests/unit/test_every_chart_value_a_template_names_actually_exists.py:1-25`, which catches a template naming an undefined value, with a mutation self-check.
- `required` guards exist for typed settings (`SKILL.md:104-106`).
- rask keeps render-time guards in rendered no-object templates (`auth-consistency.yaml`, `prod-credentials.yaml`). A scan of rask's three partials found **no** top-level `fail`/`required` outside a `define`, so rask does not have Lakekeeper's `_validations.tpl` bug.
- **Values with no reader.** I scanned first-party blocks for leaf keys whose full path and final key name appear in no template, then grepped tests, scripts and the Makefile. Seven are inert:
  - `medallion.kueueQueue` (`values.yaml:1362`, whose own comment calls it "Inert"; D11 deletes it);
  - `ray.servePort`, `ray.clientPort`, `ray.redisPort` (`values.yaml:2582-2584`);
  - `services.ingest.cronKind`, `cronDataset`, `cronOptions` (`values.yaml:161-163`).

  `maintenance.expectedDatasets` / `secondsPerUnit` are read by `tests/unit/test_the_lane_can_keep_up_with_its_own_sweep.py`, so they are declarations and not dead. The scan is heuristic: a value reached only through a `range` variable whose key name is generic could be missed, and I did not audit the subchart blocks.

**What rask should do**
- Add `chart/values.schema.json`, typed, with `required` for the keys recent regressions hit, and `additionalProperties: false` on **first-party** blocks only.
  - The subchart lesson is in rask's own values: OpenFGA's schema enum forced the `extraEnvVars` workaround (`values.yaml:3060-3070`). Measured here, 0.3.12's `experimentals` enum still lists only 4 flags (`charts/openfga-0.3.12.tgz` → `values.schema.json`).
  - Measure its packed size against the 1 MiB ceiling. My understanding is that the schema is stored in the release, but I have not verified it here.
- Add the **values → reader** gate: every first-party leaf key is read by a template, or declared test-read. Mutation-check it with Lakekeeper's `terminationPeriod` shape.
- Delete the seven inert keys (no backward compatibility).

**Rows:** new (`findings_lance_lakekeeper.md` row 80); new (values-with-no-reader gate plus 7 inert keys); XC-036 (a schema could also pin the two hardcoded `rask-` Secret names).

## 14. OpenFGA topology: who may talk to the authorizer

**What Lakekeeper does**
- The catalog is OpenFGA's **only** client. It authenticates with a pre-shared `apiKey`, or with OIDC client credentials (`clientId`/`clientSecret`/`tokenEndpoint`) (`LK/values.yaml:545-585`, `LK/templates/config/secret-config-envs.yaml:98-123`, `_helpers.tpl:272-294`). The model is migrated by `lakekeeper migrate` (§8).
- The default subchart OpenFGA is "not secured… We recommend to secure the API or use network policies" (`LK/values.yaml:595-596`).
- **An external engine (Trino) never touches OpenFGA.** The OPA bridge asks the catalog: `POST /management/v1/action/batch-check` with `{"operation": …, "identity": {"user": "oidc~<sub>"}}` (`LK/opa-bridge/v0.11/policies/lakekeeper/check.rego:11-29,174-201`). It authenticates as its own client-credentials principal, with the token cached for 150 s (`LK/opa-bridge/v0.11/policies/lakekeeper/authentication.rego:3-21`).
  - It chunks at the server's 1,000-check limit (`LK/values.yaml:698-700`, `check.rego:174-189`).
  - It caches name→id lookups for 1 h (`identifiers.rego:3-16`).
  - It passes the **action payload** into the check, for example `create_namespace` with `properties` and `name`, and `commit` with `updated_properties` / `removed_properties` (`check.rego:31-49,92-121,145-172`; `trino/allow_table.rego:81-90`). A policy can therefore decide per property, not just per action.
  - With `error-on-not-found: false` (`check.rego:17`), a missing object is "not allowed" rather than an error, so the check is no existence oracle.

**What rask does today**
- More than a dozen workloads and 2 Jobs dial OpenFGA directly on `http://<release>-openfga:8080`:
  - via `lance.governedFgaEnv` (`_helpers.tpl:1306-1310`) in `services.yaml`, `fleet.yaml`, `controlplane.yaml` and `explorer.yaml`;
  - plus `maintenance.yaml:368`, `maintenance-worker.yaml:321` and `medallion.yaml:179,688`;
  - plus `bootstrap-admin.yaml:82` and `openfga-model.yaml:69`.
- No template renders an OpenFGA credential (grep: no `OPENFGA_AUTHN`/preshared key in `rask/chart/templates`; this was a heuristic grep). The earlier audit's headline records that OpenFGA answers unauthenticated calls.

**What rask should do on its own stack**
- **Turn on OpenFGA authn.** Use OIDC against the SA issuer if P5.3(a) passes, which needs §3's `system:unauthenticated` discovery binding. Otherwise use pre-shared keys, one per client, generated in OpenBao and delivered by Dapr or an ESO file.
- **Restrict writers.** Tuple and model writes should come only from the catalog (and lineage, if it must) and the model Job. Two ways to get there:
  - Lakekeeper's way: every other PEP asks a **catalog check door** (a batch-check over FGA with the verified caller's identity, or on-behalf-of from a delegator allowlist, the LH-064 (a) pattern). Services then stop holding an OpenFGA credential at all.
  - OpenFGA's own `enable-access-control` experimental. It is listed in the 0.3.12 schema enum; its semantics are **unverified** here.
  - The first is the Lakekeeper-proven shape. The second keeps rask's distributed PEPs.
- **Carry the action payload into the check.** Lakekeeper's property-aware operations are the model for **LH-077**: `alter_transaction` should check per action inside the request, not once at committer tier.

**Rows:** new (OpenFGA unauthenticated, from the `findings_lance_lakekeeper.md` headline); LH-077 / XC-020 (superseded into the transaction row per `findings_reconciliation.md:5`); CP-004; LH-076; XC-007 (plaintext OpenFGA); LH-037 (authz before existence is consistent with Lakekeeper's `error-on-not-found: false`; I did not check the drop/create `mode` semantics).

## 15. What the chart does not carry: storage endpoints

**What Lakekeeper does:** the chart has no object-store endpoint, bucket or STS setting anywhere (all 780 lines of `LK/values.yaml`). The client-facing `endpoint` and the catalog-facing `sts_endpoint` are **per-warehouse storage-profile data**, set through the management API (`LKsrc/crates/lakekeeper/src/service/storage/s3.rs:74-99`: `endpoint`, `sts_endpoint` "Use this when the STS endpoint differs from the S3 endpoint", `sts_enabled`). The catalog's own base URL comes from `Host` / `X-Forwarded-*`, unless `LAKEKEEPER__BASE_URI` forces it (`LK/values.yaml:136-147`, `LK/Changelog.md:141-142`).

**What rask does today:** the reconciliation proposes a chart value `vending.stsEndpoint` to split the STS endpoint from the client endpoint (`findings_reconciliation.md:53`). LH-177 is that the catalog vends its in-cluster address.

**What rask should do:** make the client-facing endpoint and the STS endpoint fields of the **warehouse** record, following the reference, rather than estate-wide chart env. A per-warehouse value is also correct when warehouses sit on different stores. A chart default may seed the platform warehouse only.

**Rows:** LH-177.

## 16. Chart versioning, changelog and docs

**What Lakekeeper does:** the chart `version` moves with every release (0.12.0, `LK/Chart.yaml:4`). A Keep-a-Changelog file flags each upgrade hazard, for example: the ⚠️ Postgres 17→18 bump has no auto-migration (`LK/Changelog.md:24-25`); the 0.4.1 selector change needs `--force` (`:215`); 0.13 refuses to start against a DB migrated by a newer binary (`:21`). The README is generated by helm-docs from `# --` comments (`LK/README.md.gotmpl:1-38`, `LK/README.md:254`). `helm.sh/chart` carries the version (`_helpers.tpl:65-67`).

**What rask does today:** `version: 0.3.0` on every revision (`rask/chart/Chart.yaml:14`; XC-055: 193 revisions). The recovery verbs have no seam (XC-056). Upgrade hazards live in commit messages and skills.

**What rask should do:** have the Dagger chart lane stamp `version` / `appVersion` from git (for example `0.3.0-<n>+<sha>`; `helm.sh/chart` already sanitises `+`, `LK/_helpers.tpl:66`), so `helm history rask` names what each revision deployed. Keep upgrade hazards in `docs/DECISIONS.md`, which is where rask's comment rule puts history. Skip helm-docs: rask's `values.yaml` prose is free in the release (`SKILL.md:35-37`), and a generated README adds nothing the values file lacks.

**Rows:** XC-055, XC-056.

## 17. Portability details

- **Cluster domain.** Lakekeeper has a `clusterDomain` value (`LK/values.yaml:11-13`) and uses an absolute FQDN with a trailing dot (`secret-config-envs.yaml:105`; Changelog 0.10.1: "use absolute FQDN", `LK/Changelog.md:44`). rask hardcodes `svc.cluster.local` with no trailing dot (`rask/chart/templates/_helpers.tpl:792-794`, `lance.vaultAddrFQDN`) and has no `clusterDomain` value. That is low priority, and it goes with XC-036.
- **Ingress TLS.** Lakekeeper's is optional, with a pre-created Secret (`LK/values.yaml:331-336`, `catalog-ingress.yaml:18-25`). It is no stronger than rask. XC-027 (a prod ingress with no TLS) is rask's own open item.

## 18. Do not copy from Lakekeeper (summary)

| Lakekeeper practice | Why not, here |
|---|---|
| Secrets as env via `envFrom` / `secretKeyRef` (`_helpers.tpl:104-294`) | The owner's rule; rask-helm §7 |
| Non-secret config in a Secret (`secret-config-envs.yaml`) | It hides config and blurs rotation; rask's ConfigMap split is better |
| One `/health` for liveness and readiness, aggregating dependencies | A DB outage restarts the fleet; rask's `/livez`/`/readyz` split is better |
| Empty default security contexts; hardening only in CI | rask hardens by default with a ratchet |
| `automount: true` by default | Only a verifier needs an API token, and then a projected one |
| Rego policies shipped as chart files (`opa-bridge-policies.yaml:11-14`) | Release bytes and a second copy; rask reads its model from the image |
| `allowInsecureImages: true` to use a mirror | Own the bytes (Dagger) and digest-pin instead |
| lookup-or-random for production material | GitOps rotates it; generate in OpenBao instead |

## Not covered

- ~40 of rask's 63 templates were not read; nothing was concluded about them.
- No render was run, so these are unverified: the `_validations.tpl` non-execution (inferred from Helm's documented rule plus corroboration), and the prod-path auth-delegator failure (inferred from reading plus the Vault docs).
- Whether Dapr 1.18's Vault component supports k8s auth, whether daprd forwards `Authorization` on invocation, OpenFGA's `enable-access-control` semantics, ESO 2.10's generators, and whether the Helm release stores `values.schema.json`: all unverified.
- The live `rask-test` SA, Role and RoleBinding were seen but their provenance was not established, so no finding is filed on them.
- The user's parallel request (a validity audit of the lakehouse tests) is outside this domain and was not done here.
