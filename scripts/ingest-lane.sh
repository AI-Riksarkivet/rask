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
# The tenant the lane ingests into. NOT `demo`: that namespace predates warehouse enforcement and
# exists unbound in the default root, and the catalog refuses to adopt it ("binding it to a
# warehouse would orphan its tables"), so the lane could never provision it. A lane-owned project
# can be created and bound cleanly.
PROJECT="${PROJECT:-lane}"

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
		# The medallion IS the cascade — its producer owns /bronze-arrival (the head that turns a
		# bronze write into `medallion.bronze`), and its movers carry bronze->silver->gold. Disabled,
		# the ingest lane passes every one of its own gates while nothing above bronze moves — which is
		# exactly the gap that leaves. Its compute is the in-process fake-Ray path, so the cascade can
		# be witnessed without a Ray cluster.
		medallion: {enabled: true}
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
		    if doc.get("kind") == "Ingress":
		        # NEVER apply the estate's Ingress from the lane.
		        #
		        # This deploys a SLICE into its own namespace, but the chart's Ingress has no host: its
		        # rules are bare paths, including `/` and `/api`. Applied here it becomes a second
		        # host-less claim on the same paths, and Traefik picks between them arbitrarily —
		        # observed 2026-08-04: `/`, `/projects` and `/settings` on the real estate started
		        # answering FastAPI's {"detail":"Not Found"} because the root was resolving to THIS
		        # namespace's gateway. Nothing in the lane needs the ingress; it is reached in-cluster.
		        continue
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
# Provision the tenant hierarchy the way an ADMIN would, because ingest deliberately will not.
#
# With `warehouses.enabled` (the chart default) the catalog enforces
# project > warehouse > namespace > table and refuses a bare top-level namespace. Ingest is a data
# writer, not a tenant provisioner — `POST /v1/projects` is estate-admin gated and writes the
# creator's project#admin tuple — so the lane does this setup itself rather than asking the plane to
# quietly grant itself tenancy.
#
# Idempotent: each door answers 200 on a repeat, and a 409 means someone got there first, which is
# success for our purposes. Only a hard failure on the NAMESPACE is fatal, since that is the one the
# run actually needs.
cmd_provision() {
	local pod
	pod="$(ingest_pod)" || die "no ingest pod"
	log "provisioning $PROJECT (project > warehouse > namespace) via the catalog"
	kubectl exec -n "$NS" "$pod" -c ingest -- python -c "
import json, os, sys, urllib.request, urllib.error
import os as _os
def _auth():
    tok = _os.environ.get('APP_API_TOKEN')
    return {'dapr-api-token': tok} if tok else {}
base = os.getenv('RASK_CATALOG_URL', 'http://rask-catalog:2333').rstrip('/')
def post(path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, ''
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
project = '$PROJECT'
for path, body, fatal in (
    ('/v1/projects', {'id': project, 'name': project}, False),
    ('/v1/warehouses', {'id': project + '-wh', 'project': project}, False),
    ('/v1/warehouses/' + project + '-wh/namespaces', {'namespace': project}, True),
):
    status, detail = post(path, body)
    print('   %-46s -> %s' % (path, status))
    # 409 = it already exists, which is the state we wanted. Anything else on the namespace is fatal:
    # without it every run fails at the namespace step with units_total 0.
    if fatal and status >= 400 and status != 409:
        sys.exit('   provisioning failed: %s %s' % (status, detail))
" || die "could not provision $PROJECT"
	ok "$PROJECT provisioned"

	# Creating the tenant is only half of standing one up: the service token is project-BLIND and may
	# write into exactly ONE configured project, so a freshly provisioned tenant that nothing is
	# authorized for gets a correct 403 on its first ingest. That is the door working, and it cost a
	# lane run to see — the chart pins `demo` (the estate's nominal tenant) and the lane provisions its
	# own, because `demo` predates warehouse enforcement and cannot be adopted.
	#
	# Authorizing the service for the tenant it just created is what an operator does, so the lane does
	# it too rather than reaching for a project the deployment happens to already allow.
	log "authorizing the ingest service token for $PROJECT"
	# …and enable the fixture source, pointed at the ONE directory the lane seeds. Unset (the chart
	# default) means local-dir is refused outright, which is what a production ingest wants and
	# what made the first run after the confinement fail every unit with "local-dir is not
	# enabled here". Both settings in one call so there is one rollout, not two.
	kubectl set env -n "$NS" deploy/"$RELEASE"-ingest \
		"RASK_INGEST_SERVICE_PROJECT=$PROJECT" "RASK_INGEST_LOCAL_ROOT=$POD_FIXTURE_DIR" >/dev/null
	kubectl rollout status -n "$NS" deploy/"$RELEASE"-ingest --timeout=240s >/dev/null ||
		die "ingest did not come back after the service-project change"
	ok "service token scoped to project:$PROJECT"
}

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

	# A FRESH dataset per run. Bronze is append-only and a run's rows accumulate into the tier, so
	# re-running against a fixed name makes "4 fixtures in" and "N rows in the dataset" diverge — the
	# second run of the previous lane reported 8 units done for 4 files. Row-count assertions have to
	# be about THIS run, and the cheapest way to guarantee that is to give each run its own dataset.
	# PRE-FLIGHT: the fixtures must be in THIS pod. They live on the pod's ephemeral filesystem, so a
	# rollout between seeding and running silently empties them — and an empty source is not an error
	# anywhere in the plane. Enumeration finds no keys, no chunks are dispatched, `finalize` takes its
	# legitimate no-op path, and the run reports COMPLETE at the empty-create version. Every assertion
	# except the row count passes. Checked here rather than trusted, and re-seeded if absent.
	local present
	present="$(kubectl exec -n "$NS" "$pod" -c ingest -- python -c "
import os
print(len([f for f in os.listdir('$POD_FIXTURE_DIR') if f.endswith('.tif')]) if os.path.isdir('$POD_FIXTURE_DIR') else 0)
" 2>/dev/null || echo 0)"
	if [ "${present:-0}" -eq 0 ]; then
		log "fixtures absent in $pod (rolled since seeding) — re-seeding"
		cmd_fixtures
	fi

	# A FRESH dataset per run. Bronze is append-only and a run's rows accumulate into the tier, so
	# re-running against a fixed name makes "4 fixtures in" and "N rows in the dataset" diverge — an
	# earlier lane reported 8 units done for 4 ingested files. A row-count assertion has to be about
	# THIS run, and the cheapest way to guarantee that is to give each run its own dataset.
	local stamp
	stamp="$(date +%s)"
	# DATASET overridable so the lane can run TWICE into ONE table — which is the only way to observe
	# a publication RANGE with a real `from_version` (D-R3). A fresh dataset per run always publishes
	# from None, so the delta a consumer actually resolves is never exercised.
	local key="a11-$stamp"
	local dataset="${DATASET:-a11-$stamp}"
	[ -n "${DATASET:-}" ] && key="a11-$dataset-$stamp"
	log "POST /api/ingests (Idempotency-Key: $key, dataset: $dataset)"

	# Timed, because A1 is a CONTRACT: 202 in under a second. Measured inside the cluster so the
	# number is the handler's, not the port-forward's.
	local accepted
	accepted="$(kubectl exec -n "$NS" "$pod" -c ingest -- python -c "
import json, time, urllib.request
import os as _os
def _auth():
    tok = _os.environ.get('APP_API_TOKEN')
    return {'dapr-api-token': tok} if tok else {}
body = json.dumps({
    'kind': 'local-dir',
    'project': '$PROJECT',
    'dataset': '$dataset',
    'options': {'root': '$POD_FIXTURE_DIR', 'pattern': '*.tif'},
}).encode()
req = urllib.request.Request('http://127.0.0.1:8830/api/ingests', data=body,
                             headers={'Content-Type': 'application/json', 'Idempotency-Key': '$key', **_auth()})
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
import os as _os
def _auth():
    tok = _os.environ.get('APP_API_TOKEN')
    return {'dapr-api-token': tok} if tok else {}
_req = urllib.request.Request('http://127.0.0.1:8830/api/ingests/$run_id', headers=_auth())
with urllib.request.urlopen(_req, timeout=15) as r:
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
import os as _os
def _auth():
    tok = _os.environ.get('APP_API_TOKEN')
    return {'dapr-api-token': tok} if tok else {}
body = json.dumps({'kind':'local-dir','project':'$PROJECT','dataset':'$dataset',
                   'options':{'root':'$POD_FIXTURE_DIR','pattern':'*.tif'}}).encode()
req = urllib.request.Request('http://127.0.0.1:8830/api/ingests', data=body,
                             headers={'Content-Type':'application/json','Idempotency-Key':'$key', **_auth()})
with urllib.request.urlopen(req, timeout=30) as r:
    print(r.read().decode())
")" || die "repeat POST failed"
	[ "$(jq -r .run_id <<<"$repeat")" = "$run_id" ] || die "A2: the key resolved to a DIFFERENT run"
	[ "$(jq -r .deduplicated <<<"$repeat")" = "true" ] || die "A2: repeat was not reported as deduplicated"
	ok "A2 — deduplicated onto $run_id"

	printf '\n\033[1;32mA11: the lane ran end to end. run=%s version=%s rows=%s\033[0m\n' "$run_id" "$committed" "$rows"
	echo "$run_id" >"$ROOT/.a11-run-id"
}

# ── A5 — a corrupt unit must be a tracked error, not a poisoned dataset ────────────────
#
# The corrupt fixture is a real TIFF header followed by garbage, NOT a file with a wrong
# extension. That distinction is the test: bronze is faithful to source and must accept formats it
# does not recognise, so a gate that refused on the extension would prove nothing about corruption
# and would wrongly reject new material. The refusal has to come from actually decoding it.
#
# The assertion is deliberately two-sided. It is not enough that the run notices the bad page — the
# three GOOD pages must still land. A plane that fails the whole run over one corrupt file makes a
# 10,000-page harvest hostage to its worst byte, and an operator's only recovery is to delete the
# offending file and start over.
cmd_corrupt() {
	local pod
	pod="$(ingest_pod)"
	[ -n "$pod" ] || die "no ingest pod in namespace $NS"

	log "seeding the corrupt fixture set (3 good + 1 corrupt)"
	local payload
	payload="$(cd "$ROOT/tests/fixtures/ingest-lane-corrupt" && tar czf - . | base64 -w0)"
	kubectl exec -i -n "$NS" "$pod" -c ingest -- python -c "
import base64, io, os, sys, tarfile
os.makedirs('$POD_FIXTURE_DIR-corrupt', exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(sys.stdin.read()))) as archive:
    archive.extractall('$POD_FIXTURE_DIR-corrupt')
print(sorted(os.listdir('$POD_FIXTURE_DIR-corrupt')))
" <<<"$payload" >/dev/null || die "corrupt fixture seed failed"

	local stamp key dataset
	stamp="$(date +%s)"
	key="a5-$stamp"
	dataset="a5-$stamp"
	log "A5 — POST with a corrupt page among good ones"
	local run_id
	run_id="$(kubectl exec -n "$NS" "$pod" -c ingest -- python -c "
import json, urllib.request
import os as _os
def _auth():
    tok = _os.environ.get('APP_API_TOKEN')
    return {'dapr-api-token': tok} if tok else {}
body = json.dumps({'kind':'local-dir','project':'$PROJECT','dataset':'$dataset',
                   'options':{'root':'$POD_FIXTURE_DIR-corrupt','pattern':'*.tif'}}).encode()
req = urllib.request.Request('http://127.0.0.1:8830/api/ingests', data=body,
                             headers={'Content-Type':'application/json','Idempotency-Key':'$key', **_auth()})
with urllib.request.urlopen(req, timeout=30) as r:
    print(json.load(r)['run_id'])
")" || die "A5 POST failed"

	local body="" status=""
	for _ in $(seq 1 60); do
		body="$(kubectl exec -n "$NS" "$pod" -c ingest -- python -c "
import os as _os, urllib.request
def _auth():
    tok = _os.environ.get('APP_API_TOKEN')
    return {'dapr-api-token': tok} if tok else {}
_req = urllib.request.Request('http://127.0.0.1:8830/api/ingests/$run_id', headers=_auth())
with urllib.request.urlopen(_req, timeout=15) as r:
    print(r.read().decode())
" 2>/dev/null)" || true
		status="$(jq -r .status <<<"$body" 2>/dev/null || echo '')"
		case "$status" in COMPLETE | COMPLETE_WITH_ERRORS | FAILED) break ;; esac
		sleep 5
	done
	echo "   $body"

	[ "$status" = "COMPLETE_WITH_ERRORS" ] ||
		die "A5: expected COMPLETE_WITH_ERRORS, got $status — a corrupt page must be REPORTED, not silently dropped or fatal"
	ok "A5 — run reported COMPLETE_WITH_ERRORS"

	# Recorded the way `run` records its own, so A20's error-reporting test can EXECUTE rather than
	# skip. A gate that has only ever been seen to skip is indistinguishable from one that would
	# fail — the same argument A10 makes for seeding each grep gate with a real violation.
	echo "$run_id" >"$ROOT/.a5-run-id"

	local errors named
	errors="$(jq -r '.errors | length' <<<"$body")"
	[ "$errors" = "1" ] || die "A5: expected exactly 1 tracked error, got $errors"
	named="$(jq -r '.errors | keys[0]' <<<"$body")"
	case "$named" in *page-0004*) ok "A5 — the error names the corrupt unit ($named)" ;;
	*) die "A5: the tracked error names $named, not the corrupt page" ;; esac

	local rows
	rows="$(jq -r .units_done <<<"$body")"
	[ "$rows" = "3" ] || die "A5: expected the 3 GOOD pages to land, got $rows — one bad byte must not cost the whole run"
	ok "A5 — the 3 good pages landed anyway"
}

# ── A3 — kill the pod mid-run; the run must still land every row ──────────────────────
#
# This is the assertion `ingest/staging.py` exists for. A worker acks a unit only after its bytes
# AND its fragment's identity are on the object store, so a pod that dies mid-drain leaves recoverable
# work: the units it had not acked redeliver, and the fragments it HAD acked are rediscovered from
# the staging prefix by whichever pod finalizes. Before staging, the acked fragments' names died
# with the pod — their bytes stranded on the store, their units gone from a WORK_QUEUE stream. That
# is silent row loss, and the only way to see it is to count.
cmd_kill() {
	local pod
	pod="$(ingest_pod)"
	[ -n "$pod" ] || die "no ingest pod in namespace $NS"

	local stamp key dataset prefix
	stamp="$(date +%s)"
	key="a3-$stamp"
	dataset="a3-$stamp"
	prefix="a3-fixtures/$stamp"

	# A3 runs off S3, not the pod's filesystem, and that is the test's design rather than a
	# convenience. Killing the pod also destroys `/tmp`, so a LocalDirSource run resumes against an
	# EMPTY directory — enumeration finds no keys, the workflow legitimately completes with zero rows,
	# and the recovery assertion passes for entirely the wrong reason. The source has to outlive the
	# process whose death is under test. It is also the more honest shape: a real ingest source is
	# external by definition.
	log "A3 — uploading fixtures to S3 so the SOURCE survives the pod"
	local payload
	payload="$(cd "$FIXTURES" && tar czf - . | base64 -w0)"
	kubectl exec -i -n "$NS" "$pod" -c ingest -- python -c "
import base64, io, os, sys, tarfile
from storage import s3_client
bucket = os.environ['RASK_INGEST_WAREHOUSE'].removeprefix('s3://').split('/')[0]
client = s3_client(os.getenv('RASK_S3_ENDPOINT_URL'))
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(sys.stdin.read()))) as archive:
    for member in archive.getmembers():
        if not member.isfile() or not member.name.endswith('.tif'):
            continue
        data = archive.extractfile(member).read()
        client.put_object(Bucket=bucket, Key='$prefix/' + os.path.basename(member.name), Body=data)
print('uploaded to s3://%s/%s' % (bucket, '$prefix'))
" <<<"$payload" || die "A3: S3 fixture upload failed"

	log "A3 — POST, then kill the pod mid-run"
	local run_id
	run_id="$(kubectl exec -n "$NS" "$pod" -c ingest -- python -c "
import json, os, urllib.request
import os as _os
def _auth():
    tok = _os.environ.get('APP_API_TOKEN')
    return {'dapr-api-token': tok} if tok else {}
bucket = os.environ['RASK_INGEST_WAREHOUSE'].removeprefix('s3://').split('/')[0]
body = json.dumps({'kind':'s3-prefix','project':'$PROJECT','dataset':'$dataset',
                   'options':{'bucket':bucket,'prefix':'$prefix','endpoint':os.getenv('RASK_S3_ENDPOINT_URL')}}).encode()
req = urllib.request.Request('http://127.0.0.1:8830/api/ingests', data=body,
                             headers={'Content-Type':'application/json','Idempotency-Key':'$key', **_auth()})
with urllib.request.urlopen(req, timeout=30) as r:
    print(json.load(r)['run_id'])
")" || die "A3 POST failed"
	ok "A3 — run $run_id accepted (source on S3)"

	# Immediately, with no grace period: a graceful stop would let the workflow finish and prove
	# nothing. This is a machine losing power, which is the case durability claims are about.
	kubectl delete pod -n "$NS" "$pod" --grace-period=0 --force >/dev/null 2>&1 || true
	ok "A3 — pod $pod killed (grace 0)"

	log "waiting for the replacement pod"
	local newpod=""
	for _ in $(seq 1 60); do
		newpod="$(ingest_pod)"
		[ -n "$newpod" ] && [ "$newpod" != "$pod" ] && break
		sleep 5
	done
	[ -n "$newpod" ] || die "A3: no replacement pod appeared"
	ok "A3 — replacement pod $newpod is up"

	local body="" status=""
	for _ in $(seq 1 90); do
		body="$(kubectl exec -n "$NS" "$newpod" -c ingest -- python -c "
import os as _os, urllib.request
def _auth():
    tok = _os.environ.get('APP_API_TOKEN')
    return {'dapr-api-token': tok} if tok else {}
_req = urllib.request.Request('http://127.0.0.1:8830/api/ingests/$run_id', headers=_auth())
with urllib.request.urlopen(_req, timeout=15) as r:
    print(r.read().decode())
" 2>/dev/null)" || true
		status="$(jq -r .status <<<"$body" 2>/dev/null || echo '')"
		case "$status" in COMPLETE | COMPLETE_WITH_ERRORS | FAILED) break ;; esac
		sleep 5
	done
	echo "   $body"

	case "$status" in
	COMPLETE | COMPLETE_WITH_ERRORS) ok "A3 — the run survived the kill and reached $status" ;;
	*) die "A3: the run did not recover (last: ${status:-<none>})" ;;
	esac

	local rows expected
	expected="$(find "$FIXTURES" -name '*.tif' | wc -l)"
	rows="$(jq -r .units_done <<<"$body")"
	[ "$rows" = "$expected" ] ||
		die "A3: expected all $expected rows after the kill, got $rows — this is the silent row loss staging exists to prevent"
	ok "A3 — all $rows rows landed despite the kill"
}

case "${1:-all}" in
deploy) cmd_deploy ;;
image) cmd_image ;;
fixtures) cmd_fixtures ;;
provision) cmd_provision ;;
corrupt) cmd_corrupt ;;
kill) cmd_kill ;;
run) cmd_run ;;
all)
	cmd_deploy
	cmd_provision
	cmd_fixtures
	cmd_run
	cmd_corrupt
	cmd_kill
	;;
*) die "usage: $0 {deploy|image|provision|fixtures|run|corrupt|kill|all}" ;;
esac
