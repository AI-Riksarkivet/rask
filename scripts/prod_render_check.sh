#!/usr/bin/env bash
# Render the PRODUCTION overlay (values-prod.yaml) and assert its HA + security switches are actually ON.
# Nothing else in CI renders the prod overlay — the e2e jobs deploy the DEFAULT (kind) values — so without
# this a regression that silently ships the dev posture in prod (NetworkPolicy off, OpenFGA/Dapr single-
# replica) sails through green. Dummy values satisfy the render-FAILS-CLOSED prod-secret guards (appToken /
# edgeAuth htpasswd / age pw / rustfs key); supplying them ALSO proves the guards don't block a legitimate
# prod render. Run: `make prod-render-check` (or in CI). Requires helm + the vendored chart/charts/*.tgz.
set -euo pipefail

CHART="${CHART:-chart}"
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

helm template lance-ns "$CHART" -f "$CHART/values-prod.yaml" \
  --set image.catalog.tag=v0 --set frontend.image.tag=v0 \
  --set dapr.appToken=ci-dummy-token-0000000000 \
  --set 'observability.edgeAuth.htpasswd=observer:$apr1$ci000000$0000000000000000000000' \
  --set age.password=ci-dummy-pw --set rustfs.secretKey=ci-dummy-key \
  --set backups.volumeSnapshot.snapshotClassName=csi-snapclass \
  --set ingress.host=lance.example.com > "$OUT"

fail() { echo "!! prod-render-check: $*" >&2; exit 1; }

# 1. Network isolation ON (the audit's headline gap): default-deny + DNS + the exclusive openbao lock.
np=$(grep -c "kind: NetworkPolicy" "$OUT" || true)
[ "$np" -ge 9 ] || fail "prod must render the network-isolation layer (>=9 NetworkPolicies), got $np"
grep -q "default-deny" "$OUT" || fail "prod NetworkPolicy set missing default-deny"
grep -q -- "-openbao" "$OUT" || fail "prod NetworkPolicy set missing the openbao ingress lock"
# NATS isolation (goal cond 8): the general intra-namespace ingress allow must EXCLUDE the nats pods —
# without the exclusion the explicit nats rules are decorative (NetworkPolicies are additive). Then the
# two explicit rules must render: 4222 (clients, port-scoped from any in-namespace pod — the daprd
# sidecars live in nearly every app pod) and 8222 (monitor) from the web-lakehouse zone pods. The windowed
# greps pin the from-selector + port to THEIR rule, not a stray match elsewhere.
grep -qF "values: [openbao, age, rustfs, nats]" "$OUT" \
  || fail "the general intra-namespace ingress allow must exclude the nats pods (with openbao/age/rustfs)"
grep -q "name: lance-ns-nats-clients" "$OUT" || fail "prod NetworkPolicy set missing the nats-clients (4222) rule"
grep -A20 "name: lance-ns-nats-clients" "$OUT" | grep -q "port: 4222" \
  || fail "the nats-clients rule must target the 4222 client port"
grep -q "name: lance-ns-nats-monitor" "$OUT" || fail "prod NetworkPolicy set missing the nats-monitor (8222) rule"
grep -A20 "name: lance-ns-nats-monitor" "$OUT" | grep -q "app.kubernetes.io/component: web-lakehouse" \
  || fail "the nats-monitor rule must admit only the web-lakehouse component pods"
grep -A20 "name: lance-ns-nats-monitor" "$OUT" | grep -q "port: 8222" \
  || fail "the nats-monitor rule must target the 8222 monitor port"

# 2. OpenFGA HA — the authz chokepoint every governed call fails-closed through: 3 replicas. (The subchart
# Deployment's spec.replicas renders ~10 lines below its metadata.name; only the Deployment carries a
# replicas: field, so the windowed grep can't false-match the Service/SA/migrate-Job of the same name.)
grep -A15 "name: lance-ns-openfga$" "$OUT" | grep -q "replicas: 3" \
  || fail "prod OpenFGA must run 3 replicas (authz SPOF)"

# 3. Dapr control-plane HA — Sentry is the mTLS CA in the sidecar cert-renewal path.
awk '/name: dapr-sentry$/{n=1} n&&/replicas:/{print; exit}' "$OUT" | grep -q "replicas: 3" \
  || fail "prod Dapr control-plane must be HA (dapr-sentry replicas: 3)"

# 4. PodDisruptionBudgets: the request-serving backends (catalog/lineage/gateway) + OpenFGA (the authz
# chokepoint) + one per frontend zone (frontend.replicas=2 in prod; the zones are the Ingress-routed
# north-south edge — audit 2026-07-24: the raw `>=5` count was inflated by the 5 Dapr-HA subchart PDBs,
# so the missing zone PDBs sailed through). NAMED asserts on the PDB documents themselves, not a count.
pdb=$(grep -c "kind: PodDisruptionBudget" "$OUT" || true)
pdb_names=$(awk '/^kind: PodDisruptionBudget$/{f=1} f&&/^  name: /{print $2; f=0}' "$OUT")
for p in catalog lineage gateway openfga web-home web-lakehouse web-media web-annotator; do
  grep -qx "lance-ns-$p" <<<"$pdb_names" \
    || fail "prod is missing the lance-ns-$p PodDisruptionBudget (got: $(tr '\n' ' ' <<<"$pdb_names"))"
done

# 5. Anti-affinity: the replicas:2 services spread across nodes (else one node loss defeats their PDB).
spread=$(grep -c "topologySpreadConstraints:" "$OUT" || true)
[ "$spread" -ge 4 ] || fail "prod must spread the 4 multi-replica services across nodes (>=4), got $spread"

# 6. Resource tiers: the memory-heavy workloads (catalog Arrow buffer, age dual-store, rustfs data plane)
# get a 1Gi limit above the shared 512Mi default. The default render tiers NOTHING to 1Gi (verified), so a
# non-zero count here proves the per-workload tiers apply on the prod overlay only.
tiers=$(grep -c "memory: 1Gi" "$OUT" || true)
[ "$tiers" -ge 3 ] || fail "prod must tier the memory-heavy workloads (catalog/age/rustfs) to 1Gi, got $tiers"

# 7. Alerting engine (P3b): vmalert + Alertmanager deployed, vmalert wired to GreptimeDB's PromQL endpoint,
# and the PROVEN rules actually mounted (a known alertname must appear — proves .Files.Get loaded rules.yml,
# not an empty ConfigMap). Off on the default render.
grep -q "name: lance-ns-vmalert$" "$OUT" || fail "prod must deploy vmalert (the alert evaluator)"
grep -q "name: lance-ns-alertmanager$" "$OUT" || fail "prod must deploy Alertmanager"
grep -q "datasource.url=http://lance-ns-greptimedb-standalone:.*prometheus" "$OUT" \
  || fail "vmalert must query GreptimeDB's PromQL endpoint"
grep -q "LineageOutboxNotDraining" "$OUT" || fail "the proven alert rules must be mounted into vmalert"

# 8. The DaprConsumerWedge rule is mounted (SLO-as-code, ready for when the OTel Collector scrapes the Dapr
# sidecars), and NO bespoke scraper is shipped — the redundant vmagent was removed; the OTel Collector's
# prometheus receiver owns that scrape at operator-adoption time.
grep -q "DaprConsumerWedge" "$OUT" || fail "the consumer-wedge rule must be mounted (SLO-as-code)"
grep -q "name: lance-ns-vmagent$" "$OUT" && fail "vmagent must NOT be shipped (the OTel Collector owns the Dapr scrape)"

# 8b. THE GATEWAY'S TWO UNAUTHENTICATED DOORS ARE SHUT IN PROD.
#
# The gateway is an nginx reverse proxy: no OIDC, no FGA. It forwards to the services, which enforce on the
# bearer — so /catalog/ and /lineage/ answer 401 to an anonymous caller, verified live. Two locations are
# NOT protected that way, and both were correct in values-prod.yaml while NOTHING here asserted them:
#
#   - POST /produce fires the medallion cascade and carries no auth of its own. Measured anonymously
#     through the gateway on the dev render: 202. That is deliberate for the demo
#     (`medallion.producer.expose: true`) and values-prod sets it false — a caller who could reach it
#     could trigger cascades and forge medallion provenance.
#   - /perses/ and /greptime/ are a dashboard suite and a raw telemetry store. Measured anonymously on the
#     dev render: 200 and 200. values-prod turns on `observability.edgeAuth` to put basic-auth on both.
#
# Asserting the rendered OUTPUT rather than trusting the values file: a values edit, a template refactor, or
# a `--set` in a deploy script can reopen either door, and neither failure is visible — an exposed /produce
# looks like a working demo, and an open /greptime looks like a working dashboard.
grep -q "location /produce" "$OUT" \
  && fail "prod must NOT expose the unauthenticated /produce door (medallion.producer.expose)"
grep -q "auth_basic_user_file /etc/nginx/edge-auth/htpasswd" "$OUT" \
  || fail "prod must put basic-auth on the gateway's /perses/ + /greptime/ (observability.edgeAuth.enabled)"
# One auth_basic per protected location, so a half-applied guard (one of the two) fails rather than passing.
[ "$(grep -c 'auth_basic_user_file /etc/nginx/edge-auth/htpasswd' "$OUT")" = "2" ] \
  || fail "both /perses/ AND /greptime/ must carry basic-auth in prod (found $(grep -c 'auth_basic_user_file' "$OUT"))"

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

# 10. RustFS externalize is ATOMIC (operator-handoff audit): the greptimedb-standalone object-store endpoint
# is a static subchart value that can't follow the rustfs.externalEndpoint helper, so externalizing RustFS
# without ALSO repointing GreptimeDB leaves its object backend at the deleted in-cluster service. The
# values-prod EXTERNALIZE block documents both as a pair; prove that when BOTH are set no in-cluster rustfs
# DNS survives anywhere (app env OR the greptime config), and that setting ONLY the rustfs half leaks.
EXT_S3=https://s3.ext.example.com
atomic=$(helm template lance-ns "$CHART" -f "$CHART/values-prod.yaml" \
  --set image.catalog.tag=v0 --set frontend.image.tag=v0 --set dapr.appToken=ci-dummy-token-0000000000 \
  --set 'observability.edgeAuth.htpasswd=observer:$apr1$ci000000$0000000000000000000000' \
  --set age.password=ci-dummy-pw --set rustfs.secretKey=ci-dummy-key \
  --set backups.volumeSnapshot.snapshotClassName=csi-snapclass --set ingress.host=lance.example.com \
  --set rustfs.enabled=false --set rustfs.externalEndpoint="$EXT_S3" \
  --set greptimedb-standalone.objectStorage.s3.endpoint="$EXT_S3" 2>/dev/null)
grep -q "lance-ns-rustfs:9000" <<<"$atomic" \
  && fail "externalizing RustFS + the greptime endpoint companion still leaks in-cluster rustfs DNS"
# Negative: rustfs externalized but the greptime companion OMITTED must still show the leak (proves the pairing
# is load-bearing, i.e. the guard above isn't vacuous).
leak=$(helm template lance-ns "$CHART" -f "$CHART/values-prod.yaml" \
  --set image.catalog.tag=v0 --set frontend.image.tag=v0 --set dapr.appToken=ci-dummy-token-0000000000 \
  --set 'observability.edgeAuth.htpasswd=observer:$apr1$ci000000$0000000000000000000000' \
  --set age.password=ci-dummy-pw --set rustfs.secretKey=ci-dummy-key \
  --set backups.volumeSnapshot.snapshotClassName=csi-snapclass --set ingress.host=lance.example.com \
  --set rustfs.enabled=false --set rustfs.externalEndpoint="$EXT_S3" 2>/dev/null)
grep -q "lance-ns-rustfs:9000" <<<"$leak" \
  || fail "the rustfs-externalize coherence guard is vacuous (expected the greptime leak without the companion override)"

# 11. External Secrets Operator path renders (operator-handoff audit): externalSecrets.enabled=true must
# render the SecretStore + ExternalSecret CRs, SKIP the static infra-credentials + observability-s3 Secrets,
# and SATISFY the fail-closed prod-secret guard WITHOUT age.password/rustfs.secretKey (ESO supplies them).
eso=$(helm template lance-ns "$CHART" -f "$CHART/values-prod.yaml" \
  --set image.catalog.tag=v0 --set frontend.image.tag=v0 --set dapr.appToken=ci-dummy-token-0000000000 \
  --set 'observability.edgeAuth.htpasswd=observer:$apr1$ci000000$0000000000000000000000' \
  --set backups.volumeSnapshot.snapshotClassName=csi-snapclass --set ingress.host=lance.example.com \
  --set externalSecrets.enabled=true 2>/dev/null) \
  || fail "prod render with externalSecrets.enabled=true FAILED (age.password/rustfs.secretKey should not be required)"
grep -q "kind: SecretStore" <<<"$eso" || fail "ESO path must render a SecretStore"
grep -q "kind: ExternalSecret" <<<"$eso" || fail "ESO path must render the ExternalSecret CRs"
grep -A2 "name: lance-ns-infra-credentials" <<<"$eso" | grep -q "stringData:" \
  && fail "ESO path must SKIP the static infra-credentials Secret (external-secrets owns it)"

# 12. Every Job/CronJob POD TEMPLATE carries a component label (the P4 landmine, audit 2026-07-24: the
# unlabeled rustfs-mkbucket hook matched no rustfs-ingress client and every prod install wedged before a
# bucket existed). Pod-template label indents only — 8 spaces (Job) / 12 (CronJob) — so the heredoc'd
# VolumeSnapshot labels inside a command string can't satisfy the check. Then pin the load-bearing pair:
# the mkbucket pod label AND its admission in the rustfs ingress client list.
jobs_missing=$(awk '
  /^---/     { if (k && !c && n != "") print n; k=0; c=0; n="" }
  /^kind: Job$/ || /^kind: CronJob$/ { k=1 }
  k && n == "" && /^  name: /        { n=$2 }
  k && (/^        app\.kubernetes\.io\/component:/ || /^            app\.kubernetes\.io\/component:/) { c=1 }
  END        { if (k && !c && n != "") print n }
' "$OUT")
[ -z "$jobs_missing" ] \
  || fail "Job/CronJob pod templates missing the component label (invisible to component-scoped NetworkPolicies): $(tr '\n' ' ' <<<"$jobs_missing")"
grep -q "app.kubernetes.io/component: rustfs-mkbucket" "$OUT" || fail "the mkbucket hook pod must carry its component label"
grep -q -- "- rustfs-mkbucket" "$OUT" || fail "the rustfs ingress client list must admit the mkbucket hook component"

echo "✓ prod-render-check: NetworkPolicy=$np, OpenFGA=3, Dapr-HA on, PDBs=$pdb (backends+OpenFGA+zones named), spread=$spread, tiers=$tiers, alerting on, no-bespoke-scraper, write-cap=$cap fits $cat_mem, rustfs-externalize atomic, ESO path renders, hook pods labeled"
