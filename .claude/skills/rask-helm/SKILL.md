---
name: rask-helm
description: "The rask Helm chart and its release: the 1 MiB ceiling on the release object and what fills it, why operators do not belong in the app chart, how removing a subchart can delete live data, the upgrade flags that silently drop new defaults, and which generic Helm advice contradicts rask's zero-trust rule. Use when editing anything under chart/, adding or removing a subchart or CRD, running helm upgrade or install, a value does not reach a pod, an upgrade fails with 'Too long: may not be more than 1048576 bytes', or when reaching for a public Helm skill."
---

# rask × Helm — the chart, the release, and what a public skill will not tell you

The generic sources are a sound baseline and miss every problem this estate has actually hit. A
public set of Helm chart skills (development, review, maintenance, testing) was read in full on
2026-09-25: across all four, the only mention of CRDs or subcharts is a `crds/` line in a directory
tree. Nothing on umbrella charts, operator CRD lifecycle, the release ceiling,
`--reuse-values`, or `resource-policy: keep`. Two of its sections also contradict the owner's secrets
rule (§7). This skill is the estate-grounded layer; §6 is what was adopted from it.

## 1. The release object has a hard ceiling — know what fills it

Helm stores every revision as ONE Kubernetes object, `Secret sh.helm.release.v1.rask.v<N>`, holding
`base64(gzip(json(release)))`. The API server refuses any Secret over **1,048,576 bytes**. It is a
hard-coded API-server validation constant: not an etcd setting, not tunable, and the `configmap`
driver has the same cap. `--history-max` limits how MANY revisions are kept, never how big one is.

**What is stored:** `manifest` + `chart.templates` + `chart.files` + `hooks` + `config`.
**What is NOT stored:** subchart tarballs. `Chart.dependencies` is an unexported field in Helm's
`pkg/chart/chart.go`, so everything under `chart/charts/` is invisible to the JSON. Their *rendered*
output reaches the manifest; their source costs nothing.

The chart today: with every operator, the compute engine, the telemetry chain and the development stand-ins out of the
chart (ADR 0153, [[XC-124]] to [[XC-132]]), revision 316 measured 335,260 packed bytes on 2026-10-12 with
`values.schema.json` in the chart: **32.0%** of the ceiling.

What filled it while operators were subcharts, measured 2026-09-25 on revision 240 (96.3% of the ceiling), is why
none goes back in:

| part | share of the packed ceiling |
|---|---|
| rask's own 63 templates | ~50% — 299 KB of it `{{/* */}}` comments, stored in full every revision |
| Kueue's templated CRDs (manifest; a subchart then, none now) | ~24% |
| the other seven subcharts together | ~5.5% |

**`values.yaml` prose is free** — it is stored as a parsed map, comments dropped. **Template prose is
not**, so templates carry no `{{/* */}}` comments ([[XC-123]], ADR 0153): a template's rationale lives in
`docs/chart/templates/<template>.md`, one section per template line it explains, and each setting carries a
one-line `# --` comment directly above its key in `values.yaml`, from which helm-docs generates
`chart/README.md` (`make chart-docs`; `make chart-docs-check`, in `make check` and `dagger call charts`,
fails on a stale README or a value with no description). A section banner in `values.yaml` is `# ===`, never
`# ---`: helm-docs reads any `# --` line as the start of a description. The environment overlays live in
`deploy/` (`values-local.yaml`, `values-prod.yaml`, `values-live-pins.yaml`), outside `chart.files`. Template comments
cost the release in full on every revision and render nothing, so keeping them out is free headroom. A YAML `#` comment inside a template is different: it is part of the rendered
manifest (measured 2026-11-06: 274 `#` lines in the default render, 66 of them Helm's `# Source:` headers, and 348 in the deployed overlay's), so moving one changes what is deployed. An empty `{{/**/}}` left in a template is load-bearing whitespace control: deleting it
lets a neighbouring `-}}` trim text the comment shields.

**Measure before applying, the way Helm's Go code encodes it:**

```bash
bash scripts/helm.sh upgrade rask ./chart --reset-then-reuse-values --set image.tags.<stem>=<tag> \
  --dry-run=server -o json > /tmp/rel.json
```

then pack it as Go does: compact JSON with `<`, `>`, `&` escaped to `\u003c` `\u003e` `\u0026`,
UTF-8 kept, gzip, base64. Helm writes with Go's `gzip.BestCompression` (`pkg/storage/driver/util.go:43`),
and Go's deflate and Python's zlib do not produce the same size at any level: measured on revision 251,
Python at level 6 read 1,044,692 bytes for a release stored at 1,047,760 (99.92%), 3,068 short. So take
the LIVE revision's stored size (the decoded `data.release` of `sh.helm.release.v1.rask.v<N>`) and add
the difference your change makes to the Python-packed render; an absolute Python number reads "fits"
for a revision the API server refuses. Python's default `gzip.compress` is level 9 and **under-counts
further** — enough to read "fits" for a
revision that the API server refuses.

`.helmignore` keeps non-runtime files out of `chart.files`. The alert rules are not chart files at all
([[XC-127]]): `deploy/alerting/rules.yml` is an artefact the platform's evaluator loads; `rules_test.yml` alone,
when it still sat in the chart, was 40,912 packed bytes.

**Answer the ceiling by removing what fills it, not by trimming around it**: what fills it is rendered output and
template source (above), never `chart/charts/*.tgz`, which by Helm's source is not stored. The ceiling's chronology
and the storage-driver decision are `docs/adr/0038-helm-release-storage-the-sql-driver-stands-the-chart-is-not.md`'s.

## 2. Operators do not belong in the app chart

Helm's own CRD guidance gives two methods: a non-templated `crds/` directory (installed once, never
upgraded or deleted), or the CRDs in a **separate chart installed on its own**. Operators are the
second case. Kueue's upstream docs only ever install it as its own release in `kueue-system`.

- **Kueue left the chart ([[XC-126]])**, as BYO compute: its 11 CRDs, annotated keep and with their conversion
  webhook repointed at `kueue-system/kueue-webhook-service`, are served by the cluster's own Kueue (not a rask
  release, and no rask fixture). What follows is why it could not stay as a vendored copy.
- **A vendored Kueue cannot keep its CRDs out of the umbrella.** It templates its 11 CRDs under
  `templates/crd/` with no switch — 0.18.1 and 0.19.6 alike, and the upstream request to move them was
  closed as not-planned. The conversion webhook's service name and namespace are templated, so a
  vendored copy must be pre-rendered with release `rask` and namespace `default` baked in.
- `docs/adr/0038-helm-release-storage-the-sql-driver-stands-the-chart-is-not.md` (2026-08-15) already calls infra-separated-from-app "the architecturally correct
  answer and … the intended end state". It was deferred, not rejected; ADR 0153 decides it.
- **What leaves the chart lands in `deploy/fixtures/`** (ADR 0153, [[XC-124]]): one directory per prerequisite, an
  upstream chart pinned in `fixture.env`, installed as its own release by `scripts/fixtures.sh` before rask's
  (`make k3s-fixtures`, a prerequisite of `make k3s-up`; `FIXTURES` names which, default `external-secrets nats
  postgres dex rustfs openbao observability marquez`: the NATS
  JetStream bus `rask-nats` in rask's namespace that `nats.url` names ([[XC-129]]), OpenFGA's development Postgres
  `rask-postgres` that `postgres.enabled` names (ADR 0228; `WAIT=false`, its password is the dev store's), the dev OIDC issuer `rask-dex`, object store `rask-rustfs` ([[XC-163]], ADR 0168) and secret store `rask-openbao` ([[XC-131]]; both stores' fixtures are `WAIT=false`: the object store's root credential and the secret store's seed scripts are rask's), the telemetry chain whose Collector every service exports to as itself ([[XC-127]], ADR 0215), and the OpenLineage graph backend
  `rask-marquez` lineage's forwarder sends to (ADR 0158)). **The Dex fixture** (`deploy/fixtures/dex/chart`) enforces PKCE for every
  authorization code (`oauth2.pkce.enforce: true`, `codeChallengeMethodsSupported: [S256]`; the password,
  client-credentials and token-exchange grants never pass `/auth`), serves no device grant (its `/auth` leg carries no
  challenge, so under enforced PKCE it could not complete), and answers CORS only for the origins its `allowedOrigins`
  value lists (empty by default, rendering no `web.allowedOrigins`). Its own sign-in client `rask-web` is confidential:
  no `public`, no redirect URI, its secret `required`, signed in as only by the live lanes' password grant. **The
  console's sign-in client is the fixture's own `console` value** (ADR 0193): `rask-console`, public (PKCE S256, no
  secret), redirecting to `/auth/callback` on `:8080` and the Vite dev server's `:5173`, a trusted peer of the audience
  client, its `allowedOrigins` answered for CORS. It is a key of its own so that a client's `clients` or
  `allowedOrigins` values cannot drop it by replacing a list; `clients` stays the empty seam a client's own repository
  fills with `-f`: a map keyed by client id, each entry `{public, secret, redirectURIs, allowedOrigins}`, so several
  repositories' files merge ([[XC-221]]); a list is refused at the render.
  **The console is the product's, not a client** (ADR 0193): the product chart renders it (`templates/console.yaml`,
  `console.enabled`: Deployment, Service and HTTPRoute `rask-console`, ServiceAccount `rask-sa-console` with no token
  mounted, ConfigMap `rask-console-config`), and `make k3s-up` installs it with the lakehouse. **No client is a
  prerequisite** (XC-183), and this repository deploys none: a client's release reaches rask by reference, and what
  the lakehouse must admit for it (a bus user in `nats.auth.users`, with a `serviceAccount` to be a session user; store reads in `openbao.externalReaders`, a store
  peer in `networkPolicy.objectStoreExtraFrom`) comes from the client's own repository with `-f`. A client's roles
  on the catalog are grants to its own identity: made at the catalog's access doors ([[LH-563]]), or, on a project or
  warehouse, declared in `catalog.declared.grants` from the client's own values (§ 5). Every client lives outside this repository
  (XC-192) and are recovered from git history (`64ef118a9`) as each moves to its own repository. A fixture's `SET_FILES` passes an
  artefact from outside its chart with `--set-file` (the alert rules), and a local fixture chart with upstream
  `dependencies:` is vendored from its own `Chart.lock` (`scripts/fixtures.sh --vendor`, run by `make k3s-deps`, so a
  test can render it through `chart_render.render_fixture`). The e2e lanes install ESO and NATS through the same
  script. The §3 handover is automatic: every object the fixture renders that another release (or none) already owns is
  annotated keep and the install takes `--take-ownership`, so run the fixture BEFORE the rask upgrade that stops
  rendering the object. A workload whose selector names the old release (immutable) is re-created instead: a
  StatefulSet is deleted with `--cascade=orphan` and its pods relabelled, so the new one adopts them and rolls them onto
  the same claims (keep the fixture's `fullnameOverride` equal to the old names, as NATS's `rask-nats`, or the claims
  do not match). Fixtures are not for production: namespace label
  `rask.dev/fixture=not-for-production`, release description the same.

## 3. Removing a subchart that owns CRDs deletes the CRDs AND every object of that kind

A CRD rendered from a template is an ordinary resource owned by release `rask`. Drop the subchart —
`kueue.enabled=false`, or deleting it from `Chart.yaml` — and the next upgrade **deletes all its CRDs,
and with them every custom resource in the cluster**. For Kueue on 2026-09-25 that was 11 CRDs, 2
ClusterQueues, 2 LocalQueues, 2 ResourceFlavors and 8 live Workloads.

Before ANY change that removes a CRD-owning subchart:

```bash
kubectl annotate crd <each crd> helm.sh/resource-policy=keep
```

then hand them to the new owner (`helm install … --take-ownership`). Kueue's CRDs carried
`resource-policy: keep` before rask's release stopped rendering them
([[XC-126]]).

## 4. Upgrade flags — `--reset-then-reuse-values`, never `--reuse-values`

`--reuse-values` keeps the previous release's values and **ignores this chart's defaults**. A key
added after that release arrives nil, and `{{ .Values.x | quote }}` renders nil as `""`: a typed setting such as
`LANCE_ERASURE_RELEASE_GRACE_DAYS` would reach its pod as `''` and pydantic refuse it at boot (CrashLoopBackOff,
measured 2026-09-25 on a maintenance setting, contained only because the rolling update kept the old pods serving).

- **`make k3s-up` and `make k3s-converge` use `--reset-values`** ([[XC-194]]): the release's values are composed from
  git each time (`deploy/values-local.yaml`, `image.repository` from `K3S_REGISTRY` or `image.localImages`, the tags in
  `deploy/values-live-pins.yaml`, and the derived `auth.oidc.issuer`, `vending.endpoint` and `auth.bootstrapAdmin`),
  so a value that exists only in the live release does not outlive the next run. Values written outside this
  repository are named in `K3S_EXTRA_VALUES` (the rask release) and `K3S_DEX_EXTRA_VALUES` (the Dex fixture), lists of
  values files, empty by default, appended after rask's own ([[XC-220]]); a `make k3s-up` run without them drops what
  they added. `make k3s-values` prints the
  composition `helm get values rask -o json` equals afterwards. A new build reaches it by tag:
  `make k3s-pins TAGS="<stem>=<tag> ..."` writes the tags into the pins file without reading the cluster.
- A hand-run upgrade that must keep the release's values uses `--reset-then-reuse-values` (helm ≥ 3.14; this estate
  runs 3.20): chart defaults, then the release's values, then any `--set`.
- A fixture's install takes `--reset-values` (`scripts/fixtures.sh`): its values are its chart's defaults, its files in
  git and the caller's arguments, which every caller passes on each run, so a value set once by hand, or a client's
  `dex-values.yaml` deleted from the tree, does not outlive the next run.
- Guard every value a typed setting depends on with `required`, so absence fails the RENDER with a
  message rather than a pod: see `LANCE_ERASURE_RELEASE_GRACE_DAYS` in `chart/templates/services.yaml`.
- `helm diff upgrade` shows a `''` before it ships.
- Never kill a running upgrade — it leaves the release in `pending-upgrade`, which refuses every later
  one until `make helm-rollback REV=<revision>`.

## 5. The chart is the only path configuration reaches the estate

`kubectl set image` keeps pods on current code and applies **no configuration**. When helm cannot
write, config silently stops arriving while images keep moving — which is how LH-064 ran signing code
for a day with neither half of its credential pair. A signer's pair comes from the release:
`RASK_SIGNING_IDENTITY` (its service-account subject) is rendered on its Deployment, and the `signing-key-<key name>`
secret (the ServiceAccount's name, `lance.signingKeys`) is
seeded into the dev OpenBao by the seed the chart renders (the platform's store, which the chart does not mint into, is
provisioned by its operator and attested by `signing.provisioned`).

- Before believing a chart commit landed: `make helm-history` — if the top revision predates the
  commit, it did not.
- The release stores image tags. A chart apply after a `kubectl set image` roll **reverts the roll**
  unless the tag is pinned on the command: `--set image.tags.lance-rest-catalog=<tag>`.
- Every helm call goes through `scripts/helm.sh`.
- **The estate's tenants are chart values too** ([[XC-213]], ADR 0206). `catalog.declared` (`projects`, `warehouses`,
  `grants` to principals that are not people, a map keyed by a name its values file chooses so several files merge
  ([[XC-221]]); the default declares the project `default`, administered by
  `auth.bootstrapAdmin` when it names no admin), with the estate's own grants (`estate.admins`: `auth.bootstrapAdmin`;
  `estate.roles`: `lance.fgaEstateRoles`, [[XC-011]]; no hook writes OpenFGA), renders to the ConfigMap
  `<release>-catalog-declaration`
  (`templates/catalog-declaration.yaml`, key `declaration.json`, built by `lance.declaration` in `_helpers.tpl`). The
  catalog mounts it as a VOLUME at `/etc/rask/declaration` (`LANCE_DECLARATION_FILE`), never through env, so a changed
  declaration reaches the running pod with no roll (the kubelet refreshes the file), and the
  `catalog-declaration-converge` timer (`catalog.declarationConverge.periodSeconds`, 60 s) converges the estate onto it.
  Read a declared change back on the catalog (`managed_by: "git"` on the project or warehouse read, the
  `declaration_converged` log line), not on the Deployment's revision: none moves. `docs/OPERATORS.md` § 2 is the
  procedure. Pinned by `tests/chart/test_the_chart_declares_the_tenants_the_catalog_converges.py`.
- **`services.lineage.provenanceNamespace` must name a namespace of the estate's own warehouse**
  (`lance_catalog$<namespace>`, default `lance_catalog$provenance`): `lance.provenanceNamespace` fails the render on
  any other, and the catalog refuses to boot on one. The first segment of every name is its warehouse ([[LH-593]]).
  There is no `catalog.warehouses` value and no `auth.lockRootCreate`: every namespace is created inside a warehouse,
  and nothing reads either key (the schema refuses unknown keys only at the root, so a stale overlay setting one renders
  silently).

## 6. Adopted — the generic baseline that fits

- **Security context**, per container and pod: `runAsNonRoot: true`, `runAsUser` explicit,
  `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]`,
  `seccompProfile.type: RuntimeDefault`. Uneven application of these is backlog row XC-061.
- **A container passes `args`, never `command`.** A container's `command` replaces its image's ENTRYPOINT, and each
  image's ENTRYPOINT is its tini, so the charts pass uvicorn's command line only as `args` ([[LH-313]]): as PID 1, uvicorn
  loses its closing SIGTERM re-raise and no SIGKILL sent from inside the pod reaches it, which criterion 5's catalog
  drill needs (`tests/chart/test_uvicorn_drains_inside_its_pods_grace_period.py`). Keeping the image's init this way holds
  whatever renders the pod, the chart now and an operator later.
- **`serviceAccount.automount: false`** unless the pod genuinely calls the Kubernetes API. Here it is
  not a switch: every first-party pod runs as its own SA from `templates/security-sa.yaml`, tokenless
  on the SA object; a first-party pod mounts a token only where a binding gives its SA work: the pods
  whose templates carry their own RBAC (§8).
- **`values.schema.json`** — validates the MERGED values at install and upgrade, so a value of the wrong type fails
  before any pod starts. Its root admits only the top-level keys `values.yaml` declares and Helm's `global`
  (`--schema-root.additional-properties=false` in `scripts/chart_docs.sh`, [[XC-194]]), so `--set catalogg.replicas=1`
  or a dead overlay key fails the render. A misspelled or missing key below the root does not: the schema carries no
  `required` and no nested `additionalProperties: false` (XC-086 #289). rask's is generated from `values.yaml`'s types and `# --` comments by helm-values-schema-json
  (`make chart-docs`, beside the README; `make chart-docs-check` fails on a stale one). It refuses a number passed as a
  string (measured 2026-10-12: a `--set-string` float re-imposed every `make k3s-up` by `-f <live values>`); `--set`
  keeps a float a string, so set one with `--set-json`. It costs ~49 KB of the release.
- **Lint and the empty-cluster install run in Dagger**: `dagger call charts` lints through the helm Dagger module
  (disaster37/dagger-library-go/helm, pinned) on the pinned helm, and `dagger call install-from-empty` installs the
  fixtures then the chart onto an empty k3s until every workload is Ready ([[XC-133]]).
- **Standard labels** (`app.kubernetes.io/name|instance|version|managed-by`, `helm.sh/chart`) through
  a helper, never hand-built per template.
- **Chart `version` follows semver** and moves when the chart changes. rask's reads `0.3.0` on every
  revision (XC-055), so no revision can be told apart by what it deployed.
- **Scanners** over the rendered output: `helm lint --strict`, `trivy config` (already
  `make scan-config`), and — not yet wired — `kubescape`, `polaris audit`, `pluto detect` for
  deprecated APIs.

## 7. Overridden — do NOT import these from generic Helm advice

The owner's rule: never a secret through env; a credential comes from ESO, the secret store or STS, for zero
trust.

| generic advice | here |
|---|---|
| `env: valueFrom: secretKeyRef` marked ✅ "secrets from secretKeyRef" | **Not approved.** Whether an ESO-written Secret delivered this way satisfies the rule is backlog XC-002, awaiting a ruling. Default: a working secret never enters process env. |
| `helm-secrets` / SOPS: encrypted secrets in a values file | **No.** A chart value never carries a secret, encrypted or not. A record NAMES a secret. |
| `templates/secret.yaml` rendering a Secret from `.Values` | **No.** Credentials come from the secret store (the platform's Vault or OpenBao; the dev OpenBao fixture): a service reads its own under its own login, and every other pod gets an ExternalSecret. |
| a literal `value:` in a Job's env for a credential | **No** — readable by anyone with `get jobs`; a pod mounts a secret as a file ([[XC-004]]), and a workload's object-store identity is no secret at all but its projected ServiceAccount token ([[XC-084]]). |
| Docker-based chart tooling | **Dagger.** `dagger call charts` runs the chart gate. |

## 8. `default` is a shared identity, and a subchart can hand it every Secret

Any pod with no `serviceAccountName` runs as the namespace's `default` SA, and so does every such pod
of every subchart. A grant to `default` is a grant to all of them, and a platform chart can ship exactly that: a
`secrets: get` Role bound to `default` for a built-in secret store of its own. Measured live 2026-09-25 (audit,
read-only): `kubectl auth can-i get secrets --as=system:serviceaccount:default:default` answered yes, and the catalog,
lineage, maintenance, OpenBao and Dex, among others, ran as `default` with a mounted token.

- **Every first-party pod names its own SA**, and the NATS fixture's server and nats-box run as their own
  (`serviceAccount` values in deploy/fixtures/nats). A shared SA is allowed only where the sharers are one identity
  (`sa-maintenance` for the maintenance service); one-shot Jobs share `sa-jobs`.
- **OpenFGA's pod runs as a tokenless account rask renders** ([[XC-156]]): the subchart's own ServiceAccount mounts an
  API token and binds a Job-status reader with no switch for either, so `openfga.serviceAccount` is `create: false`,
  `name: <release>-openfga`, and `security-sa.yaml` renders that account with `automountServiceAccountToken: false`.
  `openfga-authn.yaml` refuses a render where that name, or the `openfga-config` volume's Secret
  (`<release>-infra-credentials`, [[XC-036]]), names anything but this release's. No subchart pod may mount a token: the
  identity test's `a subchart's pod holding an API token` bucket.
- **A binding names its subject through the helper the pod uses**, never through a free value with a
  `default` fallback: an unset value then puts the grant on the shared identity. Likewise a pod names its SA with the prefix `security-sa.yaml`
  renders it with, `lance.fullname` (the release name). `rask.fullname` is pinned to `rask` by
  `fullnameOverride`, so under any other release name the two diverge and the pod is refused at
  admission (measured 2026-10-02: 14 pods on a render as release `foo`).
- **An identity is never a hook** ([[XC-126]]): no ServiceAccount, Role, ClusterRole or binding carries
  `helm.sh/hook`. A hook-shaped identity is deleted by `hook-succeeded` while a failed hook's pod still holds
  its token (every later hook on it 401s), and exists only during its own phases. `rask-sa-hooks`, the identity
  of the pre-manifest hooks (`openfga-model`, `object-store-provisioning`), is an ordinary SA, so a `pre-upgrade` hook
  finds it from the previous revision. The one upgrade that INTRODUCES an ordinary SA runs its `pre-upgrade`
  hooks before the manifest creates it (measured 2026-10-02 on rev 259: `serviceaccount "rask-sa-jobs" not
  found`, the release stuck `pending-upgrade`): create it by hand first and let `make k3s-up`'s
  `--take-ownership` adopt it. The gate is the identity test of
  `tests/chart/test_a_first_party_pod_cannot_read_a_secret_through_the_kube_api.py`: the `identities rendered as hooks`
  bucket, and a pre-upgrade hook may run only as an SA in its `_ESTABLISHED` set, so naming a new one is a
  deliberate edit there, which is the reminder to pre-create it.
- **A hook that removes what git no longer declares runs post-upgrade** ([[XC-083]], ADR 0170).
  `object-store-provisioning.yaml` renders one Job per phase, `range $phase := ternary (list "install" "post-install")
  (list "pre-upgrade" "post-upgrade") .Release.IsInstall`: the provisioning run (a plain object on an install, a
  `pre-upgrade` hook on an upgrade) provisions what the new revision's pods need before they roll and prunes nothing,
  because the previous release's pods still present what a prune would remove; the prune run
  (`<release>-object-store-provisioning-prune-<rev>`, `post-install`/`post-upgrade`) runs once the manifest has replaced
  them and removes every store user and rask-owned policy the chart does not declare. Under Argo CD, which renders every
  sync as an install, they are a `Sync` and a `PostSync` hook ([[XC-164]], `lance.reconciledJob`). Helm's `ternary`
  evaluates both arms, so neither may fail on the other's input.
- **Every workload's store identity is a projected token, named by one value** ([[XC-083]], [[XC-084]], ADR 0170,
  ADR 0197): `objectStore.identity.audience` (`lance.objectStoreAudience`, `required`) is both the token the catalog,
  lineage, maintenance and each backup CronJob mount through `lance.identityTokens` and the client id the dev store's
  provider trusts (`deploy/fixtures/rustfs` `identityProvider.clientId`). Each signs with sessions it exchanges that
  token for (`lance.objectStoreSessionEnv`: `<PREFIX>_S3_WEB_IDENTITY_TOKEN_FILE`, `_STS_ENDPOINT`, `_ASSUME_ROLE_ARN`
  from `objectStore.identity.roleArns.<component>`, `_SESSION_SECONDS`), and holds no key pair. The policies render in
  every mode in the ConfigMap `<release>-object-store-identities`, one `<release>-sa-<component>.json` per identity
  (`lance.objectStoreIdentities`, `lance.objectStorePolicy`); the hook writes each under its account
  (`lance.saAccount`), the token's `sub`, and binds no user to any, and a platform store's operator creates them.
  `tests/chart/test_a_provisioned_identity_is_one_the_service_uses.py` holds each workload's token, its policy and the
  store's provider equal.
- **Changing the dev OpenBao's pod spec empties its store**, because its server keeps it in memory. The server is not
  `server -dev` (which prints its root token and unseal key to its log): it starts uninitialized, and the seed beside
  it initializes it, mints the store's administrator under an id the fixture generates in-cluster (`rask-openbao-root`,
  mounted into the seed alone) and revokes the init token, so no token is published anywhere ([[XC-165]], ADR 0169).
  `bao` in the seed container is that administrator (`$HOME/.vault-token`); run an operator's `bao` there.
  The pod is the openbao fixture's ([[XC-131]]); its `mint` and `seed` run the scripts rask renders into
  `<release>-openbao-seed`, and a change rask makes to them reaches the running seed in place (it mints a pair for any
  new identity, issues the static bus users and the scoped signing keys again under the same root, and seeds again), so a rask upgrade replaces no store. A change to the fixture's own pod spec does replace it, and the replacement
  honours no token the old store issued, a surge included. A service asks the store about its own token every 15 s and
  logs in again on the pass that finds it refused, so it reads again within a pass of the replacement admitting it
  ([[XC-199]], `lakehouse.governed.secrets`; pinned offline by
  `packages/lakehouse/tests/test_a_process_reads_its_secrets_from_the_store_as_itself.py`, and measured on the estate
  2026-11-14, helm rev 8: after `kubectl rollout restart deploy/rask-openbao` all six pods logged in again 22 to 27 s
  later, none restarted or left Ready, and readiness answered `degraded` for 5 s). The store's replacement alone
  restarts no pod; one that minted a new bus root does (below, docs/OPERATORS.md § 6 and § 7).
  The `seed` container in that pod writes only to its own server and gates the pod's readiness on a
  key it writes last, and a rollout surges first so the seed can carry each signing identity's Ed25519
  pair (`signing-public-<key name>`, then `signing-key-<key name>`) over from the outgoing pod
  (`test_the_dev_openbao_is_seeded_by_its_own_pod.py`); a `mint` init container on a memory volume makes
  the candidates. A replacement with no outgoing pod (a deleted pod, a drained node) mints every pair
  afresh: the old public keys go with the old store, and an enforcing lineage refuses what was signed
  before the loss. Signers re-resolve their key within 5 minutes and heal in place, so restart the signers
  and lineage after a non-surge replacement rather than waiting (docs/OPERATORS.md § 6). values.yaml
  `signing:` holds the tiers, the rotation and the store-loss cases, and the platform's store is
  refused at render until `signing.provisioned` attests that an operator created them.
- **The dev OpenBao also mints the NATS trust root** ([[XC-078]], [[XC-199]]): the seed carries `nats-root` and `nats-route`
  over a surge and, under that root, converges the bus for the issued table (`lance.natsTable`: values.yaml
  `nats.auth.users` plus each `nats.auth.flagged` grant whose flag is on) before its readiness key. **A session user**
  (a row with a ServiceAccount: the lakehouse's own, `lance.natsSessions`) gets NO credential and no `nats-user-<user>`:
  its pod presents its projected ServiceAccount token (`nats.auth.audience`) as the connection's token, and the bus's
  signer answers with a session of at most `nats.auth.sessionSeconds` signed by that user's scoped signing key in APP (one
  per session user, role = the user's name, scope = the row; a changed row re-scopes the same key, a user that stops being
  a session user loses its key and its sessions). The seed also converges the AUTH account (the signer's user `callout`, a
  bearer `sentinel` denied every subject as the server's `default_sentinel`, one signing key for the signer's answers, the
  callout naming `callout` and APP) and writes `nats-callout` (the signer's keys, read by ESO alone) and
  `nats-server` with the `default_sentinel`. **A static user** (a row with no ServiceAccount: `monitor`, `object-store`, a
  client's) keeps or is issued `nats-user-<user>`: it keeps its key pair and JWT across reseeds while its grants match; a
  changed grant is reissued under the same key and the old JWT revoked `--at` its issue time; a stored user whose seed is
  unreadable gets a fresh pair and its old key revoked `--at` the stored JWT's `iat` (`NATS user <u>: replaced`), the
  seed stopping only on a stored JWT that names no key; and the APP account's `*` revocation sits one second before the
  oldest JWT a static user holds, taking back whatever earlier reseeds left (measured 2026-11-07, nats-server 2.14.2, nsc
  2.15.0, nats.go 1.39.1: a client holding a revoked JWT is closed after two refused reconnects, the state a service's own
  connection opens again from, in-process, `lakehouse.events.bus`; the maintenance worker ends its process instead and starts
  again, `TaskQueue.connect`); a non-dev store waits for `nats.auth.provisioned`, which attests that the platform's signer
  admits each session user of `<release>-nats-permissions` and that each static user's secret exists. **The seed revokes
  every static APP bus user the release stops issuing**: it lists the `nats-user-*` secrets in its own store and the one
  behind the Service, reads each unissued user's key out of its stored JWT, adds it to the APP account's revocations
  (carried to the bus in `resolver_preload`), and deletes its secret from this pod's store, since a user JWT carries no
  expiry and the account that signed it stays trusted. A revocation reaches the running bus at its config reload:
  measured 2026-11-07 on nats-server 2.14.2 with nsc 2.15.0, a SIGHUP (the bus fixture's reloader sends one when its auth
  Secret changes) applies it and drops a connected revoked client, and a restart keeps it. **The seed also deletes every
  store role and policy the release no longer renders** (all but `default` and `root`), so an identity git stopped
  declaring logs in to nothing. The NATS fixture's ExternalSecrets read `nats-server`, `nats-route`, `nats-callout` and
  `nats-user-monitor` through rask's SecretStore, so the dev store's ESO policy grants them (`lance.natsFiles`). The
  chart renders the session users' table as ConfigMap `<release>-nats-permissions` (`permissions.json`) in every mode,
  for the signer. With the fixture's `nats.auth.server` on, a non-surge OpenBao replacement mints a new root that the
  operator-mode server refuses until the NATS pods restart, which empties JetStream: docs/OPERATORS.md § 7 has the
  switch-over and recovery.
- **The chart renders no secret store; production names the platform's** ([[XC-149]]): `openbao.enabled` means the
  estate uses the dev OpenBao fixture (a real deployment refuses it), and off it the render requires `openbao.externalAddr`
  (https only: every service logs in to it with its pod's ServiceAccount token; a store under a
  private CA names it in `openbao.ca.configMap`, which reaches the services and ESO) and, under
  `networkPolicy.enabled`, `openbao.egress`, a rule admitting exactly the pods `lance.secretScopes` names. Setting both is
  refused. The dev store's
  seed stays rask's (owner, 2026-10-16): only rask's values know what to seed.
- **Every endpoint the platform serves outside the namespace is one row of `lance.platformEgress`** ([[XC-202]]): its
  egress value, what it reaches, the components that dial it, and when it is required (the API server, OpenFGA, the
  secret store, the telemetry backend, the S3 and STS with `objectStore.enabled` off, the IdP with `dex.enabled` off, and
  on a real deployment the bus and lineage's graph backend). Under `networkPolicy.enabled` the render refuses every empty
  row in ONE message naming them all, and the same rows render the egress policies, so a new endpoint is a new row, never
  `networkPolicy.extraEgress` (kept for an endpoint no row names). `lance.egressRules` refuses a rule with no `to` peers
  or a `/0` peer. The development stores' ingress admits exactly their callers from the same helpers
  (`lance.objectStoreClients`, `lance.busClients`), plus each fixture's own Job and, on the bus, the development object store's server, which publishes each change on `store.events`.
- **Who reads which secret is one table, `lance.storeReads`** ([[XC-165]], ADR 0169): per app, its ServiceAccount and
  the secrets its code reads. Each service logs in to the store ITSELF
  ([[XC-199]], `lakehouse.governed.secrets`): Kubernetes auth as its role `<release>-<app>`, which `lance.secretStoreEnv`
  names in its environment (`RASK_SECRET_STORE_ADDR`, `_ROLE`, `_AUTH_MOUNT`, `_KV_MOUNT`, `_TOKEN_FILE`), with a token
  projected for `openbao.identity.audience` into its own container. Each role's
  policy reads exactly that app's reads (`lance.storeIdentity` mounts the token); a lakehouse app's list holds no bus credential, since its bus connection presents its ServiceAccount token (`lance.busIdentity`). The roles and policies render in every
  mode as `role-*.json`/`policy-*.json` in `<release>-secret-store-identities`, which the dev seed applies and a platform
  store's operator creates. A new secret a service reads is a row there, or the store's policy refuses its read.
  A client reads through the same table from `openbao.externalReaders` (`<app>: {serviceAccount, secrets}`,
  supplied from the client's own repository, empty by default), and it is an allowlist: an external reader `<app>` may hold only
  `nats-user-<app>` (refused when `<app>` is a session user, which has no such secret), names of its own (`<app>-<name>`) and `signing-public-<name>`. The render refuses an app id that is
  no lowercase name, a lakehouse app (catalog, lineage, maintenance, even disabled), one whose own names would cover a family
  the store issues per identity (`nats-user-<name>` for every static user of the issued table, a client's own included,
  `signing-key-<name>`, `signing-public-<name>`), a name ESO, a lakehouse identity or the dev store's seed holds
  (`nats-root`, `object-store-root`, `postgres`, `*-s3`, …) or another reader's id, or one whose role
  `<release>-<app>` is ESO's (`externalSecrets.role`); a ServiceAccount that is no name, one the lakehouse renders
  (`lance.serviceAccounts`) or `default`; and an empty or non-name secret list. An external reader gets a
  store role and policy. The gate is
  `tests/chart/test_every_secret_in_the_dev_store_has_exactly_its_readers.py`.
- **OpenFGA is optional, production names the platform's, and the chart runs no Postgres** ([[XC-132]], ADR 0228):
  `deploy/values-prod.yaml` sets `openfga.enabled` and `postgres.enabled` off and `auth.fgaApiUrl` (the render refuses
  it empty), so the render names no Postgres. The chart reads `postgres.*` only while it runs OpenFGA: the development
  fixture under `postgres.enabled`, else the platform's `postgres.externalHost`. Every FGA client dials
  `lance.openfgaUrl` and every Postgres client `lance.postgresHost`; a template never builds either address itself. The OpenFGA subchart's schema refuses unknown keys, so a rask value
  about OpenFGA lives under `auth`, never under `openfga`.
- **A real deployment is refused what would expose it** ([[XC-184]]): `templates/prod-credentials.yaml`, on
  `rask.isRealDeployment`, refuses a published dev credential, `auth.enabled=false` (the chart would write the services'
  `RASK_INSECURE_ALLOW_UNAUTHENTICATED` itself), and every platform HTTP address it names that is not https: `auth.fgaApiUrl`, `objectStore.externalEndpoint`,
  `vending.endpoint`, `observability.export.endpoint`, `services.lineage.forward.url`. A new platform address joins
  that list with what would cross it. The secret store's and the IdP's https refusals sit in their own helpers.
- **OpenFGA admits only a projected `rask-openfga` token from a listed ServiceAccount** ([[XC-077]]): OIDC
  against the cluster SA issuer, keys fetched through the issuer mirror in `templates/issuer-mirror.yaml`
  (OpenFGA fetches discovery with no bearer and k3s answers that 401; the dev object store reads the same mirror), the subject allow-list from
  `lance.openfgaSubjects` (a ConfigMap OpenFGA reads at start; the pod names the same clients in
  `openfga.podAnnotations."rask.dev/openfga-clients"`, which the render holds equal to `lance.openfgaClients`, so a
  change to them rolls OpenFGA at the next reconcile; maintenance is not on it, since it holds no OpenFGA token and asks
  the catalog's grant doors, ADR 0197),
  playground off, CORS closed. A pod that names OpenFGA renders all three parts of `lance.fgaToken` (env,
  volume, mount) under the condition its URL has, and `lance.openfgaSubjects` must list its account under
  the same condition. A list-valued `OPENFGA_*` env is comma-joined (measured on v1.18.3). An operator tool
  mints a token with `kubectl create token <release>-sa-hooks --audience rask-openfga`. The gate is
  `tests/chart/test_openfga_admits_only_a_projected_service_account_token.py`; `openfga-authn.yaml` fails
  the render when OpenFGA's audience, issuer alias or subject source drifts from the clients'.
- **The gate is the render, judged by what each role grants**, so a new subchart that binds a
  secret-reading or token-reviewing role to `default` fails without anyone listing it:
  `tests/chart/test_a_first_party_pod_cannot_read_a_secret_through_the_kube_api.py`. A `User` subject
  named `system:serviceaccount:<ns>:<sa>` counts as that SA. The gate carries its own mutation check:
  a template binding a `secrets: get` Role to `default` must fail it.
