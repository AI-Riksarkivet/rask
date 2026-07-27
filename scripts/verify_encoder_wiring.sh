#!/usr/bin/env bash
# #123 gate: the chart's `encoders.*` values are what turn the vector search modes on.
#
# Before this change `MEDIA_EMBED_URL` / `MEDIA_RERANK_URL` were unreachable from an operator:
# `services/common/core/config.py` defaults them to `http://127.0.0.1:8001` / `:8002`, which inside
# a pod is the POD's own loopback, and no template rendered the env. So `semantic`, `visual`,
# `scene`, `hybrid`, `all` and `rerank=true` were permanently 503 on any deployment, with no value
# an operator could set. That unchangeable default IS the bug — not a missing service.
#
# What this proves, in order:
#   1. render, unset  — no encoder env is emitted anywhere (the honest "no encoder deployed" state);
#   2. render, set    — BOTH search and viewer carry both URLs, and the annotator carries neither.
#                       search CALLS them; the viewer PROBES them for /api/health, which is what
#                       drives the media zone's mode gating — if the two disagreed the UI would
#                       disable a mode search can actually serve. The annotator does neither.
#   3. live, before   — `mode=semantic` through the ingress on :8090 is 503;
#   4. live, after    — with the values pointed at a server that speaks the encoder wire protocol,
#                       the SAME url returns 200 with rows, and `rerank=true` returns 200;
#   5. live, restored — unset the values and the 503 comes back, so it was the chart that flipped it.
#
# The server in step 4 is `scripts/encoder_stub.py`, deployed by this script and deleted at the end.
# It is a WIRE-PROTOCOL STUB, not an encoder: its vectors are a hash of the request body, so the rows
# it returns are real rows ranked meaninglessly. That is deliberate and sufficient — the claim under
# test is "the chart wiring is load-bearing end to end", not "the ranking is good". Proving the
# ranking needs the real Qwen3-VL-Embedding-2B on vLLM, which needs a GPU this kind cluster cannot
# schedule (no device plugin, no nvidia runtime in the node's containerd, no nvidia.com/gpu in
# capacity) — which is exactly why `encoders.*` is a URL to an external fleet, not an in-chart
# Deployment, the same call as the existing `runners.jobsUrl`.
#
# Run from the repo root against the kind `lance` cluster: `bash scripts/verify_encoder_wiring.sh`.
set -euo pipefail

RELEASE="${RELEASE:-lance-ns}"
CHART="${CHART:-chart}"
INGRESS="${INGRESS:-http://localhost:8090}"
STUB="$RELEASE-encoder-stub"
# The corpus's vector width. Exposed so the runtime legs can be proven non-vacuous without editing
# anything: `STUB_DIM=384 bash scripts/verify_encoder_wiring.sh` serves vectors the search client's
# `expected_dim` check rejects, and step 5 must then fail with the same 503 step 3 started from.
STUB_DIM="${STUB_DIM:-2048}"
export PATH="$PWD/.localbin:$PATH"

WORK="$(mktemp -d)"
LIVE_VALUES="$WORK/live-values.yaml"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
step() { echo; echo "━━ $* ━━"; }
fail() {
  echo "!! FAIL: $*"
  exit 1
}

# Search through the PUBLIC ingress, not a port-forward: #123 is a product bug, so the proof has to
# travel the path a user's browser takes (zone BFF proxy → search service).
search() { curl -s -o "$WORK/body.json" -w '%{http_code}' -m 120 "$INGRESS/media/api/search?q=music&n=5&$1"; }
rows() { python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(len(d) if isinstance(d,list) else -1)' "$WORK/body.json"; }
detail() { head -c 200 "$WORK/body.json"; }

# One rendered Deployment, sliced out of a multi-doc render by name, so an env var landing on the
# WRONG workload can never satisfy the assertion below.
deployment() {
  python3 - "$1" "$2" <<'PY'
import sys

text = open(sys.argv[1]).read()
name = sys.argv[2]
for doc in text.split("\n---\n"):
    if "kind: Deployment" in doc and f"\n  name: {name}\n" in doc:
        print(doc)
        break
else:
    sys.exit(f"no Deployment named {name} in the render")
PY
}

# ── 1 + 2. the render contract (offline: helm only, no cluster needed) ────────────────────────────
# media.enabled=true because the chart ships the media plane off by default — the encoder env lives
# on the media workloads, so a render with media off would assert nothing at all.

step "1/5 render with encoders unset — no encoder env anywhere"
helm template "$RELEASE" "$CHART" --set media.enabled=true >"$WORK/off.yaml"
for var in MEDIA_EMBED_URL MEDIA_RERANK_URL; do
  n=$(grep -c "$var" "$WORK/off.yaml" || true)
  [ "$n" = "0" ] || fail "$var must not render when encoders.* is unset (found $n) — unset is the honest-off state"
done
echo "no MEDIA_EMBED_URL / MEDIA_RERANK_URL rendered (honest off)"

step "2/5 render with encoders set — both URLs on search + viewer, neither on annotator"
helm template "$RELEASE" "$CHART" --set media.enabled=true \
  --set encoders.embedUrl=http://enc.test:8001 \
  --set encoders.rerankUrl=http://enc.test:8002 >"$WORK/on.yaml"
for comp in search viewer; do
  deployment "$WORK/on.yaml" "$RELEASE-$comp" >"$WORK/$comp.yaml" || fail "could not slice the $comp Deployment"
  grep -qF 'name: MEDIA_EMBED_URL, value: "http://enc.test:8001"' "$WORK/$comp.yaml" ||
    fail "$comp does not render MEDIA_EMBED_URL from encoders.embedUrl — the value is not wired to this workload"
  grep -qF 'name: MEDIA_RERANK_URL, value: "http://enc.test:8002"' "$WORK/$comp.yaml" ||
    fail "$comp does not render MEDIA_RERANK_URL from encoders.rerankUrl — the value is not wired to this workload"
  echo "$comp: MEDIA_EMBED_URL + MEDIA_RERANK_URL rendered from values"
done
deployment "$WORK/on.yaml" "$RELEASE-annotator" >"$WORK/annotator.yaml"
grep -q "MEDIA_EMBED_URL\|MEDIA_RERANK_URL" "$WORK/annotator.yaml" &&
  fail "the annotator must not carry encoder env — it neither calls nor probes them"
echo "annotator: no encoder env (correct — it neither calls nor probes them)"

# 2b. `encoders` is a new TOP-LEVEL key, and `helm upgrade --reuse-values` merges the STORED values
# over the chart defaults — a release upgraded that way has no `encoders` key at all. A bare
# `.Values.encoders.embedUrl` then aborts the whole render ("nil pointer evaluating interface
# {}.embedUrl"), which is why media.yaml goes through `| default dict`. `--set encoders=null` is the
# same shape as the stored-values case, so it reproduces it without needing a release.
helm template "$RELEASE" "$CHART" --set media.enabled=true --set encoders=null >"$WORK/nil.yaml" 2>"$WORK/nil.err" ||
  fail "the chart does not render when encoders is nil: $(head -2 "$WORK/nil.err") — a --reuse-values upgrade would fail here"
grep -qE '^\s+- \{ name: MEDIA_(EMBED|RERANK)_URL' "$WORK/nil.yaml" &&
  fail "encoder env rendered from a nil encoders key"
echo "encoders=null (the --reuse-values shape): renders clean, no encoder env, no nil-pointer abort"

# ── 3. live baseline ─────────────────────────────────────────────────────────────────────────────

step "3/5 live baseline through $INGRESS — the vector modes are 503"
kubectl get "deploy/$RELEASE-search" >/dev/null 2>&1 || fail "release $RELEASE has no search deployment (media.enabled?)"
code=$(search "mode=semantic")
[ "$code" = "503" ] || fail "expected mode=semantic to be 503 before wiring, got $code — is an encoder already configured? ($(detail))"
echo "mode=semantic → 503 $(detail)"

# Snapshot the operator's own values so the two upgrades below preserve them. NOT --reuse-values:
# a brand-new key like encoders.* renders EMPTY under --reuse-values, which is exactly how a past
# run shipped an empty env into a running service.
helm get values "$RELEASE" -o yaml >"$LIVE_VALUES"
grep -q "^media:" "$LIVE_VALUES" || fail "captured release values look wrong — refusing to upgrade with them"

restore() {
  echo "restoring: encoders unset, stub deleted"
  helm upgrade "$RELEASE" "$CHART" -f "$LIVE_VALUES" >/dev/null
  kubectl delete deploy,svc,configmap -l "app.kubernetes.io/component=encoder-stub" --ignore-not-found >/dev/null
  kubectl rollout status "deploy/$RELEASE-search" --timeout=240s >/dev/null
}

# ── 4. wire it to a server that speaks the protocol ──────────────────────────────────────────────

step "4/5 deploy the wire-protocol stub, point the chart at it, drive the same URL"
kubectl delete deploy,svc,configmap -l "app.kubernetes.io/component=encoder-stub" --ignore-not-found >/dev/null
kubectl create configmap "$STUB" --from-file=encoder_stub.py=scripts/encoder_stub.py >/dev/null
kubectl label configmap "$STUB" "app.kubernetes.io/component=encoder-stub" >/dev/null
# Runs inside the already-deployed service image (python 3.13 at /opt/venv/bin/python3), so there is
# no image to build and no `kind load` — and therefore none of the same-tag stale-digest trap.
IMAGE=$(kubectl get "deploy/$RELEASE-search" -o jsonpath='{.spec.template.spec.containers[0].image}')
kubectl apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: { name: $STUB, labels: { app.kubernetes.io/component: encoder-stub } }
spec:
  replicas: 1
  selector: { matchLabels: { app.kubernetes.io/component: encoder-stub } }
  template:
    metadata: { labels: { app.kubernetes.io/component: encoder-stub } }
    spec:
      volumes: [{ name: stub, configMap: { name: $STUB } }]
      containers:
        - name: embed
          image: $IMAGE
          command: [python3, /stub/encoder_stub.py, --port, "8001", --dim, "$STUB_DIM"]
          ports: [{ containerPort: 8001 }]
          volumeMounts: [{ name: stub, mountPath: /stub }]
          resources: { requests: { cpu: 50m, memory: 64Mi }, limits: { cpu: "1", memory: 256Mi } }
        - name: rerank
          image: $IMAGE
          command: [python3, /stub/encoder_stub.py, --port, "8002", --dim, "$STUB_DIM"]
          ports: [{ containerPort: 8002 }]
          volumeMounts: [{ name: stub, mountPath: /stub }]
          resources: { requests: { cpu: 50m, memory: 64Mi }, limits: { cpu: "1", memory: 256Mi } }
---
apiVersion: v1
kind: Service
metadata: { name: $STUB, labels: { app.kubernetes.io/component: encoder-stub } }
spec:
  selector: { app.kubernetes.io/component: encoder-stub }
  ports:
    - { name: embed, port: 8001, targetPort: 8001 }
    - { name: rerank, port: 8002, targetPort: 8002 }
YAML
kubectl rollout status "deploy/$STUB" --timeout=180s >/dev/null || fail "stub did not become ready"

trap 'restore; cleanup' EXIT
helm upgrade "$RELEASE" "$CHART" -f "$LIVE_VALUES" \
  --set "encoders.embedUrl=http://$STUB:8001" \
  --set "encoders.rerankUrl=http://$STUB:8002" >/dev/null
kubectl rollout status "deploy/$RELEASE-search" --timeout=300s >/dev/null
kubectl rollout status "deploy/$RELEASE-viewer" --timeout=300s >/dev/null

# ── 5. the proof ─────────────────────────────────────────────────────────────────────────────────

step "5/5 the previously-503 modes now answer through $INGRESS"
bad=0
for q in "mode=semantic" "mode=visual" "mode=scene" "mode=hybrid" "mode=all" "mode=fts&rerank=true"; do
  code=$(search "$q")
  n=$(rows)
  printf '  %-24s %s rows=%s\n' "$q" "$code" "$n"
  if [ "$code" != "200" ]; then
    echo "    body: $(detail)"
    bad=$((bad + 1))
  fi
done
[ "$bad" = "0" ] || fail "$bad mode(s) still failed with the encoder URLs set — the wiring does not reach the search service"

# Rows, not just a 200: an empty list would prove the encoder was reached but not that its vector
# made it into Lance's index scan.
code=$(search "mode=semantic")
n=$(rows)
{ [ "$code" = "200" ] && [ "$n" -gt 0 ]; } || fail "mode=semantic returned $code with $n rows — expected 200 with rows"
echo "mode=semantic → 200 with $n rows"
python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print("  first hit:",{k:d[0][k] for k in ("doc_id","chunk_id","_distance") if k in d[0]})' "$WORK/body.json"

# The health endpoint the media zone reads for its mode gating must agree with what search serves.
kubectl port-forward "svc/$RELEASE-viewer" 18109:8101 >/dev/null 2>&1 &
pf=$!
for _ in $(seq 20); do
  sleep 0.5
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18109/api/health || true)" = "200" ] && break
done
health=$(curl -s -m 10 http://127.0.0.1:18109/api/health || true)
kill "$pf" 2>/dev/null || true
echo "$health" | python3 -c 'import json,sys;d=json.load(sys.stdin);print("  viewer /api/health embed:",d["embed"],"\n  viewer /api/health rerank:",d["rerank"])'
echo "$health" | grep -q '"ok":true' ||
  fail "the viewer health probe still reports the encoders down — the UI would disable modes search can serve"

step "restore — unsetting the values must bring the 503 back"
restore
trap cleanup EXIT
code=$(search "mode=semantic")
[ "$code" = "503" ] || fail "mode=semantic is $code after unsetting encoders.* — something other than the chart value is serving it"
echo "mode=semantic → 503 again $(detail)"

echo
echo "✓ #123: encoders.embedUrl/rerankUrl are load-bearing — 503 → 200 with rows → 503, through $INGRESS"
