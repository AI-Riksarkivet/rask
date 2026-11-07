#!/usr/bin/env bash
# Install rask the way a GitOps platform does, onto the cluster KUBECONFIG names, which must be empty ([[XC-164]]): the
# development fixtures as scripts/install_from_empty.sh installs them, this tree's chart packaged and served from inside
# the cluster as a classic Helm repository, Argo CD from its pinned upstream release, and an Argo CD Application
# installing rask with the governed stack's values (scripts/stack_values.sh, the console on) under automated sync. Phase 1
# passes when Argo CD reports the release Synced and Healthy and every workload is Ready. Phase 2 changes one input (CHANGE
# below) on the Application and, relying on automated sync alone, checks item by item that the cluster converged on it
# (CHECKS below).
#
# The images the render names (`<component>:dev`, image.localImages) must already be pullable by the cluster:
# `dagger call gitops-from-empty` builds them into a registry its k3s mirrors.
#
# Every wait is bounded (SYNC_TIMEOUT for Argo CD, TIMEOUT for the workloads, CHECK_WINDOW for each check). On a failure
# it prints install_from_empty.sh's diagnostics and Argo CD's own view of the release.
set -euo pipefail
cd "$(dirname "$0")/.."

# Argo CD, pinned by exact release. Its core install is the controllers without the API server, its UI or Dex: the
# Application is the whole interface this lane drives.
ARGOCD_VERSION=v3.5.7
ARGOCD_MANIFEST="https://raw.githubusercontent.com/argoproj/argo-cd/$ARGOCD_VERSION/manifests/core-install.yaml"

RELEASE=rask
TIMEOUT="${TIMEOUT:-900s}"
SYNC_TIMEOUT="${SYNC_TIMEOUT:-1200}"
CHECK_WINDOW="${CHECK_WINDOW:-180}"
REPO_NAMESPACE=chart-repo
REPO_URL="http://rask-charts.$REPO_NAMESPACE.svc.cluster.local"

# ── Phase 2's one changed input: merged into the Application's values (a JSON merge patch). ─────────────────────────
CHANGE='
nats: { streamMaxAgeSeconds: 259200 }
catalog: { commitReconcile: { schedule: "@every 660s" } }
maintenance: { vendWriteCredentials: false }
'
# ── What CHANGE must converge to, one function each (defined under "Checks" below). ─────────────────────────────────
CHECKS=(check_lineage_max_age check_catalog_restarted check_maintenance_tuple_revoked check_nats_stream_job_reran)

# shellcheck source=scripts/stack_values.sh
. scripts/stack_values.sh
# shellcheck source=scripts/install_common.sh
. scripts/install_common.sh

JOB_LOGS="$(mktemp -d)"
RECORDER=""

argocd_view() {
  echo "==> Argo CD's view of the release"
  kubectl -n argocd get application "$RELEASE" -o json 2>/dev/null | jq -r '
    "sync=\(.status.sync.status // "-") health=\(.status.health.status // "-") revision=\(.status.sync.revision // "-")",
    "conditions:", ((.status.conditions // [])[] | "  \(.type): \(.message)"),
    "operationState: phase=\(.status.operationState.phase // "-") started=\(.status.operationState.startedAt // "-") finished=\(.status.operationState.finishedAt // "-")",
    "  message: \(.status.operationState.message // "-")",
    "  resource results:",
    ((.status.operationState.syncResult.resources // [])[]
      | "    \(.kind)/\(.name) syncPhase=\(.syncPhase // "-") hookType=\(.hookType // "-") hookPhase=\(.hookPhase // "-") status=\(.status // "-") message=\(.message // "-")"),
    "status.resources (not Synced or not Healthy):",
    ((.status.resources // [])[] | select(.status != "Synced" or ((.health.status // "Healthy") != "Healthy"))
      | "    \(.kind)/\(.name) status=\(.status // "-") health=\(.health.status // "-") \(.health.message // "") hook=\(.hook // false) prune=\(.requiresPruning // false)"),
    "status.resources: \((.status.resources // []) | length) in all"' || echo "(no Application)"
  # A hook Job Argo CD waits on can hold no pod at all (a ServiceAccount not yet created): its events say why.
  for j in $(kubectl get jobs -o json 2>/dev/null | jq -r '.items[] | select((.status.succeeded // 0) < 1) | .metadata.name'); do
    echo "--- UNFINISHED JOB: $j"
    kubectl describe job "$j" 2>/dev/null | sed -n '/Events:/,$p' | head -15 || true
  done
  echo "--- ServiceAccounts in default"
  kubectl get serviceaccounts -o name 2>/dev/null || true
  echo "--- argocd-application-controller (tail)"
  kubectl -n argocd logs statefulset/argocd-application-controller --tail=60 2>/dev/null || true
  echo "--- argocd-repo-server (tail)"
  kubectl -n argocd logs deploy/argocd-repo-server --tail=30 2>/dev/null || true
  kubectl -n "$REPO_NAMESPACE" get pods -o wide 2>/dev/null || true
}

diagnose() {
  local rc=$?
  [ -n "$RECORDER" ] && kill "$RECORDER" 2>/dev/null
  [ "$rc" -eq 0 ] && return 0
  echo "==> gitops-from-empty FAILED (exit $rc)"
  print_cluster_state "$JOB_LOGS"
  argocd_view
  return "$rc"
}
trap diagnose EXIT

# values_json [<helm value args>...]: the values those arguments set, as one JSON object, read by Helm's own parser (a
# throwaway chart whose only template prints its values).
values_json() {
  local d
  d="$(mktemp -d)"
  mkdir "$d/templates"
  printf 'apiVersion: v2\nname: values\nversion: 0.0.0\n' >"$d/Chart.yaml"
  printf '{{ toJson .Values }}\n' >"$d/templates/values.yaml"
  helm template values "$d" "$@" | grep -v -e '^---$' -e '^# Source:'
  rm -rf "$d"
}

# wait_for <seconds> <what> <probe>: runs <probe> every 10 s, printing its status line whenever it changes, until it
# returns 0; fails after <seconds>, or at once when the probe returns 2 (a terminal state).
wait_for() {
  local limit="$1" what="$2" probe="$3" deadline last="" out rc
  deadline=$(($(date +%s) + limit))
  echo "==> waiting up to ${limit}s for $what"
  while :; do
    rc=0
    out="$("$probe" 2>&1)" || rc=$?
    if [ "$out" != "$last" ]; then echo "   [$(date -u +%H:%M:%S)] $out"; last="$out"; fi
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -eq 2 ] && { echo "!! $what: terminal state"; return 1; }
    [ "$(date +%s)" -ge "$deadline" ] && { echo "!! $what: not reached in ${limit}s"; return 1; }
    sleep 10
  done
}

# Argo CD's verdict on the release, for a sync operation started at or after SINCE (epoch seconds): 0 when Synced, Healthy
# and Succeeded, 2 when the operation Failed or Errored, 1 otherwise.
SINCE=0
argocd_reconciled() {
  local app
  app="$(kubectl -n argocd get application "$RELEASE" -o json 2>/dev/null)" || { echo "Application unreadable"; return 1; }
  jq -r '(.status.operationState // {}) as $op
    | "sync=\(.status.sync.status // "-") health=\(.status.health.status // "-") operation=\($op.phase // "-") started=\($op.startedAt // "-") message=\(($op.message // "-")[0:240])"' <<<"$app"
  jq -e --argjson since "$SINCE" '(.status.operationState // {}) as $op
    | .status.sync.status == "Synced" and .status.health.status == "Healthy" and $op.phase == "Succeeded"
      and (($op.startedAt // "1970-01-01T00:00:00Z") | fromdateiso8601) >= $since' <<<"$app" >/dev/null && return 0
  jq -e --argjson since "$SINCE" '(.status.operationState // {}) as $op
    | ($op.phase == "Failed" or $op.phase == "Error") and (($op.startedAt // "1970-01-01T00:00:00Z") | fromdateiso8601) >= $since' <<<"$app" >/dev/null && return 2
  return 1
}

workloads_ready() {
  local s
  kubectl wait --for=condition=Available deploy --all --timeout="$TIMEOUT" || return 1
  for s in $(kubectl get statefulsets -o name); do kubectl rollout status "$s" --timeout="$TIMEOUT" || return 1; done
}

# ── Checks: each prints its evidence and returns 0 when the item converged. ─────────────────────────────────────────

# (a) The LINEAGE stream's max_age is CHANGE's 259200 s (72 h, in nanoseconds on the stream), read through the nats fixture's nats-box.
check_lineage_max_age() {
  local info age
  info="$(kubectl exec deploy/rask-nats-box -c nats-box -- nats --server nats://rask-nats:4222 stream info LINEAGE --json 2>&1)" || true
  age="$(jq -r '.config.max_age // "absent"' <<<"$info" 2>/dev/null || echo "unreadable: ${info:0:200}")"
  echo "LINEAGE config.max_age=$age (want 259200000000000, 72h)"
  [ "$age" = 259200000000000 ]
}

# (b) The catalog loads the commit-reconcile cron Component at start (HotReload is off), so every catalog pod must be one
# created after the change; terminating pods are listed and not counted.
check_catalog_restarted() {
  local pods
  pods="$(kubectl get pods -l app.kubernetes.io/component=catalog -o json)"
  jq -r --argjson t "$CHANGED_AT" '.items[]
    | "\(.metadata.name) created=\(.metadata.creationTimestamp) after_change=\((.metadata.creationTimestamp | fromdateiso8601) >= $t) terminating=\(.metadata.deletionTimestamp != null)"' <<<"$pods"
  kubectl get deploy rask-catalog -o jsonpath='deploy/rask-catalog available={.status.conditions[?(@.type=="Available")].status}{"\n"}'
  jq -e --argjson t "$CHANGED_AT" '[.items[] | select(.metadata.deletionTimestamp == null)] as $live
    | ($live | length) > 0 and all($live[]; (.metadata.creationTimestamp | fromdateiso8601) >= $t)' <<<"$pods" >/dev/null &&
    [ "$(kubectl get deploy rask-catalog -o jsonpath='{.status.conditions[?(@.type=="Available")].status}')" = True ]
}

# (c) OpenFGA's tuples, read through its HTTP API from inside the catalog container (its NetworkPolicy reaches OpenFGA),
# with a token OpenFGA admits: rask-sa-jobs's, projected for its audience, given on stdin.
# The store every reader and writer names (lakehouse.governed.auth.write_model.STORE_NAME); each store of that name is
# read, the newest being the one the services use.
FGA_STORE="lance-catalog"
FGA_OBJECT="warehouse:lance_catalog"
FGA_RELATION="maintenance"
FGA_USER="user:kubernetes~system%3Aserviceaccount%3Adefault%3Arask-sa-maintenance"
FGA_READ='import json, sys, urllib.request
token = sys.stdin.read().strip()
api, name, obj, relation, user = sys.argv[1:6]
def call(path, body=None):
    req = urllib.request.Request(api + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)
stores = [s for s in call("/stores")["stores"] if s["name"] == name]
if not stores:
    sys.exit("no store named " + name)
for s in stores:
    found = call("/stores/" + s["id"] + "/read", {"tuple_key": {"object": obj, "relation": relation, "user": user}})["tuples"]
    holders = call("/stores/" + s["id"] + "/read", {"tuple_key": {"object": obj, "relation": relation}})["tuples"]
    print(s["id"], len(found), "holders_of_" + relation + "=" + ",".join(t["key"]["user"] for t in holders))'
# fga_tuple_count: prints `<store> <count>` per store named FGA_STORE, then `total=<n>`; fails when it cannot read.
fga_tuple_count() {
  local out
  out="$(kubectl create token rask-sa-jobs --audience rask-openfga --duration 10m |
    kubectl exec -i deploy/rask-catalog -c catalog -- python -c "$FGA_READ" http://rask-openfga:8080 "$FGA_STORE" "$FGA_OBJECT" "$FGA_RELATION" "$FGA_USER" 2>&1)" ||
    { echo "OpenFGA unreadable: $out"; return 1; }
  echo "$out"
  echo "total=$(awk '{n += $2} END {print n + 0}' <<<"$out")"
}
check_maintenance_tuple_present() {
  local out
  out="$(fga_tuple_count)" || { echo "$out"; return 1; }
  echo "tuple $FGA_OBJECT#$FGA_RELATION@$FGA_USER: $out (want total>0)" | tr '\n' ' '
  echo
  ! grep -qx 'total=0' <<<"$out"
}
check_maintenance_tuple_revoked() {
  local out
  out="$(fga_tuple_count)" || { echo "$out"; return 1; }
  echo "tuple $FGA_OBJECT#$FGA_RELATION@$FGA_USER: $out (want total=0)" | tr '\n' ' '
  echo
  grep -qx 'total=0' <<<"$out"
}

# (d) The bootstrap Jobs ran again: a pod of the nats stream Job started after the change, and its Job completed.
check_nats_stream_job_reran() {
  local pods job ok=1
  pods="$(kubectl get pods -l app.kubernetes.io/component=nats-stream -o json)"
  jq -r --argjson t "$CHANGED_AT" '.items[]
    | "\(.metadata.labels["batch.kubernetes.io/job-name"]) pod=\(.metadata.name) created=\(.metadata.creationTimestamp) after_change=\((.metadata.creationTimestamp | fromdateiso8601) >= $t) phase=\(.status.phase)"' <<<"$pods"
  kubectl get jobs -o custom-columns='JOB:.metadata.name,CREATED:.metadata.creationTimestamp,SUCCEEDED:.status.succeeded,FAILED:.status.failed' 2>/dev/null | grep -e '^JOB' -e nats-stream || true
  for job in $(jq -r --argjson t "$CHANGED_AT" '[.items[] | select((.metadata.creationTimestamp | fromdateiso8601) >= $t) | .metadata.labels["batch.kubernetes.io/job-name"]] | unique | .[]' <<<"$pods"); do
    [ "$(kubectl get job "$job" -o jsonpath='{.status.succeeded}' 2>/dev/null)" -ge 1 ] 2>/dev/null && ok=0
  done
  return "$ok"
}

FAILED=()
# run_check <fn>: retries <fn> every 10 s for CHECK_WINDOW seconds until it passes, then prints PASS or FAIL with the
# last evidence.
run_check() {
  local fn="$1" deadline out rc
  deadline=$(($(date +%s) + CHECK_WINDOW))
  while :; do
    rc=0
    out="$("$fn" 2>&1)" || rc=$?
    [ "$rc" -eq 0 ] && break
    [ "$(date +%s)" -ge "$deadline" ] && break
    sleep 10
  done
  if [ "$rc" -eq 0 ]; then echo "PASS $fn"; else echo "FAIL $fn (after ${CHECK_WINDOW}s)"; FAILED+=("$fn"); fi
  sed 's/^/     /' <<<"$out"
}

# ── Phase 1: from empty to reconciled ───────────────────────────────────────────────────────────────────────────────
echo "==> fixtures"
install_stack_fixtures

echo "==> the chart, served from inside the cluster as a Helm repository"
PKG="$(mktemp -d)"
helm package ./chart -d "$PKG" >/dev/null
CHART_VERSION="$(helm show chart ./chart | awk '/^version:/ {print $2}')"
helm repo index "$PKG" --url "$REPO_URL"
ls -l "$PKG"
# A ConfigMap holds at most 1 MiB in all.
size="$(du -cb "$PKG"/* | awk 'END {print $1}')"
echo "repository files: $size bytes (a ConfigMap holds 1048576)"
[ "$size" -lt 1000000 ] || { echo "!! the packaged chart does not fit in a ConfigMap; serve it another way"; exit 1; }
kubectl create namespace "$REPO_NAMESPACE"
kubectl -n "$REPO_NAMESPACE" create configmap rask-charts --from-file="$PKG"
kubectl -n "$REPO_NAMESPACE" apply -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata: { name: rask-charts }
spec:
  selector: { matchLabels: { app: rask-charts } }
  template:
    metadata: { labels: { app: rask-charts } }
    spec:
      automountServiceAccountToken: false
      securityContext: { runAsNonRoot: true, runAsUser: 65534, runAsGroup: 65534, seccompProfile: { type: RuntimeDefault } }
      containers:
        - name: httpd
          image: busybox:1.36.1
          command: [httpd, -f, -v, -p, "8080", -h, /www]
          ports: [{ containerPort: 8080 }]
          readinessProbe: { tcpSocket: { port: 8080 } }
          securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } }
          volumeMounts: [{ name: charts, mountPath: /www, readOnly: true }]
      volumes: [{ name: charts, configMap: { name: rask-charts } }]
---
apiVersion: v1
kind: Service
metadata: { name: rask-charts }
spec:
  selector: { app: rask-charts }
  ports: [{ port: 80, targetPort: 8080 }]
EOF
kubectl -n "$REPO_NAMESPACE" rollout status deploy/rask-charts --timeout=300s
kubectl get --raw "/api/v1/namespaces/$REPO_NAMESPACE/services/rask-charts:80/proxy/index.yaml" | grep -E '^\s+version:|^\s+- http'

echo "==> Argo CD $ARGOCD_VERSION, pinned"
MANIFEST="$(mktemp)"
curl -fsSL -o "$MANIFEST" "$ARGOCD_MANIFEST"
echo "manifest sha256=$(sha256sum "$MANIFEST" | cut -d' ' -f1) bytes=$(wc -c <"$MANIFEST")"
grep -E '^kind:' "$MANIFEST" | sort | uniq -c
grep -E '^\s+image:' "$MANIFEST" | sort -u || true
kubectl create namespace argocd
# Server-side: the CRDs exceed the last-applied annotation a client-side apply writes.
kubectl -n argocd apply --server-side --force-conflicts -f "$MANIFEST" >/dev/null
for w in $(kubectl -n argocd get deploy,statefulset -o name); do kubectl -n argocd rollout status "$w" --timeout=300s; done

echo "==> rask, reconciled by Argo CD"
start_job_recorder "$JOB_LOGS"
VALUES_JSON="$(values_json "${HELM_SET[@]}" --set console.enabled=true)"
CHANGE_JSON="$(values_json -f <(printf '%s' "$CHANGE"))"
echo "values: $VALUES_JSON"
# The project every Application names; Argo CD's API server creates it, and a core install runs none.
kubectl apply -f - <<'EOF'
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata: { name: default, namespace: argocd }
spec:
  sourceRepos: ["*"]
  destinations: [{ server: "*", namespace: "*" }]
  clusterResourceWhitelist: [{ group: "*", kind: "*" }]
EOF
jq -n --argjson values "$VALUES_JSON" --arg repo "$REPO_URL" --arg version "$CHART_VERSION" --arg release "$RELEASE" '{
  apiVersion: "argoproj.io/v1alpha1", kind: "Application",
  metadata: {name: $release, namespace: "argocd"},
  spec: {
    project: "default",
    destination: {server: "https://kubernetes.default.svc", namespace: "default"},
    source: {repoURL: $repo, chart: "rask", targetRevision: $version, helm: {releaseName: $release, valuesObject: $values}},
    syncPolicy: {automated: {prune: true, selfHeal: true}}}}' | kubectl apply -f -

PHASE1_START=$(date +%s)
wait_for "$SYNC_TIMEOUT" "Argo CD to sync the release (phase 1)" argocd_reconciled
workloads_ready
kubectl get pods -o wide
echo "==> phase 1 PASSED in $(($(date +%s) - PHASE1_START))s: $(kubectl get deploy,statefulset --no-headers | wc -l) workloads Ready"

# ── Phase 2: one input changed ──────────────────────────────────────────────────────────────────────────────────────
echo "==> phase 2 baseline"
# The tuple must be there before the change, or its revocation check measures nothing; the rest is evidence.
run_check check_maintenance_tuple_present
check_lineage_max_age || true
kubectl get pods -l app.kubernetes.io/component=catalog -o custom-columns='POD:.metadata.name,CREATED:.metadata.creationTimestamp'
kubectl get pods -l app.kubernetes.io/component=nats-stream -o custom-columns='POD:.metadata.name,CREATED:.metadata.creationTimestamp,PHASE:.status.phase'

echo "==> phase 2: the change"
printf '%s\n' "$CHANGE"
CHANGED_AT=$(date +%s)
# The change is made to the Application alone: automated sync carries it to the cluster, with no sync requested by hand.
kubectl -n argocd patch application "$RELEASE" --type merge -p "$(jq -n --argjson c "$CHANGE_JSON" '{spec: {source: {helm: {valuesObject: $c}}}}')"
SINCE=$CHANGED_AT
PHASE2=0
wait_for "$SYNC_TIMEOUT" "Argo CD's automated sync of the change (phase 2)" argocd_reconciled || PHASE2=1
workloads_ready || PHASE2=1
[ "$PHASE2" -eq 0 ] || FAILED+=("phase-2-reconcile")

echo "==> phase 2 checks"
for c in "${CHECKS[@]}"; do run_check "$c"; done

if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "==> gitops-from-empty: FAILED: ${FAILED[*]}"
  exit 1
fi
echo "==> gitops-from-empty: every changed input converged"
