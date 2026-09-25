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

**`values.yaml` prose is free** — it is stored as a parsed map, comments dropped. **Template prose is
not.** A rationale comment belongs in `values.yaml` or a doc where it can; in a template it is paid for
on every revision forever.

**Measure before applying, the way Helm's Go code encodes it:**

```bash
bash scripts/helm.sh upgrade rask ./chart --reset-then-reuse-values --set image.tags.<stem>=<tag> \
  --dry-run=server -o json > /tmp/rel.json
```

then pack it as Go does: compact JSON with `<`, `>`, `&` escaped to `\u003c` `\u003e` `\u0026`,
UTF-8 kept, gzip **level 6**, base64. That lands within 0.2% of the real stored size. Python's
default `gzip.compress` is level 9 and **under-counts by ~0.9%** — enough to read "fits" for a
revision that the API server refuses.

`.helmignore` keeps non-runtime files out of `chart.files`. `alerting/rules_test.yml` (promtool's
fixture, read from the working tree, never from the package) was 40,912 packed bytes on its own.

**It has hit the ceiling three times**, each time answered by trimming rather than by removing what
fills it. v35 on 2026-08-15 (`HELM_DRIVER=sql`, then CNPG's CRDs moved out in `b56a49c9`); the SQL
store was dropped on 2026-09-08 when it split-brained against the Secret store (SQL rev 42 vs Secret
rev 108). v194 on 2026-09-21 (template YAML comments turned into `{{/* */}}`, `80783647`). v239 on
2026-09-24 (`.helmignore` and the `required` guards, `c352a232`/`4bff1036`). `docs/DECISIONS.md`'s
2026-08-15 entry attributes the size to `chart/charts/*.tgz`; by Helm's source those bytes are never
stored, so that entry's premise is false and its chosen answer no longer applies.

## 2. Operators do not belong in the app chart

Helm's own CRD guidance gives two methods: a non-templated `crds/` directory (installed once, never
upgraded or deleted), or the CRDs in a **separate chart installed on its own**. Operators are the
second case. Kueue's upstream docs only ever install it as its own release in `kueue-system`.

- **CNPG is the precedent** (`b56a49c9`, 2026-08-19): its 1.2 MB CRD file moved to
  `chart/crds-bootstrap/`, `.helmignore`d, applied out-of-band by `make k3s-crds`.
- **Kueue cannot follow it from inside the umbrella.** It templates its 11 CRDs under
  `templates/crd/` with no switch — 0.18.1 and 0.19.6 alike, and the upstream request to move them was
  closed as not-planned. The conversion webhook's service name and namespace are templated, so a
  vendored copy must be pre-rendered with release `rask` and namespace `default` baked in.
- **Kueue is a workload concern in a platform seam.** `values.yaml` sizes it as "how many projects
  transcribe at once" — one runner's GPU admission. No rask template sets `kueue.x-k8s.io/queue-name`,
  and rask's own queue has admitted zero workloads.
- `DECISIONS.md` (2026-08-15) already calls infra-separated-from-app "the architecturally correct
  answer and … the intended end state". It was deferred, not rejected.

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
`resource-policy: keep` already; Kueue's do not.

## 4. Upgrade flags — `--reset-then-reuse-values`, never `--reuse-values`

`--reuse-values` keeps the previous release's values and **ignores this chart's defaults**. A key
added after that release arrives nil, and `{{ .Values.x | quote }}` renders nil as `""`. On 2026-09-25
that put `MAINTENANCE_RECYCLE_AT_MEMORY_FRACTION=''` into the maintenance worker and pydantic refused
it at boot (CrashLoopBackOff, contained only because the rolling update kept the old pods serving).

- Use `--reset-then-reuse-values` (helm ≥ 3.14; this estate runs 3.20): chart defaults, then the
  release's values, then any `--set`.
- Guard every value a typed setting depends on with `required`, so absence fails the RENDER with a
  message rather than a pod: see `chart/templates/maintenance-worker.yaml`.
- `helm diff upgrade` shows a `''` before it ships.
- Never kill a running upgrade — it leaves the release in `pending-upgrade`, which refuses every later
  one until `helm rollback`.

## 5. The chart is the only path configuration reaches the estate

`kubectl set image` keeps pods on current code and applies **no configuration**. When helm cannot
write, config silently stops arriving while images keep moving — which is how LH-064 ran signing code
for a day with neither half of its credential pair: `LANCE_SERVICE_IDENTITY` and the
`service-token-service-catalog` key are both chart-supplied.

- Before believing a chart commit landed: `helm history rask` — if the top revision predates the
  commit, it did not.
- The release stores image tags. A chart apply after a `kubectl set image` roll **reverts the roll**
  unless the tag is pinned on the command: `--set image.tags.lance-rest-catalog=<tag>`.
- Every helm call goes through `scripts/helm.sh`.

## 6. Adopted — the generic baseline that fits

- **Security context**, per container and pod: `runAsNonRoot: true`, `runAsUser` explicit,
  `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]`,
  `seccompProfile.type: RuntimeDefault`. Uneven application of these is backlog row XC-061.
- **`serviceAccount.automount: false`** unless the pod genuinely calls the Kubernetes API.
- **`values.schema.json`** — validates the MERGED values at install and upgrade, so a missing or
  mistyped key fails before any pod starts. rask has none yet.
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
| `templates/secret.yaml` rendering a Secret from `.Values` | **No.** Credentials come from OpenBao via the Dapr secret store or ESO. |
| a literal `value:` in a Job's env for a derived credential | **No** — `minio-scoped-users.yaml` does this today and it is readable by anyone with `get jobs`. |
| Docker-based chart tooling | **Dagger.** `dagger call charts` runs the chart gate. |
