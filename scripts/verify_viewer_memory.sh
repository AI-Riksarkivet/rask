#!/usr/bin/env bash
# #121 gate: the viewer's memory tier holds the working set the media zone actually creates.
#
# The viewer used to fall through to `resources.default` (512Mi), the tier meant for stateless request
# pods. It is not one: it holds an in-process lance-graph CypherEngine over the knowledge graph
# (139,407 entities / 290,408 relationships / 507,344 mentions) plus a cache of Arrow-IPC atlas point
# clouds, both sized by the corpus. ONE `GET /api/graph/status` — what
# `media/src/routes/graph/+page.svelte` calls on mount — took the container from 180Mi to 736Mi
# resident with a 955Mi transient and was OOM-killed every time: exit 137, kernel `Memory cgroup out
# of memory: Killed process (uvicorn) anon-rss:510000kB`. The annotator's next thumbnail fetch then
# hit a dead upstream through its BFF proxy and surfaced the bare 502 that #121 was filed as.
# Thumbnails were the victim, not the cause: the entire corpus of 1154 of them costs +15Mi.
#
# Four legs, cheapest first:
#   1. render  — the chart itself gives the viewer a tier at or above the floor (no cluster needed,
#                so a values regression fails here before anything is deployed);
#   2. live    — the deployed limit matches the rendered one (catches a hand-patched Deployment);
#   3. drive   — the load that killed it: all three atlas point clouds, a thumbnail burst, and the
#                graph build. 200s throughout, and the container must not restart or carry a
#                terminated state;
#   4. peak    — the observed cgroup high-water is genuinely under the limit, so leg 3 was not luck.
#
# Run from the repo root against the kind `lance` cluster. Read-only apart from one port-forward.
set -euo pipefail

RELEASE="${RELEASE:-lance-ns}"
CHART="${CHART:-chart}"
# The measured high-water was 955Mi (the KG build's transient, cgroup memory.peak, 2026-07-26). The
# floor sits just above it so a tier that would survive only by luck still fails leg 1.
MIN_LIMIT_MI="${MIN_LIMIT_MI:-1024}"
# The measured steady working set after a full concurrent drive was 609Mi, 736Mi with the graph
# engine resident. Requests below that make the scheduler under-reserve what the pod always holds.
MIN_REQUEST_MI="${MIN_REQUEST_MI:-512}"
PORT="${PORT:-18101}"
export PATH="$PWD/.localbin:$PATH"

WORK="$(mktemp -d)"
PF_PID=""
cleanup() {
  [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null
  rm -rf "$WORK"
  return 0
}
trap cleanup EXIT
step() { echo; echo "━━ $* ━━"; }
fail() {
  echo "!! FAIL: $*"
  exit 1
}

# Mi from a k8s memory quantity (the only units the chart uses are Mi and Gi).
to_mi() {
  case "$1" in
  *Gi) echo $((${1%Gi} * 1024)) ;;
  *Mi) echo "${1%Mi}" ;;
  *) fail "unparseable memory quantity: $1" ;;
  esac
}

# ── 1. the chart contract (offline: helm only, no cluster needed) ─────────────────────────────────
# media.enabled=true because the chart ships the media plane off by default.

step "1/4 the chart gives the viewer a tier sized for the KG build"
helm template "$RELEASE" "$CHART" --set media.enabled=true >"$WORK/render.yaml"
read -r rendered_limit rendered_request <<<"$(python3 - "$WORK/render.yaml" "$RELEASE-viewer" <<'PY'
import sys

text = open(sys.argv[1]).read()
name = sys.argv[2]
for doc in text.split("\n---\n"):
    if "kind: Deployment" in doc and f"\n  name: {name}\n" in doc:
        break
else:
    sys.exit(f"no Deployment named {name} in the render")
# `resources:` is emitted once per container and the viewer is the only container in this pod spec
# (the daprd sidecar is injected at admission, not rendered here), so the first block is the viewer's.
lines = [ln.strip() for ln in doc.splitlines()]
i = lines.index("resources:")
block = lines[i + 1 : i + 7]
section, found = None, {}
for line in block:
    if line in ("limits:", "requests:"):
        section = line[:-1]
    elif line.startswith("memory:") and section:
        found[section] = line.split()[-1]
if set(found) != {"limits", "requests"}:
    sys.exit(f"could not read both memory quantities from the viewer's resources block: {block}")
print(found["limits"], found["requests"])
PY
)"
[ -n "$rendered_limit" ] && [ -n "$rendered_request" ] || fail "could not read the viewer's rendered memory resources"
lim_mi=$(to_mi "$rendered_limit")
req_mi=$(to_mi "$rendered_request")
echo "chart renders viewer memory: requests $rendered_request (${req_mi}Mi), limits $rendered_limit (${lim_mi}Mi)"
[ "$lim_mi" -ge "$MIN_LIMIT_MI" ] ||
  fail "viewer memory limit $rendered_limit is below the ${MIN_LIMIT_MI}Mi floor — one /api/graph/status will OOM-kill it (measured peak 955Mi)"
[ "$req_mi" -ge "$MIN_REQUEST_MI" ] ||
  fail "viewer memory request $rendered_request is below the ${MIN_REQUEST_MI}Mi floor — the scheduler would under-reserve the 609Mi steady working set"

# `resources.viewer` is a new key, and `lance.resources` falls back to `resources.default` when it is
# missing — SILENTLY, back to the 512Mi tier that OOM-killed the pod. `helm upgrade --reuse-values`
# produces exactly that (stored values merged over chart defaults, no new key), so this asserts leg 1
# would catch it rather than the operator finding out from an exit 137.
missing=$(helm template "$RELEASE" "$CHART" --set media.enabled=true --set resources.viewer=null |
  python3 -c 'import sys;t=sys.stdin.read();d=[x for x in t.split("\n---\n") if "kind: Deployment" in x and "\n  name: '"$RELEASE"'-viewer\n" in x][0];l=[y.strip() for y in d.splitlines()];i=l.index("resources:");print(next(y.split()[-1] for y in l[i+1:i+7] if y.startswith("memory:")))')
[ "$(to_mi "$missing")" -lt "$MIN_LIMIT_MI" ] ||
  fail "dropping resources.viewer still renders $missing — leg 1 cannot distinguish the tier from the fallback, so it proves nothing"
echo "without resources.viewer the chart falls back to $missing (below the floor) — leg 1 is load-bearing"

# ── 2. what is actually deployed ─────────────────────────────────────────────────────────────────

step "2/4 the deployed viewer matches what the chart renders"
live_limit=$(kubectl get "deploy/$RELEASE-viewer" \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="viewer")].resources.limits.memory}')
[ -n "$live_limit" ] || fail "the deployed viewer has no memory limit at all"
[ "$(to_mi "$live_limit")" = "$lim_mi" ] ||
  fail "deployed viewer limit $live_limit != rendered $rendered_limit — the cluster has drifted from the chart"
echo "deployed viewer memory limit = $live_limit"

# ── 3. drive the load that killed it ─────────────────────────────────────────────────────────────

step "3/4 drive the load that OOM-killed the 512Mi pod"
pod=$(kubectl get pods -l app.kubernetes.io/component=viewer -o jsonpath='{.items[0].metadata.name}')
[ -n "$pod" ] || fail "no viewer pod (is media.enabled set?)"
before=$(kubectl get "pod/$pod" -o jsonpath='{.status.containerStatuses[?(@.name=="viewer")].restartCount}')
echo "pod=$pod restarts before=$before"

kubectl port-forward "svc/$RELEASE-viewer" "$PORT:8101" >/dev/null 2>&1 &
PF_PID=$!
for _ in $(seq 30); do
  sleep 0.5
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/health" || true)" = "200" ] && break
done

# `|| true` on every curl: an OOM kill drops the connection mid-response and curl exits 52, which
# under `set -e` would abort with a bare transport error instead of naming the failure.
get() { curl -s -o /dev/null -m 300 -w '%{http_code}' "http://127.0.0.1:$PORT$1" || true; }

# a. the three atlas point clouds — ~42Mi of Arrow IPC each, all retained in the points cache.
for space in text visual caption; do
  code=$(get "/api/atlas/points?space=$space")
  echo "  GET /api/atlas/points?space=$space → $code"
  [ "$code" = "200" ] || fail "atlas points ($space) returned $code"
done

# b. a thumbnail burst — the load #121 blamed. One page of documents, fetched twice at concurrency 8:
#    many times an annotator grid, and the measurement said the whole 1154-doc corpus costs +15Mi.
mapfile -t docs < <(curl -s -m 60 "http://127.0.0.1:$PORT/api/documents?per_page=100&page=1" |
  python3 -c 'import json,sys;print("\n".join(d["doc_id"] for d in json.load(sys.stdin)["docs"]))')
[ "${#docs[@]}" -gt 0 ] || fail "no documents returned — cannot drive the thumbnail load"
printf '%s\n' "${docs[@]}" "${docs[@]}" |
  xargs -P 8 -I{} curl -s -o /dev/null -m 60 "http://127.0.0.1:$PORT/api/thumbnail/{}"
echo "  ${#docs[@]} thumbnails x2 at concurrency 8 → done"

# c. the killer: the in-process Cypher engine build.
code=$(get "/api/graph/status")
body=$(curl -s -m 300 "http://127.0.0.1:$PORT/api/graph/status" || true)
echo "  GET /api/graph/status → ${code:-<no response>}  $body"
[ "$code" = "200" ] ||
  fail "/api/graph/status returned '${code:-<connection dropped>}' — the container was killed mid-request (kubectl get pod $pod -o jsonpath='{.status.containerStatuses[*].lastState}')"
case "$body" in
*'"built":true'*) ;;
*) fail "the graph is not built in this corpus — the drive proves nothing; stage the KG tables first" ;;
esac

after=$(kubectl get "pod/$pod" -o jsonpath='{.status.containerStatuses[?(@.name=="viewer")].restartCount}')
[ "$after" = "$before" ] || fail "viewer restarted during the drive ($before → $after) — still being OOM-killed"
reason=$(kubectl get "pod/$pod" -o jsonpath='{.status.containerStatuses[?(@.name=="viewer")].lastState.terminated.reason}')
[ -z "$reason" ] || fail "the viewer container's last termination reason is '$reason' — it has been killed under this tier"
echo "restarts after=$after, no terminated state on the viewer container"

# ── 4. the high-water actually fits ──────────────────────────────────────────────────────────────

step "4/4 the observed high-water fits under the limit"
peak_mi=$(kubectl exec "$pod" -c viewer -- cat /sys/fs/cgroup/memory.peak | awk '{printf "%d", $1/1048576}')
cur_mi=$(kubectl exec "$pod" -c viewer -- cat /sys/fs/cgroup/memory.current | awk '{printf "%d", $1/1048576}')
echo "cgroup memory.current = ${cur_mi}Mi, memory.peak = ${peak_mi}Mi, limit = ${lim_mi}Mi"
[ "$peak_mi" -lt "$lim_mi" ] || fail "high-water ${peak_mi}Mi reached the ${lim_mi}Mi limit"

echo
echo "✓ #121: the viewer holds the atlas + thumbnail + KG-build load (${peak_mi}Mi peak under ${lim_mi}Mi), no restart"
