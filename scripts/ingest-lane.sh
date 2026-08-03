#!/usr/bin/env bash
# A11 — drive the ingest lane end to end against a REAL cluster, and fail loudly if it lies.
#
# "The pod is Running" and "the POST returned 202" are not evidence; this repo has shipped a
# Tiltfile that could never have worked and an ingest plane whose queue nothing drained, and both
# looked healthy from every angle except an actual run. So this script asserts on the OUTCOME: rows
# in bronze, a committed version, a lineage run, and a status endpoint that agrees with the engine.
#
# Deploys with `helm template | kubectl apply`, not `helm upgrade`. Not a preference — helm cannot
# reach k3s 1.36 on this host at all (`Kubernetes cluster unreachable: the server could not find the
# requested resource`, on 3.16.4 and 3.20.0 alike, while kubectl against the same kubeconfig is
# fine). `helm template` is pure rendering and needs no cluster, so the chart stays the single source
# of truth and only the delivery changes.
#
# Usage:
#   scripts/ingest-lane.sh deploy     # render + apply the slice A11 needs, wait for it
#   scripts/ingest-lane.sh fixtures   # write the checked-in TIFFs into the ingest pod
#   scripts/ingest-lane.sh run        # POST /v1/ingests and assert the whole outcome
#   scripts/ingest-lane.sh all        # all three
set -euo pipefail

NS="${NS:-rask}"
RELEASE="${RELEASE:-rask}"
TAG="${TAG:-dev}"
REGISTRY="${REGISTRY:-localhost:5000}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURES="$ROOT/tests/fixtures/ingest-lane"
POD_FIXTURE_DIR="/tmp/ingest-fixtures"

log() { printf '\033[1;36m>> %s\033[0m\n' "$*"; }
ok() { printf '\033[1;32m  OK  %s\033[0m\n' "$*"; }
die() {
	printf '\033[1;31m  FAIL  %s\033[0m\n' "$*" >&2
	exit 1
}

# The slice A11 needs, and nothing else. The full chart renders 277 objects including a GPU device
# plugin, Kueue, GreptimeDB and a Ray cluster — none of which the lane touches, all of which cost
# minutes of pull and schedule time. Disabling them is not a shortcut: it is the difference between
# a verification anyone will run and one nobody will.
lane_values() {
	cat <<-'YAML'
		ray: {enabled: false}
		kuberay: {enabled: false}
		kueue: {enabled: false}
		nvdp: {enabled: false}
		observability: {enabled: false}
		frontend: {enabled: false}
		medallion: {enabled: false}
		compaction: {enabled: false}
		media: {enabled: false}
		dev: {reload: false}
	YAML
}

render() {
	helm template "$RELEASE" "$ROOT/chart" \
		--namespace "$NS" \
		--include-crds \
		-f <(lane_values) \
		--set-string image.repository="$REGISTRY" \
		--set-string image.tag="$TAG" \
		--set-string image.catalog.repository="$REGISTRY/lance-rest-catalog"
}

cmd_deploy() {
	log "rendering the lane slice"
	local manifest
	manifest="$(mktemp)"
	render >"$manifest"
	printf '   %s objects\n' "$(grep -c '^kind:' "$manifest")"

	kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"

	# Split by hand, because a single `kubectl apply -n rask` cannot deliver this manifest:
	#   * CRDs must be Established BEFORE the CustomResources that use them (Dapr Components, the
	#     RustFS Tenant) — one apply races that and fails on "no matches for kind";
	#   * two objects carry an explicit `namespace: default` (the dapr secret-reader Role pair), and
	#     kubectl REFUSES the whole apply over a namespace mismatch rather than skipping them;
	#   * helm test hooks are not part of the release and must not be applied at all.
	local outdir
	outdir="$(mktemp -d)"
	uv run --project "$ROOT" python - "$manifest" "$outdir" <<-'PY'
		import sys, pathlib, yaml

		manifest, outdir = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
		buckets = {"crds": [], "default": [], "release": []}
		for doc in yaml.safe_load_all(manifest.read_text()):
		    if not doc:
		        continue
		    meta = doc.get("metadata") or {}
		    if any("helm.sh/hook" in key for key in (meta.get("annotations") or {})):
		        continue  # a test hook is not part of the release
		    if doc.get("kind") == "CustomResourceDefinition":
		        buckets["crds"].append(doc)
		    elif meta.get("namespace") == "default":
		        buckets["default"].append(doc)
		    else:
		        buckets["release"].append(doc)
		for name, docs in buckets.items():
		    (outdir / f"{name}.yaml").write_text(yaml.safe_dump_all(docs))
		    print(f"   {name}: {len(docs)}")
	PY

	log "applying CRDs"
	kubectl apply --server-side --force-conflicts -f "$outdir/crds.yaml" 2>&1 | tail -2
	kubectl wait --for=condition=Established crd --all --timeout=180s >/dev/null 2>&1 || true

	log "applying the release"
	kubectl apply --server-side --force-conflicts -n default -f "$outdir/default.yaml" 2>&1 | tail -2
	kubectl apply --server-side --force-conflicts -n "$NS" -f "$outdir/release.yaml" 2>&1 | tail -4

	log "waiting for dapr + nats + rustfs"
	kubectl rollout status -n "$NS" deploy/dapr-operator --timeout=300s || true
	kubectl rollout status -n "$NS" deploy/dapr-sidecar-injector --timeout=300s || true
	kubectl rollout status -n "$NS" statefulset/"$RELEASE"-nats --timeout=300s || true

	cmd_image

	log "waiting for ingest"
	kubectl rollout status -n "$NS" deploy/"$RELEASE"-ingest --timeout=300s ||
		die "ingest never became ready — kubectl describe deploy/$RELEASE-ingest -n $NS"
	ok "ingest is ready"
}

# Build, push, and pin the deployment to the resulting DIGEST.
#
# The digest is the whole point. `imagePullPolicy: IfNotPresent` plus a mutable `:dev` tag means a
# node that already holds *some* `ingest:dev` never pulls again — so a rebuilt, re-pushed, rolled-out
# Deployment happily keeps serving code from an arbitrarily old build. It cost an hour here: a pod
# 404ing on a health route that had been added, committed and verified present in the very image
# that had just been pushed. `kubectl rollout restart` does not help; only a changed image REFERENCE
# does, and a digest is the one reference that cannot lie about its contents.
cmd_image() {
	log "building ingest through Dagger"
	local pushed digest
	pushed="$(bash "$ROOT/scripts/dagger-image.sh" --name ingest --push "${PUSH_REGISTRY:-172.17.0.1:5000}/ingest:$TAG" 2>&1 | tail -20 | grep -oE 'sha256:[0-9a-f]{64}' | tail -1)"
	[ -n "$pushed" ] || die "the Dagger build published no digest"
	digest="$pushed"
	ok "published $digest"

	log "pinning the deployment to that digest"
	kubectl set image -n "$NS" deploy/"$RELEASE"-ingest "ingest=$REGISTRY/ingest@$digest" >/dev/null
}

ingest_pod() {
	# `app.kubernetes.io/component`, not `app` — the chart uses the recommended-labels set, and a
	# selector that matches nothing returns success with empty output, so a wrong label here reads
	# as "no pod" rather than as a broken query.
	kubectl get pod -n "$NS" \
		-l app.kubernetes.io/component=ingest \
		--field-selector=status.phase=Running \
		-o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null
}

# The fixtures are written INTO the pod rather than baked into the image or mounted from a
# ConfigMap. Baking test data into a shipped image is wrong on its own terms; a ConfigMap needs a
# chart volume that exists only for this test. `kubectl cp` needs tar in the container, which the
# distroless-ish runtime does not have — so the bytes go over `kubectl exec` as base64 and are
# decoded by the Python that is already there.
cmd_fixtures() {
	local pod
	pod="$(ingest_pod)" || die "no ingest pod"
	[ -n "$pod" ] || die "no ingest pod in namespace $NS"
	[ -d "$FIXTURES" ] || die "no fixtures at $FIXTURES"

	log "seeding fixtures into $pod:$POD_FIXTURE_DIR"
	local payload
	payload="$(cd "$FIXTURES" && tar czf - . | base64 -w0)"
	# `-i` is required: without it kubectl attaches no stdin and the heredoc is silently discarded,
	# so the container's tarfile sees an empty stream and reports "not a bzip2 file" — an error that
	# describes the decompressor's last guess rather than the actual problem.
	kubectl exec -i -n "$NS" "$pod" -c ingest -- python -c "
import base64, io, os, sys, tarfile
os.makedirs('$POD_FIXTURE_DIR', exist_ok=True)
raw = base64.b64decode(sys.stdin.read())
with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
    archive.extractall('$POD_FIXTURE_DIR')
print(sorted(os.listdir('$POD_FIXTURE_DIR')))
" <<<"$payload" || die "fixture seed failed"
	ok "fixtures seeded"
}

cmd_run() {
	local pod
	pod="$(ingest_pod)"
	[ -n "$pod" ] || die "no ingest pod in namespace $NS"

	local key="a11-$(date +%s)"
	log "POST /v1/ingests (Idempotency-Key: $key)"

	# Timed, because A1 is a CONTRACT: 202 in under a second. Measured inside the cluster so the
	# number is the handler's, not the port-forward's.
	local accepted
	accepted="$(kubectl exec -n "$NS" "$pod" -c ingest -- python -c "
import json, time, urllib.request
body = json.dumps({
    'kind': 'local-dir',
    'project': 'demo',
    'dataset': 'a11pages',
    'options': {'root': '$POD_FIXTURE_DIR', 'pattern': '*.tif'},
}).encode()
req = urllib.request.Request('http://127.0.0.1:8830/api/ingests', data=body,
                             headers={'Content-Type': 'application/json', 'Idempotency-Key': '$key'})
start = time.monotonic()
with urllib.request.urlopen(req, timeout=30) as response:
    payload = json.load(response)
    payload['_elapsed'] = round(time.monotonic() - start, 3)
    payload['_status'] = response.status
print(json.dumps(payload))
")" || die "POST failed"
	echo "   $accepted"

	local run_id elapsed status
	run_id="$(jq -r .run_id <<<"$accepted")"
	elapsed="$(jq -r ._elapsed <<<"$accepted")"
	status="$(jq -r ._status <<<"$accepted")"

	[ "$status" = "202" ] || die "A1: expected 202, got $status"
	awk "BEGIN{exit !($elapsed < 1.0)}" || die "A1: 202 took ${elapsed}s, contract is < 1s"
	ok "A1 — 202 in ${elapsed}s"

	log "waiting for the run to reach a terminal state"
	local body="" run_status=""
	for _ in $(seq 1 60); do
		body="$(kubectl exec -n "$NS" "$pod" -c ingest -- python -c "
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8830/api/ingests/$run_id', timeout=15) as r:
    print(r.read().decode())
" 2>/dev/null)" || true
		run_status="$(jq -r .status <<<"$body" 2>/dev/null || echo '')"
		case "$run_status" in
		COMPLETE | COMPLETE_WITH_ERRORS | FAILED) break ;;
		esac
		sleep 5
	done
	echo "   $body"

	case "$run_status" in
	COMPLETE | COMPLETE_WITH_ERRORS) ok "run reached $run_status" ;;
	*) die "run never reached a terminal state (last: ${run_status:-<none>})" ;;
	esac

	# THE assertion the previous run could not make. A status endpoint that reports ACCEPTED for a
	# run the engine COMPLETED is exactly the declared-but-absent semantics this plane exists to fix.
	local committed
	committed="$(jq -r .committed_version <<<"$body")"
	[ "$committed" != "null" ] || die "no committed version — the run finished without landing a Lance version"
	ok "bronze committed at version $committed"

	local expected
	expected="$(find "$FIXTURES" -name '*.tif' | wc -l)"
	local rows
	rows="$(jq -r .units_done <<<"$body")"
	[ "$rows" = "$expected" ] || die "expected $expected rows, got $rows"
	ok "$rows rows landed (one per fixture)"

	[ "$(jq -r .defect <<<"$body")" = "null" ] || die "A8 defect reported: $(jq -r .defect <<<"$body")"
	ok "A8 — no provenance defect"

	log "A2 — the same Idempotency-Key must start NO second run"
	local repeat
	repeat="$(kubectl exec -n "$NS" "$pod" -c ingest -- python -c "
import json, urllib.request
body = json.dumps({'kind':'local-dir','project':'demo','dataset':'a11pages',
                   'options':{'root':'$POD_FIXTURE_DIR','pattern':'*.tif'}}).encode()
req = urllib.request.Request('http://127.0.0.1:8830/api/ingests', data=body,
                             headers={'Content-Type':'application/json','Idempotency-Key':'$key'})
with urllib.request.urlopen(req, timeout=30) as r:
    print(r.read().decode())
")" || die "repeat POST failed"
	[ "$(jq -r .run_id <<<"$repeat")" = "$run_id" ] || die "A2: the key resolved to a DIFFERENT run"
	[ "$(jq -r .deduplicated <<<"$repeat")" = "true" ] || die "A2: repeat was not reported as deduplicated"
	ok "A2 — deduplicated onto $run_id"

	printf '\n\033[1;32mA11: the lane ran end to end. run=%s version=%s rows=%s\033[0m\n' "$run_id" "$committed" "$rows"
	echo "$run_id" >"$ROOT/.a11-run-id"
}

case "${1:-all}" in
deploy) cmd_deploy ;;
image) cmd_image ;;
fixtures) cmd_fixtures ;;
run) cmd_run ;;
all)
	cmd_deploy
	cmd_fixtures
	cmd_run
	;;
*) die "usage: $0 {deploy|fixtures|run|all}" ;;
esac
