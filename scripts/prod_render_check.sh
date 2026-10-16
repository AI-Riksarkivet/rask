#!/usr/bin/env bash
# Render the PRODUCTION overlay (values-prod.yaml) and assert its HA + security switches are actually ON.
# Nothing else in CI renders the prod overlay — the e2e jobs deploy the DEFAULT (kind) values — so without
# this a regression that silently ships the dev posture in prod (NetworkPolicy off, OpenFGA single-
# replica) sails through green. No credential is passed: the chart takes none as a value ([[XC-004]]), and
# a prod render that needed one would fail here. Run: `make prod-render-check` (or in CI). Requires helm +
# the vendored chart/charts/*.tgz.
set -euo pipefail

CHART="${CHART:-chart}"
PROD_VALUES="${PROD_VALUES:-deploy/values-prod.yaml}"

# Every value the chart's `required` guards demand before it will render at all. values-prod sets
# auth.enabled AND frontend.oidc.enabled — a governed backend with a UI that can sign in — so
# templates/{auth-consistency,frontends}.yaml demand the three OIDC values on EVERY render here, and
# _helpers.tpl demands a registry-qualified image.repository.
# One array, used by all four helm calls below: adding a fifth without them would fail the whole script
# at that line with no message (set -e + command substitution), which is how this reached CI red.
# Dummies on purpose: they satisfy the fail-closed guards and thereby also prove the guards do not block
# a legitimate prod render. The External Secrets Operator is a prerequisite the render checks through
# `.Capabilities`, so a cluster-less render names its API.
#
# image.repository joined this array on 2026-08-22. It became `required` in 3c909e0a (2026-08-04,
# "registry required") and NOTHING here supplied it, so all four renders died on the guard and the whole
# script exited 1 — every run, for 923 commits. It was invisible because `make prod-render-check` is
# step 6 of .dagger/charts.go's chain and step 2 had been failing on the SAME guard since the same
# commit: the gate never reached the script that would have reported it. values-prod.yaml deliberately
# does NOT pin a registry (the deployer supplies theirs), so the check must. signing.provisioned is the operator's attestation that the event-signing
# keys exist in the platform's store (templates/signing-provisioned.yaml refuses it without that), and
# nats.auth.provisioned the same for the NATS users (templates/nats-provisioned.yaml). nats.url is the platform's NATS
# bus, a prerequisite values-prod leaves for the deployer to name.
# observability.export.endpoint is the platform's OTLP backend, which values-prod leaves for the deployer to name, and
# auth.fgaApiUrl and age.externalHost the platform's OpenFGA and Postgres ([[XC-132]]), openbao.externalAddr its secret
# store ([[XC-149]]), and route.* the platform's Gateway, its in-cluster address and the egress that reaches it (ADR 0165),
# and the egress that reaches the API server, the telemetry backend, the platform's OpenFGA and its secret store, which
# no URL can derive ([[XC-143]]).
COMMON=(--api-versions external-secrets.io/v1/ExternalSecret --api-versions dapr.io/v1alpha1
        --set auth.oidc.issuer=https://auth.example.com/dex
        --set frontend.oidc.publicOrigin=https://lance.example.com
        --set image.repository=ghcr.io/example/rask
        --set signing.provisioned=true
        --set nats.auth.provisioned=true
        --set-string ray.dashboardUrl=https://ray.example.com:8265
        --set-string nats.url=nats://nats.example.com:4222
        --set-string observability.export.endpoint=http://otlp.example.com:4318/v1/otlp
        --set-string auth.fgaApiUrl=https://fga.example.com
        --set-string age.externalHost=pg.example.com
        --set-string openbao.externalAddr=https://vault.example.com:8200
        --set-json 'openbao.egress=[{"to":[{"ipBlock":{"cidr":"198.51.100.20/32"}}],"ports":[{"protocol":"TCP","port":8200}]}]'
        --set-json 'route.parentRefs=[{"name":"edge","namespace":"edge-system"}]'
        --set-string route.edgeUrl=http://edge.edge-system.svc.cluster.local
        --set-json 'route.edgeEgress=[{"to":[{"namespaceSelector":{"matchLabels":{"kubernetes.io/metadata.name":"edge-system"}}}],"ports":[{"protocol":"TCP","port":8080}]}]'
        --set-json 'networkPolicy.apiServerEgress=[{"to":[{"ipBlock":{"cidr":"192.0.2.10/32"}}],"ports":[{"protocol":"TCP","port":6443}]}]'
        --set-json 'observability.export.egress=[{"to":[{"ipBlock":{"cidr":"203.0.113.10/32"}}],"ports":[{"protocol":"TCP","port":4318}]}]'
        --set-json 'auth.fgaEgress=[{"to":[{"ipBlock":{"cidr":"198.51.100.10/32"}}],"ports":[{"protocol":"TCP","port":443}]}]')
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

helm template rask "$CHART" -f "$PROD_VALUES" \
  --set image.catalog.tag=v0 --set frontend.image.tag=v0 \
  --set backups.volumeSnapshot.snapshotClassName=csi-snapclass \
  --set route.hostnames[0]=lance.example.com "${COMMON[@]}" > "$OUT"

fail() { echo "!! prod-render-check: $*" >&2; exit 1; }

# 1. Network isolation ON (the audit's headline gap): default-deny + DNS + the secret store's exclusive egress.
np=$(grep -c "kind: NetworkPolicy" "$OUT" || true)
[ "$np" -ge 9 ] || fail "prod must render the network-isolation layer (>=9 NetworkPolicies), got $np"
grep -q "default-deny" "$OUT" || fail "prod NetworkPolicy set missing default-deny"
grep -q "name: rask-allow-secret-store-egress" "$OUT" || fail "prod NetworkPolicy set missing the egress that admits only the secret store's readers"
# NATS isolation (goal cond 8): the general intra-namespace ingress allow must EXCLUDE the nats pods —
# without the exclusion the explicit nats rules are decorative (NetworkPolicies are additive). Then the
# two explicit rules must render: 4222 (clients, port-scoped from any in-namespace pod — the daprd
# sidecars live in nearly every app pod) and 8222 (monitor) from the web-lakehouse zone pods. The windowed
# greps pin the from-selector + port to THEIR rule, not a stray match elsewhere.
grep -qF "values: [openbao, age, minio, nats, authorization-controller]" "$OUT" \
  || fail "the general intra-namespace ingress allow must exclude the nats pods (with openbao/age/minio)"
grep -q "name: rask-nats-clients" "$OUT" || fail "prod NetworkPolicy set missing the nats-clients (4222) rule"
grep -A20 "name: rask-nats-clients" "$OUT" | grep -q "port: 4222" \
  || fail "the nats-clients rule must target the 4222 client port"
grep -q "name: rask-nats-monitor" "$OUT" || fail "prod NetworkPolicy set missing the nats-monitor (8222) rule"
grep -A20 "name: rask-nats-monitor" "$OUT" | grep -q "app.kubernetes.io/component: web-lakehouse" \
  || fail "the nats-monitor rule must admit only the web-lakehouse component pods"
grep -A20 "name: rask-nats-monitor" "$OUT" | grep -q "port: 8222" \
  || fail "the nats-monitor rule must target the 8222 monitor port"

# 2. OpenFGA and its Postgres are the platform's ([[XC-132]]): prod runs neither, and every client dials the endpoints
# the deployer names.
grep -qE "name: rask-(openfga|age)$" "$OUT" && fail "prod must not run an OpenFGA or a Postgres (the platform's)"
grep -qE "rask-openfga:8080|host=rask-age |@rask-age:" "$OUT" && fail "a prod client dials the in-chart OpenFGA or Postgres"
grep -q '"https://fga.example.com"' "$OUT" || fail "prod's FGA clients must dial auth.fgaApiUrl"
# The secret store is the platform's too ([[XC-149]]): prod renders no OpenBao, and its readers read openbao.externalAddr.
grep -qE "^  name: rask-(sa-)?openbao" "$OUT" && fail "prod must not render a secret store (the platform's)"
grep -q 'https://vault.example.com:8200' "$OUT" || fail "prod's secret readers must read openbao.externalAddr"

# 3. The Dapr control plane is the platform's (ADR 0153): prod renders none of it, only rask's own Dapr objects.
grep -qE "name: dapr-(sentry|operator|placement-server|scheduler-server|sidecar-injector)$" "$OUT" \
  && fail "prod must not render a Dapr control plane (it is a prerequisite)"
grep -q "^kind: Component$" "$OUT" || fail "prod must render rask's own Dapr Components"
# The NATS bus is the platform's too ([[XC-129]]): prod renders no server, only rask's stream Job against nats.url.
awk '/^kind: StatefulSet$/{f=1} f&&/^  name: /{print $2; f=0}' "$OUT" | grep -qx 'rask-nats' \
  && fail "prod must not render a NATS server (it is a prerequisite)"
grep -q 'S="nats://nats.example.com:4222"' "$OUT" || fail "prod's stream Job must provision rask's streams on the bus nats.url names"
# And the CloudNativePG operator ([[XC-130]]): prod runs no operator image and renders none of its webhooks.
grep -qE "image: \"?ghcr.io/cloudnative-pg/cloudnative-pg:" "$OUT" \
  && fail "prod must not run a CloudNativePG operator (it is a prerequisite)"
grep -q "name: cnpg-validating-webhook-configuration$" "$OUT" && fail "prod must not render CloudNativePG's webhooks"

# 4. PodDisruptionBudgets: the request-serving backends (catalog/lineage/gateway) + one per frontend zone (frontend.replicas=2 in prod; the zones are the Ingress-routed
# north-south edge — audit 2026-07-24: the raw `>=5` count was inflated by the 5 Dapr-HA subchart PDBs,
# so the missing zone PDBs sailed through). NAMED asserts on the PDB documents themselves, not a count.
pdb=$(grep -c "kind: PodDisruptionBudget" "$OUT" || true)
pdb_names=$(awk '/^kind: PodDisruptionBudget$/{f=1} f&&/^  name: /{print $2; f=0}' "$OUT")
# The zone half is DERIVED from the chart's own `frontend.apps`, not hand-listed. It WAS hand-listed —
# `web-home web-lakehouse web-media web-annotator` — and by 2026-08-22 that literal named a zone which no
# longer exists (`media`, retired with the R6/R20 media wave) while omitting FOUR that do (compute,
# studio, models, explorer). So the assertion could not pass, and the three zones it did cover were the
# only ones guarded. Nothing noticed for 923 commits because this script is step 6 of .dagger/charts.go's
# chain and step 2 had been failing since 2026-08-04 — the roster drifted under a gate that never ran.
# Deriving closes the class: a zone added to or removed from the chart moves this assertion with it.
# Input-vs-output, not circular: the expected set comes from values.yaml, the actual from the render.
zones=$(awk '/^  apps:/{f=1;next} f&&/^  [a-zA-Z]/{exit} f&&match($0,/name: [a-z0-9-]+/){print substr($0,RSTART+6,RLENGTH-6)}' "$CHART/values.yaml")
[ -n "$zones" ] || fail "could not derive frontend.apps from $CHART/values.yaml — the PDB zone assertion would be vacuous"
for p in catalog lineage $(sed 's/^/web-/' <<<"$zones"); do
  grep -qx "rask-$p" <<<"$pdb_names" \
    || fail "prod is missing the rask-$p PodDisruptionBudget (got: $(tr '\n' ' ' <<<"$pdb_names"))"
done

# 5. Anti-affinity: the replicas:2 services spread across nodes (else one node loss defeats their PDB).
spread=$(grep -c "topologySpreadConstraints:" "$OUT" || true)
[ "$spread" -ge 4 ] || fail "prod must spread the 4 multi-replica services across nodes (>=4), got $spread"

# 6. Resource tiers: the memory-heavy workload in the release (the catalog's Arrow buffer) gets a 1Gi limit
# above the shared 512Mi default; the object store is the platform's and outside the release. The default
# render tiers NOTHING to 1Gi (verified), so a non-zero count here proves the per-workload tier applies on
# the prod overlay only.
tiers=$(grep -c "memory: 1Gi" "$OUT" || true)
[ "$tiers" -ge 1 ] || fail "prod must tier the memory-heavy workload (the catalog) to 1Gi, got $tiers"

# 7. The Collector exports to the endpoint the deployer names, and rask ships no store or alert engine of its own
# (ADR 0153): the alert rules are deploy/alerting/rules.yml, for the platform's evaluator.
grep -q 'endpoint: "http://otlp.example.com:4318/v1/otlp"' "$OUT" || fail "prod's Collector must export to observability.export.endpoint"
# Default-deny egress would drop an off-namespace export silently: the Collector's egress must admit the backend the
# deployer states (observability.export.egress).
grep -A20 "name: rask-allow-telemetry-export-egress$" "$OUT" | grep -q "cidr: 203.0.113.10/32" \
  || fail "prod's NetworkPolicy must let the Collector reach the export backend observability.export.egress names"
grep -qE "name: rask-(vmalert|alertmanager|greptimedb-standalone|perses)$" "$OUT" \
  && fail "prod must not render an observability store, dashboard or alert engine (they are the platform's)"

# 8. NO bespoke scraper is shipped — the OTel Collector's prometheus receiver owns the Dapr scrape.
grep -q "name: rask-vmagent$" "$OUT" && fail "vmagent must NOT be shipped (the OTel Collector owns the Dapr scrape)"

# 8b. THE EDGE'S FORMERLY-UNAUTHENTICATED DOORS STAY SHUT IN PROD.
#
# The telemetry suite (/perses/, /greptime/) is cluster-internal (port-forward only): no rule of the cluster route
# publishes it, and the routes publish /api/* prefixes, the zones and /dex alone (ADR 0165). Assert the doors stay
# CLOSED on the rendered OUTPUT: a route path or an nginx location block reopens a door invisibly.
grep -qE "location /(produce|perses|greptime)" "$OUT" \
  && fail "an nginx location block resurfaced — the nginx gateway is retired (docs/adr/0117-nginx-is-retired-and-the-fastapi-gateway-is-the-in-cluster.md)"
grep -qE 'value: "?/(perses|greptime)' "$OUT" \
  && fail "prod must NOT route /perses or /greptime at the public edge: the telemetry suite is cluster-internal"

# 9. Catalog memory coherence (P1): the load-shed write cap must be sized to the catalog memory tier —
# cap × maxBodyBytes of buffered Arrow-IPC bodies + 512Mi baseline headroom (process + pyarrow decode)
# must fit the memory limit. The code default (16 × 256MiB = 4Gi) OOM-kills the 1Gi tier before shedding
# helps, so prod must ship an explicit, coherent cap (values-prod.yaml states the formula).
to_bytes() { # Gi/Mi → bytes (the only units the chart's memory quantities use)
  case "$1" in
    *Gi) echo $((${1%Gi} * 1024 * 1024 * 1024)) ;;
    *Mi) echo $((${1%Mi} * 1024 * 1024)) ;;
    *) fail "unparseable memory quantity: $1" ;;
  esac
}
# `|| true`: a no-match grep exits 1 and set -e would kill the script BEFORE the actionable fail
# message below — let the emptiness checks do the failing instead.
cap=$(grep -o 'name: LANCE_MAX_CONCURRENT_WRITES, value: "[0-9]*"' "$OUT" | head -1 | grep -o '[0-9]\+' || true)
body=$(grep -o 'name: LANCE_MAX_BODY_BYTES, value: "[0-9]*"' "$OUT" | head -1 | grep -o '[0-9]\+' || true)
[ -n "$cap" ] || fail "prod catalog must set LANCE_MAX_CONCURRENT_WRITES explicitly (else the code default 16 admits 4Gi of buffers)"
[ -n "$body" ] || fail "could not extract LANCE_MAX_BODY_BYTES from the render"
[ "$cap" -ge 1 ] || fail "prod LANCE_MAX_CONCURRENT_WRITES must be >=1 (0 disables shedding → unbounded buffer memory)"
# The catalog Deployment is the only manifest carrying LANCE_MAX_BODY_BYTES; its limits block follows in
# the same document, so the first memory after the first limits is the catalog's memory limit.
cat_mem=$(awk '/LANCE_MAX_BODY_BYTES/{n=1} n&&/limits:/{l=1} l&&/memory:/{print $2; exit}' "$OUT")
[ -n "$cat_mem" ] || fail "could not extract the catalog memory limit from the render"
headroom=$((512 * 1024 * 1024))
peak=$((cap * body + headroom))
[ "$peak" -le "$(to_bytes "$cat_mem")" ] \
  || fail "catalog sizing incoherent: $cap × $body buffered bodies + 512Mi headroom = $peak bytes > the $cat_mem limit"

# 10. Externalizing the object store leaves no in-cluster store DNS anywhere in the render (app env, jobs, configs).
EXT_S3=https://s3.ext.example.com
atomic=$(helm template rask "$CHART" -f "$PROD_VALUES" \
  --set image.catalog.tag=v0 --set frontend.image.tag=v0 \
  --set backups.volumeSnapshot.snapshotClassName=csi-snapclass --set route.hostnames[0]=lance.example.com \
  --set minio.enabled=false --set minio.externalEndpoint="$EXT_S3" "${COMMON[@]}")
grep -q "rask-minio:9000" <<<"$atomic" && fail "externalizing the object store still leaks in-cluster store DNS"
grep -q "$EXT_S3" <<<"$atomic" || fail "the externalized object store endpoint reaches nothing in the render"

# 11. The prod render takes every credential from the store through ESO: the SecretStore and the
# ExternalSecrets render, no Secret the chart renders carries a credential key, and nothing is generated
# (the Password generator is the dev OpenBao's alone).
grep -q "kind: SecretStore" "$OUT" || fail "prod must render the SecretStore ESO syncs from"
grep -q "name: rask-infra-credentials" "$OUT" || fail "prod must render the infra-credentials ExternalSecret"
grep -q "kind: Password" "$OUT" && fail "prod must not generate credentials (the Password generator is the dev OpenBao's)"

# 12. Every Job/CronJob POD TEMPLATE carries a component label (the P4 landmine, docs/adr/0124-chart-unification-one-control-plane-each-one-object-store.md, audit 2026-07-24: the
# unlabeled minio-mkbucket hook matched no minio-ingress client and every prod install wedged before a
# bucket existed). Pod-template label indents only — 8 spaces (Job) / 12 (CronJob) — so the heredoc'd
# VolumeSnapshot labels inside a command string can't satisfy the check. Then pin the load-bearing pair:
# the mkbucket pod label AND its admission in the minio ingress client list.
# post-delete hooks are exempt: the landmine class is INSTALL-time wedging (an unlabeled hook pod
# that no netpol ingress rule admits), and a post-delete hook cannot wedge an install. Today's only
# such job is the vendored nfd 0.17.3 prune (node-API-only, needs no ingress admission), whose
# labels are hardcoded upstream — revisit if nfd ever grows a prune podLabels hook.
jobs_missing=$(awk '
  /^---/     { if (k && !c && !pd && n != "") print n; k=0; c=0; pd=0; n="" }
  /^kind: Job$/ || /^kind: CronJob$/ { k=1 }
  k && n == "" && /^  name: /        { n=$2 }
  k && /"helm\.sh\/hook": post-delete/ { pd=1 }
  k && (/^        app\.kubernetes\.io\/component:/ || /^            app\.kubernetes\.io\/component:/) { c=1 }
  END        { if (k && !c && !pd && n != "") print n }
' "$OUT")
[ -z "$jobs_missing" ] \
  || fail "Job/CronJob pod templates missing the component label (invisible to component-scoped NetworkPolicies): $(tr '\n' ' ' <<<"$jobs_missing")"
grep -q "app.kubernetes.io/component: minio-mkbucket" "$OUT" || fail "the mkbucket hook pod must carry its component label"
grep -q -- "- minio-mkbucket" "$OUT" || fail "the minio ingress client list must admit the mkbucket hook component"

# POD IDENTITY. Asserted on the RENDER: a template that stops naming its SA is invisible to any values
# grep. Every workload's pod names a ServiceAccount, and every name it gives is one this render creates, so no
# pod runs as the namespace's shared `default` and none names an account that does not exist. Measured on this
# overlay 2026-10-15, with the prerequisites outside the release (ADR 0153): 21 workloads naming 12 of the 17
# ServiceAccounts rendered; the other five belong to components the overlay leaves off. The per-pod verdict
# (no idle token, no secret grant to their SAs) is
# tests/unit/test_a_first_party_pod_cannot_read_a_secret_through_the_kube_api.py.
workloads=$(grep -cE '^kind: (Deployment|StatefulSet|DaemonSet|Job|CronJob)$' "$OUT" || true)
named=$(grep -E '^ +serviceAccountName: ' "$OUT" | awk '{print $2}' | tr -d '"' | sort)
named_count=$(grep -c . <<<"$named" || true)
[ "$named_count" -ge "$workloads" ] \
  || fail "prod must give every workload a dedicated ServiceAccount (templates/security-sa.yaml): $workloads workloads, $named_count name one"
rendered=$(awk '/^kind: ServiceAccount$/{sa=1;next} sa && /^  name: /{print $2; sa=0}' "$OUT" | tr -d '"' | sort -u)
unrendered=$(comm -23 <(sort -u <<<"$named") <(echo "$rendered"))
[ -z "$unrendered" ] || fail "workloads name ServiceAccounts this render does not create: $(tr '\n' ' ' <<<"$unrendered")"
sa=$(grep -c "^kind: ServiceAccount" "$OUT" || true)
automount=$(grep -c "automountServiceAccountToken: false" "$OUT" || true)
[ "$automount" -ge 13 ] \
  || fail "prod must stop mounting the SA token into app pods, rendered $automount refusals (>=13 expected; 1 is the baseline)"
grep -q 'dapr.io/sidecar-seccomp-profile-type: RuntimeDefault' "$OUT" \
  || fail "prod must restrict the daprd sidecar (dapr.sidecarRestricted) — a pod is only as restricted as its loosest container"

# TELEMETRY PROVENANCE. `observability.environment` is the `deployment.environment.name` resource
# attribute on every trace and metric; values.yaml defaults it to `rask` and says "override per deploy",
# which this overlay did not do until 2026-09-16 — so prod telemetry was indistinguishable from dev in
# the same GreptimeDB, and an alert firing on "the estate" could not say which one. Asserted on the
# RENDER, because the attribute is what ships, not the value.
grep -q "deployment.environment.name=prod" "$OUT" \
  || fail "prod telemetry must carry deployment.environment.name=prod (observability.environment) — it renders $(grep -oE 'deployment\.environment\.name=[^,\"]*' "$OUT" | sort -u | head -1)"

echo "✓ prod-render-check: NetworkPolicy=$np, OpenFGA+Postgres+secret store external, Dapr control plane external, PDBs=$pdb (backends+zones named), spread=$spread, tiers=$tiers, export named, no-bespoke-scraper, write-cap=$cap fits $cat_mem, minio-externalize atomic, ESO path renders, hook pods labeled, ServiceAccounts=$sa, token-automount-refusals=$automount"
