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

Measured 2026-09-25 on revision 240 (96.3% of the ceiling):

| part | share of the packed ceiling |
|---|---|
| rask's own 63 templates | ~50% — 299 KB of it `{{/* */}}` comments, stored in full every revision |
| Kueue's templated CRDs (manifest) | ~24% |
| the other seven subcharts together | ~5.5% |

With every operator, the compute engine, the telemetry chain and the development stand-ins out of the chart (ADR 0153,
[[XC-124]] to [[XC-132]]), revision 316 measured 335,260 packed bytes on 2026-10-12 with `values.schema.json` in the chart: **32.0%** of the ceiling.

**`values.yaml` prose is free** — it is stored as a parsed map, comments dropped. **Template prose is
not**, so templates carry no `{{/* */}}` comments ([[XC-123]], ADR 0153): a template's rationale lives in
`docs/chart/templates/<template>.md`, one section per template line it explains, and each setting carries a
one-line `# --` comment directly above its key in `values.yaml`, from which helm-docs generates
`chart/README.md` (`make chart-docs`; `make chart-docs-check`, in `make check` and `dagger call charts`,
fails on a stale README or a value with no description). A section banner in `values.yaml` is `# ===`, never
`# ---`: helm-docs reads any `# --` line as the start of a description. The environment overlays live in
`deploy/` (`values-local.yaml`, `values-prod.yaml`, `values-live-pins.yaml`), outside `chart.files`. Moving
the 647 comment blocks out took the packed release from 98.1% to about 70% of the ceiling, with a
byte-identical render. A YAML `#` comment inside a template is different: it is part of the rendered
manifest (1,024 such lines, mostly in embedded scripts), so moving one changes what is deployed. An empty `{{/**/}}` left in a template is load-bearing whitespace control: deleting it
lets a neighbouring `-}}` trim text the comment used to shield.

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

**It has hit the ceiling three times**, each time answered by trimming rather than by removing what
fills it. v35 on 2026-08-15 (`HELM_DRIVER=sql`, then CNPG's CRDs moved out in `b56a49c9`); the SQL
store was dropped on 2026-09-08 when it split-brained against the Secret store (SQL rev 42 vs Secret
rev 108). v194 on 2026-09-21 (template YAML comments turned into `{{/* */}}`, `80783647`). v239 on
2026-09-24 (`.helmignore` and the `required` guards, `c352a232`/`4bff1036`). `docs/adr/0038-helm-release-storage-the-sql-driver-stands-the-chart-is-not.md`'s
2026-08-15 entry attributes the size to `chart/charts/*.tgz`; by Helm's source those bytes are never
stored, so that entry's premise is false and its chosen answer no longer applies.

## 2. Operators do not belong in the app chart

Helm's own CRD guidance gives two methods: a non-templated `crds/` directory (installed once, never
upgraded or deleted), or the CRDs in a **separate chart installed on its own**. Operators are the
second case. Kueue's upstream docs only ever install it as its own release in `kueue-system`.

- **CNPG is the precedent** (`b56a49c9`, 2026-08-19): its 1.2 MB CRD file moved to
  `chart/crds-bootstrap/`, `.helmignore`d, applied out-of-band by `make k3s-crds`. The operator followed as a
  prerequisite ([[XC-130]]): the `cloudnative-pg` fixture, `crds.create: false`, and `age-cluster.yaml` refuses to render
  a `Cluster` for a cluster that does not serve `postgresql.cnpg.io/v1`.
- **Kueue left the chart ([[XC-126]])**, as BYO compute: its 11 CRDs, annotated keep and with their conversion
  webhook repointed at `kueue-system/kueue-webhook-service`, are served by the cluster's own Kueue (not a rask
  release, and no rask fixture). What follows is why it could not stay as a vendored copy.
- **Kueue cannot follow it from inside the umbrella.** It templates its 11 CRDs under
  `templates/crd/` with no switch — 0.18.1 and 0.19.6 alike, and the upstream request to move them was
  closed as not-planned. The conversion webhook's service name and namespace are templated, so a
  vendored copy must be pre-rendered with release `rask` and namespace `default` baked in.
- **Kueue is a workload concern in a platform seam.** `values.yaml` sizes it as "how many projects
  transcribe at once" — one runner's GPU admission. No rask template sets `kueue.x-k8s.io/queue-name`,
  and rask's own queue has admitted zero workloads.
- `docs/adr/0038-helm-release-storage-the-sql-driver-stands-the-chart-is-not.md` (2026-08-15) already calls infra-separated-from-app "the architecturally correct
  answer and … the intended end state". It was deferred, not rejected; ADR 0153 decides it.
- **What leaves the chart lands in `deploy/fixtures/`** (ADR 0153, [[XC-124]]): one directory per prerequisite, an
  upstream chart pinned in `fixture.env`, installed as its own release by `scripts/fixtures.sh` before rask's
  (`make k3s-fixtures`, a prerequisite of `make k3s-up`; `FIXTURES` names which, default `external-secrets dapr nats
  cloudnative-pg dex rustfs openbao kuberay-operator ray-cluster compute observability marquez`: Dapr's control plane in dapr-system ([[XC-128]]), the NATS
  JetStream bus `rask-nats` in rask's namespace that `nats.url` names ([[XC-129]]), the CloudNativePG operator in
  cnpg-system that the AGE-on-CNPG `Cluster` needs, its CRDs still applied by `make k3s-crds` first ([[XC-130]]), the dev OIDC issuer `rask-dex`, object store `rask-rustfs` ([[XC-163]], ADR 0168) and secret store `rask-openbao` ([[XC-131]]; both stores' fixtures are `WAIT=false`: the object store's root credential and the secret store's seed scripts are rask's), the dev Ray engine
  ([[XC-125]]) and its companion `compute` service (a local chart, release `compute` in rask's namespace, `WAIT=false`:
  it reads rask's Dapr token, Dex, catalog and Collector by reference, and asks the catalog's decision door, which
  answers its account only through `auth.externalDeciders`), the telemetry chain the Collector exports to ([[XC-127]]), and the OpenLineage graph backend
  `rask-marquez` lineage's forwarder sends to (ADR 0158); a GPU node with no device plugin yet adds `nvidia-device-plugin`). A fixture's `SET_FILES` passes an
  artefact from outside its chart with `--set-file` (the alert rules), and a local fixture chart with upstream
  `dependencies:` is vendored from its own `Chart.lock` (`scripts/fixtures.sh --vendor`, run by `make k3s-deps`, so a
  test can render it through `chart_render.render_fixture`). The e2e lanes install ESO, Dapr and NATS through the same
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
ClusterQueues, 2 LocalQueues, 2 ResourceFlavors and 8 live Workloads in `htr-batch`.

Before ANY change that removes a CRD-owning subchart:

```bash
kubectl annotate crd <each crd> helm.sh/resource-policy=keep
```

then hand them to the new owner (`helm install … --take-ownership`). CNPG's CRDs carry
`resource-policy: keep` already, and Kueue's carried it before rask's release stopped rendering them
([[XC-126]]).

## 4. Upgrade flags — `--reset-then-reuse-values`, never `--reuse-values`

`--reuse-values` keeps the previous release's values and **ignores this chart's defaults**. A key
added after that release arrives nil, and `{{ .Values.x | quote }}` renders nil as `""`. On 2026-09-25
that put `MAINTENANCE_RECYCLE_AT_MEMORY_FRACTION=''` into the maintenance worker and pydantic refused
it at boot (CrashLoopBackOff, contained only because the rolling update kept the old pods serving).

- Use `--reset-then-reuse-values` (helm ≥ 3.14; this estate runs 3.20): chart defaults, then the
  release's values, then any `--set`.
- Guard every value a typed setting depends on with `required`, so absence fails the RENDER with a
  message rather than a pod: see `LANCE_ERASURE_RELEASE_GRACE_DAYS` in `chart/templates/services.yaml`.
- `helm diff upgrade` shows a `''` before it ships.
- Never kill a running upgrade — it leaves the release in `pending-upgrade`, which refuses every later
  one until `helm rollback`.

## 5. The chart is the only path configuration reaches the estate

`kubectl set image` keeps pods on current code and applies **no configuration**. When helm cannot
write, config silently stops arriving while images keep moving — which is how LH-064 ran signing code
for a day with neither half of its credential pair. A signer's pair comes from the release:
`RASK_SIGNING_IDENTITY` (its service-account subject) is rendered on its Deployment, and the `signing-key-<key name>`
secret (the ServiceAccount's name, `lance.signingKeys`) is
seeded into the dev OpenBao by the seed the chart renders (the platform's store, which the chart does not mint into, is
provisioned by its operator and attested by `signing.provisioned`).

- Before believing a chart commit landed: `helm history rask` — if the top revision predates the
  commit, it did not.
- The release stores image tags. A chart apply after a `kubectl set image` roll **reverts the roll**
  unless the tag is pinned on the command: `--set image.tags.lance-rest-catalog=<tag>`.
- Every helm call goes through `scripts/helm.sh`.

## 6. Adopted — the generic baseline that fits

- **Security context**, per container and pod: `runAsNonRoot: true`, `runAsUser` explicit,
  `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]`,
  `seccompProfile.type: RuntimeDefault`. Uneven application of these is backlog row XC-061.
- **A container passes `args`, never `command`.** A container's `command` replaces its image's ENTRYPOINT, and each
  image's ENTRYPOINT is its tini, so the charts pass uvicorn's command line only as `args` ([[LH-313]]): as PID 1, uvicorn
  loses its closing SIGTERM re-raise and no SIGKILL sent from inside the pod reaches it, which criterion 5's catalog
  drill needs (`tests/unit/test_uvicorn_drains_inside_its_pods_grace_period.py`). Keeping the image's init this way holds
  whatever renders the pod, the chart now and an operator later.
- **`serviceAccount.automount: false`** unless the pod genuinely calls the Kubernetes API. Here it is
  not a switch: every first-party pod runs as its own SA from `templates/security-sa.yaml`, tokenless
  on the SA object; a first-party pod mounts a token only where a binding gives its SA work: the pods
  whose templates carry their own RBAC (§8).
- **`values.schema.json`** — validates the MERGED values at install and upgrade, so a mistyped key fails
  before any pod starts. rask's is generated from `values.yaml`'s types and `# --` comments by helm-values-schema-json
  (`make chart-docs`, beside the README; `make chart-docs-check` fails on a stale one). It caught a live `--set-string`
  float on its first deploy (2026-10-12: `recycleAtMemoryFraction: "0.70"`, re-imposed every `make k3s-up` by `-f
  <live values>`); `--set` keeps a float a string, so set one with `--set-json`. It costs ~49 KB of the release.
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

The owner's rule, verbatim: *"Never secret through envs. Either from ESO, secret store dapr and STS
for zero trust."*

| generic advice | here |
|---|---|
| `env: valueFrom: secretKeyRef` marked ✅ "secrets from secretKeyRef" | **Not approved.** Whether an ESO-written Secret delivered this way satisfies the rule is backlog XC-002, awaiting a ruling. Default: a working secret never enters process env. |
| `helm-secrets` / SOPS: encrypted secrets in a values file | **No.** A chart value never carries a secret, encrypted or not. A record NAMES a secret. |
| `templates/secret.yaml` rendering a Secret from `.Values` | **No.** Credentials come from the secret store (the platform's Vault or OpenBao; the dev OpenBao fixture) via the Dapr secret store or ESO. |
| a literal `value:` in a Job's env for a derived credential | **No** — readable by anyone with `get jobs`; `object-store-scoped-users.yaml` derives each one into a memory file in-cluster ([[XC-004]]). |
| Docker-based chart tooling | **Dagger.** `dagger call charts` runs the chart gate. |

## 8. `default` is a shared identity, and a subchart can hand it every Secret

Any pod with no `serviceAccountName` runs as the namespace's `default` SA, and so does every such pod
of every subchart. A grant to `default` is a grant to all of them. Dapr's chart ships exactly
that: `dapr_rbac.secretReader` (`enabled: true, namespace: default`) binds `secrets: get` to
`default`, for daprd's built-in Kubernetes secret store. Measured live 2026-09-25 (audit, read-only),
while Dapr was still a subchart of rask's release:
`kubectl auth can-i get secrets --as=system:serviceaccount:default:default` answered yes, and the
catalog, lineage, maintenance, the medallion, OpenBao and Dex ran as `default` with a mounted token.

- **The Dapr control plane is a prerequisite ([[XC-128]]), and the dev fixture sets
  `dapr_rbac.secretReader.enabled: false`** (deploy/fixtures/dapr/values.yaml). No sidecar uses that store:
  every injected pod sets `dapr.io/disable-builtin-k8s-secret-store`.
- **Every first-party pod names its own SA**, and the NATS fixture's server and nats-box run as their own
  (`serviceAccount` values in deploy/fixtures/nats). A shared SA is allowed only where the sharers are one identity (`sa-web`
  for the zones, `sa-maintenance` for the maintenance service); one-shot Jobs share `sa-jobs`.
- **A binding names its subject through the helper the pod uses**, never through a free value with a
  `default` fallback: an unset value then puts the grant on the shared identity. Likewise a pod names its SA with the prefix `security-sa.yaml`
  renders it with, `lance.fullname` (the release name). `rask.fullname` is pinned to `rask` by
  `fullnameOverride`, so under any other release name the two diverge and the pod is refused at
  admission (measured 2026-10-02: 14 pods on a render as release `foo`).
- **An identity is never a hook** ([[XC-126]]): no ServiceAccount, Role, ClusterRole or binding carries
  `helm.sh/hook`. A hook-shaped identity is deleted by `hook-succeeded` while a failed hook's pod still holds
  its token (every later hook on it 401s), and exists only during its own phases. `rask-sa-hooks`, the identity
  of the pre-manifest hooks (`openfga-model`, `object-store-scoped-users`), is an ordinary SA, so a `pre-upgrade` hook
  finds it from the previous revision. The one upgrade that INTRODUCES an ordinary SA runs its `pre-upgrade`
  hooks before the manifest creates it (measured 2026-10-02 on rev 259: `serviceaccount "rask-sa-jobs" not
  found`, the release stuck `pending-upgrade`): create it by hand first and let `make k3s-up`'s
  `--take-ownership` adopt it. The gate is that test file's identity test: the `identities rendered as hooks`
  bucket, and a pre-upgrade hook may run only as an SA in its `_ESTABLISHED` set, so naming a new one is a
  deliberate edit there, which is the reminder to pre-create it.
- **A tokenless daprd needs `dapr.io/disable-builtin-k8s-secret-store`.** daprd initialises its built-in
  Kubernetes secret store at boot from the pod's token and treats a failure as fatal, so dropping the
  annotation from `lance.daprSidecarResources` crash-loops every injected pod (live 2026-07-13).
- **A RayCluster applies a new pod spec only under `upgradeStrategy: Recreate`** (KubeRay 1.6.2,
  `shouldRecreatePodsForUpgrade`): without it the head keeps the spec it started with, its SA included,
  until someone deletes the pod. The hash leaves out replicas, so autoscaling recreates nothing, and the
  cost is that a change to the cluster's spec ends the jobs running on it, as any singleton's rollout
  does. KubeRay refuses the field on a cluster a RayService creates. The rask chart renders no RayCluster
  ([[XC-125]]); this holds for the `ray-cluster` fixture (`deploy/fixtures/ray-cluster/chart`), which
  carries the field.
- **Changing the dev OpenBao's pod spec empties its store**, because its server keeps it in memory. The server is not
  `server -dev` (which prints its root token and unseal key to its log): it starts uninitialized, and the seed beside
  it initializes it, mints the store's administrator under an id the fixture generates in-cluster (`rask-openbao-root`,
  mounted into the seed alone) and revokes the init token, so no token is published anywhere ([[XC-165]], ADR 0169).
  `bao` in the seed container is that administrator (`$HOME/.vault-token`); run an operator's `bao` there.
  The pod is the openbao fixture's ([[XC-131]]); its `mint` and `seed` run the scripts rask renders into
  `<release>-openbao-seed`, and a change rask makes to them reaches the running seed in place (it mints a pair for any
  new identity, issues the bus users again under the same root, and seeds again), so a rask upgrade replaces no store.
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
- **The dev OpenBao also mints the NATS trust root** ([[XC-078]]): the seed carries `nats-root` and `nats-route` over a
  surge and re-issues every `nats-user-<user>` of the issued table (`lance.natsTable`: values.yaml `nats.auth.users` plus
  each `nats.auth.flagged` grant whose flag is on) before its readiness key; a non-dev store waits for
  `nats.auth.provisioned`. The NATS fixture's ExternalSecrets read `nats-server`, `nats-route` and `nats-user-monitor` through
  rask's SecretStore, so the dev store's ESO policy grants them (`lance.natsFiles`). With the fixture's `nats.auth.server` on, a
  non-surge OpenBao replacement mints a new root that the operator-mode server refuses until the NATS pods restart, which
  empties JetStream: docs/OPERATORS.md § 7 has the switch-over and recovery.
- **A Dapr Configuration edit reaches a sidecar only when its pod restarts** (HotReload is off), and Helm
  applies a Deployment before the Configuration it names, so an edit under an unchanged name is loaded
  stale by a pod that boots first, for its life. A per-app Configuration is therefore NAMED by the hash of
  its spec, `lance-config-<app>-<10 hex of sha256 of lance.daprAppSpec>` (`lance.daprAppConfigName`, used by
  the object and by the pod's `dapr.io/config`): an edit renames it, which rolls exactly the pods it changed,
  and a pod that boots before the new object exists fails closed (daprd exits on a missing named
  Configuration, Dapr v1.18.1 `pkg/runtime/config.go:252-254`; the injector reads none) and starts once Helm
  has applied it. Read a pod's Configuration from its `dapr.io/config`, never by a name built from the app-id.
  The gates: `tests/unit/test_every_secret_in_the_dev_store_has_exactly_its_readers.py` (who may read which secret, at
  the Dapr scope and at the store) and
  `tests/unit/test_event_signing_is_wired_by_the_chart_in_every_deploy_mode.py` (the name follows the spec).
- **The chart renders no secret store; production names the platform's** ([[XC-149]]): `openbao.enabled` means the
  estate uses the dev OpenBao fixture (a real deployment refuses it), and off it the render requires `openbao.externalAddr`
  (https only: every sidecar logs in to it with its pod's ServiceAccount token) and, under `networkPolicy.enabled`,
  `openbao.egress`, a rule admitting exactly the pods `lance.secretScopes` names. Setting both is refused. The dev store's
  seed stays rask's (owner, 2026-10-16): only rask's values know what to seed.
- **Who reads which secret is one table, `lance.storeReads`** ([[XC-165]], ADR 0169): per app, its ServiceAccount, the
  secrets its code reads (its Configuration's `allowedSecrets`, deny by default) and the ones its components resolve.
  The one `lance-secrets` component logs each sidecar in with Kubernetes auth as `<release>-{appID}` (Dapr fills
  `{appID}` per sidecar; Dapr 1.19 is the floor), with a token projected for `openbao.identity.audience` into daprd alone
  (`lance.storeIdentity`); each role's policy reads exactly that app's reads. The roles and policies render in every
  mode as `role-*.json`/`policy-*.json` in `<release>-secret-store-identities`, which the dev seed applies and a platform
  store's operator creates. A new secret a service reads is a row there, or its read is refused at both layers.
- **OpenFGA and its Postgres are optional, and production names the platform's** ([[XC-132]]): `openfga.enabled` and
  `age.enabled` off, `auth.fgaApiUrl` and `age.externalHost` set (the render refuses either empty). Every FGA client
  dials `lance.openfgaUrl` and every Postgres client `lance.ageConnectHost` (on the AGE-on-CNPG path, `lance-pg-rw`);
  a template never builds either address itself. The OpenFGA subchart's schema refuses unknown keys, so a rask value
  about OpenFGA lives under `auth`, never under `openfga`.
- **OpenFGA admits only a projected `rask-openfga` token from a listed ServiceAccount** ([[XC-077]]): OIDC
  against the cluster SA issuer, keys fetched through the issuer mirror in `templates/openfga-authn.yaml`
  (OpenFGA fetches discovery with no bearer and k3s answers that 401), the subject allow-list from
  `lance.openfgaSubjects` (a ConfigMap OpenFGA reads at start, so a new client needs an OpenFGA restart),
  playground off, CORS closed. A pod that names OpenFGA renders all three parts of `lance.fgaToken` (env,
  volume, mount) under the condition its URL has, and `lance.openfgaSubjects` must list its account under
  the same condition. A list-valued `OPENFGA_*` env is comma-joined (measured on v1.18.3). An operator tool
  mints a token with `kubectl create token <release>-sa-jobs --audience rask-openfga`. The gate is
  `tests/unit/test_openfga_admits_only_a_projected_service_account_token.py`; `openfga-authn.yaml` fails
  the render when OpenFGA's audience, issuer alias or subject source drifts from the clients'.
- **The gate is the render, judged by what each role grants**, so a new subchart that binds a
  secret-reading or token-reviewing role to `default` fails without anyone listing it:
  `tests/unit/test_a_first_party_pod_cannot_read_a_secret_through_the_kube_api.py`. A `User` subject
  named `system:serviceaccount:<ns>:<sa>` counts as that SA. The gate carries its own mutation check:
  a template binding a `secrets: get` Role to `default` must fail it.
