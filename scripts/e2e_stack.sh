#!/usr/bin/env bash
# The e2e stack + suite runner — the SINGLE definition used by BOTH `make e2e-ci` and the CI job,
# so "it passed in CI" and "it passed on my machine" cannot diverge (the repo's dagger/auth-e2e pattern).
#
# WHY THIS EXISTS (docs/DECISIONS.md P0.1): every "live-verified" claim in this repo rested on a
# MANUAL terminal run while `ci.yml` ran `pytest -m "not e2e"`. Nothing guarded them, so a regression
# tomorrow would be silent. This script makes the live proof an artifact that runs on every push.
#
# It brings up the GOVERNED stack (Dex OIDC + OpenFGA + Dapr + RustFS + AGE) on kind with the feature
# flags the suites need, seeds the exact grants/buckets they assume, then runs the five e2e suites the
# goal names: CAS, client-direct (#2), warehouses (#3-A), multibase (#3-B), outbox (#4).
#
# Heavy optional components (observability/Vector/Perses/Greptime, web, compaction) are DISABLED — they
# are not under test here and a GitHub runner is 2 cores / 7 GB. `make e2e-obs` covers observability.
#
# Idempotent: safe to re-run against an existing cluster. Env overrides: CLUSTER, RELEASE, KEEP_STACK=1.
set -euo pipefail

CLUSTER="${CLUSTER:-lance}"
RELEASE="${RELEASE:-lance-ns}"
CATALOG_IMG="${CATALOG_IMG:-lance-rest-catalog:dev}"
BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.localbin"
export PATH="$BIN:$PATH"

# The two data-distribution buckets #3-B spreads fragments across (must be allowlisted in the chart AND
# physically provisioned — the allowlist is a governance gate, not a provisioner).
BASE_A="s3://mb-a"
BASE_B="s3://mb-b"

PF_PIDS=()
cleanup() {
  local rc=$?
  for pid in "${PF_PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  if [ $rc -ne 0 ]; then
    echo "::group::stack diagnostics (failure)"
    kubectl get pods -o wide || true
    # Dump EVERY not-ready pod, not a hardcoded list. The hardcoded catalog+lineage dump is why two 12-minute
    # CI round-trips told us nothing: the pod actually blocking the stack was OpenFGA, and we never looked at
    # it. A diagnostic that only reports the components you already suspected cannot find a surprise.
    for p in $(kubectl get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{"\n"}{end}' \
                 | awk '$2!="Running" && $2!="Succeeded" {print $1}'); do
      echo "--- NOT READY: $p"
      kubectl describe pod "$p" 2>/dev/null | sed -n '/Events:/,$p' | head -15 || true
    done
    for p in $(kubectl get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'); do
      ready=$(kubectl get pod "$p" -o jsonpath='{.status.containerStatuses[*].ready}' 2>/dev/null)
      case "$ready" in *false*)
        echo "--- LOGS (not-ready containers): $p"
        kubectl logs "$p" --all-containers --tail=40 2>/dev/null | head -40 || true ;;
      esac
    done
    kubectl get jobs -o wide || true   # the OpenFGA migration is a post-install HOOK job — surface its state
    echo "::endgroup::"
  fi
  if [ "${KEEP_STACK:-0}" != "1" ] && [ "${CI:-}" = "true" ]; then
    kind delete cluster --name "$CLUSTER" 2>/dev/null || true
  fi
  return $rc
}
trap cleanup EXIT

step() { echo; echo "==> $*"; }

# --------------------------------------------------------------------------------------------------
step "1/8 cluster + chart deps"
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || kind create cluster --config deploy/kind/kind-config.yaml --wait 180s
for r in dapr https://dapr.github.io/helm-charts/ nats https://nats-io.github.io/k8s/helm/charts/ \
         openfga https://openfga.github.io/helm-charts greptime https://greptimeteam.github.io/helm-charts/ \
         vector https://helm.vector.dev perses https://perses.github.io/helm-charts; do :; done
helm repo add dapr    https://dapr.github.io/helm-charts/            >/dev/null 2>&1 || true
helm repo add nats    https://nats-io.github.io/k8s/helm/charts/     >/dev/null 2>&1 || true
helm repo add openfga https://openfga.github.io/helm-charts          >/dev/null 2>&1 || true
helm repo add greptime https://greptimeteam.github.io/helm-charts/   >/dev/null 2>&1 || true
helm repo add vector  https://helm.vector.dev                        >/dev/null 2>&1 || true
helm repo add perses  https://perses.github.io/helm-charts           >/dev/null 2>&1 || true
helm repo update >/dev/null && helm dependency build ./chart >/dev/null

step "2/8 build + side-load the app image"
docker build -f .docker/rest-catalog.dockerfile -t "$CATALOG_IMG" . >/dev/null
# EXPLICIT load + digest check: the same-":dev"-tag gotcha (a rebuilt tag does NOT update a running pod
# under IfNotPresent) has bitten this repo repeatedly. Capture the freshly-built image id, load it, and
# verify the kind node's containerd actually holds that digest — a silent no-op load would otherwise
# serve a stale image and every "verified live" claim below would be against old code.
CATALOG_DIGEST="$(docker image inspect --format '{{.Id}}' "$CATALOG_IMG")"
kind load docker-image "$CATALOG_IMG" --name "$CLUSTER"
if ! docker exec "${CLUSTER}-control-plane" crictl images -o json 2>/dev/null \
  | grep -q "${CATALOG_DIGEST#sha256:}"; then
  echo "!! kind node does not hold the freshly-built catalog digest ${CATALOG_DIGEST} after load — aborting" >&2
  exit 1
fi
echo "   node holds catalog digest ${CATALOG_DIGEST}"

step "3/8 deploy the governed stack (auth ON, #3-A/#3-B/#4 flags ON, heavy extras OFF)"
# NO `--wait` — it DEADLOCKS on a fresh cluster, which is why this job never once passed in CI.
# helm's order is: apply manifests → (--wait) block until every resource is Ready → run post-install hooks.
# The OpenFGA schema migration IS a post-install hook, and the OpenFGA server cannot become Ready until its
# schema exists. So --wait blocks on OpenFGA, OpenFGA blocks on the migration, and the migration blocks on
# --wait. It "worked" on a long-lived local cluster only because a PREVIOUS install had already migrated the
# database, so the server came up Ready immediately and the deadlock never armed. Textbook works-on-my-machine.
# Dropping --wait lets the hook run; we then wait EXPLICITLY, below, for what the suites actually need.
helm upgrade --install "$RELEASE" ./chart --timeout 600s \
  --set auth.enabled=true \
  --set medallion.fgaEnabled=true \
  --set catalog.warehouses.enabled=true \
  --set-json "catalog.multibase.dataBases=[\"$BASE_A\",\"$BASE_B\"]" \
  --set services.lineage.outbox.enabled=true \
  --set services.lineage.reconcile.enabled=true \
  --set observability.enabled=false \
  --set compaction.enabled=false \
  --set frontend.enabled=false

# Dapr's sidecar injector is a MUTATING WEBHOOK: it injects daprd only into pods created AFTER it is Ready.
# The Dapr control plane is a SUBCHART of this same release, so on a fresh cluster the app pods are created
# in the same breath as the injector — they win the race, come up with NO sidecar (0/1, not 0/2), and then
# block forever on `TimeoutError: Dapr health check timed out`, taking every governed app into CrashLoop.
# On a long-lived local cluster Dapr was already installed and Ready, so the race never armed — the third
# fresh-cluster ordering bug in this script, and the reason "works locally" kept meaning nothing.
kubectl rollout status deploy/dapr-sidecar-injector --timeout=300s
# Recreate the app pods THROUGH the now-ready webhook so they actually receive their sidecar. Pod DELETE,
# not `rollout restart`: on a re-run against an existing cluster a rebuilt same-tag image only lands on a
# freshly-scheduled pod, and delete forces that reschedule to pull the just-loaded digest from the node
# cache (IfNotPresent) — the kind same-tag staleness fix (memory: kind-same-tag-image-gotcha). On a fresh
# cluster this is simply the sidecar-injection recreate.
for d in catalog lineage lance-ray raw-to-bronze bronze-to-silver silver-to-gold media-to-silver gateway; do
  kubectl delete pods -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=$d" \
    --ignore-not-found >/dev/null 2>&1 || true
done

# Explicit readiness, in dependency order. OpenFGA FIRST: its post-install migration must land before the
# server can serve, and every governed app fails closed without it — so if it is not up, everything
# downstream crash-loops and the real cause is buried under a pile of secondary failures.
kubectl rollout status deploy/"$RELEASE"-openfga --timeout=300s
kubectl rollout status deploy/"$RELEASE"-catalog --timeout=300s
kubectl rollout status deploy/"$RELEASE"-lineage --timeout=300s
kubectl rollout status deploy/"$RELEASE"-lance-ray --timeout=300s

# The pod that actually came up must be running the image we just loaded — the definitive proof the whole
# suite tests fresh code, not a stale same-tag image (the failure mode this guard exists for). NOTE: a
# pod's `.status imageID` is the containerd MANIFEST digest, while `docker image inspect .Id` is the CONFIG
# digest — different hashes for the SAME image. So we match the pod's digest against the FULL set crictl
# associates with the tag (id + repoDigests); since `kind load` replaces the tag, any of them == fresh.
NODE_SHAS="$(docker exec "${CLUSTER}-control-plane" crictl images -o json 2>/dev/null | python3 -c "
import sys, json
shas = []
for img in json.load(sys.stdin).get('images', []):
    if any('$CATALOG_IMG' in t for t in (img.get('repoTags') or [])):
        if img.get('id'): shas.append(img['id'].split(':')[-1])
        for rd in (img.get('repoDigests') or []): shas.append(rd.split(':')[-1])
print(' '.join(shas))
")"
RUNNING_ID="$(kubectl get pods -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=catalog" \
  -o jsonpath='{.items[0].status.containerStatuses[?(@.name=="catalog")].imageID}')"
POD_SHA="${RUNNING_ID##*:}"
case " $NODE_SHAS " in
  *" $POD_SHA "*) echo "   running catalog pod serves the freshly-loaded image ($POD_SHA)" ;;
  *) echo "!! running catalog pod imageID ($RUNNING_ID) is not a digest the node holds for $CATALOG_IMG ($NODE_SHAS) — stale, aborting" >&2
     exit 1 ;;
esac

step "4/8 port-forward the services the suites talk to"
kubectl port-forward "svc/$RELEASE-catalog" 2333:2333 >/tmp/pf-cat.log 2>&1 & PF_PIDS+=($!)
kubectl port-forward "svc/$RELEASE-lineage" 18000:8000 >/tmp/pf-lin.log 2>&1 & PF_PIDS+=($!)
kubectl port-forward "svc/$RELEASE-rustfs"  9900:9000 >/tmp/pf-rfs.log 2>&1 & PF_PIDS+=($!)
kubectl port-forward "svc/$RELEASE-dex"     5556:5556 >/tmp/pf-dex.log 2>&1 & PF_PIDS+=($!)
kubectl port-forward "svc/$RELEASE-openfga" 8081:8080 >/tmp/pf-fga.log 2>&1 & PF_PIDS+=($!)
# The AGE Postgres itself. The outbox-crash suite asserts RECOVERY at the source of truth (the run landed in
# the graph AND on the durable /events feed) — a non-zero `outbox_drained` only proves the relay counted it.
kubectl port-forward "svc/$RELEASE-age"     5433:5432 >/tmp/pf-age.log 2>&1 & PF_PIDS+=($!)
for i in $(seq 1 40); do
  c=$(curl -s -o /dev/null -w '%{http_code}' -m2 http://localhost:2333/livez 2>/dev/null || true)
  l=$(curl -s -o /dev/null -w '%{http_code}' -m2 http://localhost:18000/livez 2>/dev/null || true)
  d=$(curl -s -o /dev/null -w '%{http_code}' -m2 http://localhost:5556/dex/.well-known/openid-configuration 2>/dev/null || true)
  [ "$c" = "200" ] && [ "$l" = "200" ] && [ "$d" = "200" ] && break
  sleep 2
done
echo "   catalog=$c lineage=$l dex=$d"
[ "$c" = "200" ] && [ "$l" = "200" ] && [ "$d" = "200" ] || { echo "!! services never became ready"; exit 1; }

step "5/8 provision the #3-B data buckets (the allowlist GOVERNS, it does not provision)"
uv run python - <<PYEOF
import boto3
c = boto3.client("s3", endpoint_url="http://localhost:9900",
                 aws_access_key_id="rustfsadmin", aws_secret_access_key="rustfsadmin",
                 region_name="us-east-1")
for b in ("mb-a", "mb-b"):
    try:
        c.create_bucket(Bucket=b); print("   created", b)
    except Exception as e:
        print("   exists", b, type(e).__name__)
PYEOF

step "6/8 mint Dex tokens + seed the project-admin grant"
mint() {
  curl -s -m10 http://localhost:5556/dex/token \
    -d grant_type=password -d client_id=lance-catalog -d client_secret=lance-catalog-secret \
    -d scope="openid email" -d username="$1" -d password=password \
  | uv run python -c "import sys,json;print(json.load(sys.stdin).get('id_token',''))"
}
ALICE="$(mint alice@example.com)"
BOB="$(mint bob@example.com)"
[ -n "$ALICE" ] && [ -n "$BOB" ] || { echo "!! Dex issued no token"; exit 1; }

# The FGA subject is the token's `sub` (a Dex opaque id), NOT the email — granting the email silently
# authorizes nobody. Decode it.
SUB="$(ALICE="$ALICE" uv run python -c "
import os,base64,json
p = os.environ['ALICE'].split('.')[1]; p += '=' * (-len(p) % 4)
print(json.loads(base64.urlsafe_b64decode(p))['sub'])")"
# NEWEST store of that name — the same selector common.fga.provision uses (max created_at), so a boot-race
# double-create (two catalog pods provisioning against a freshly-rolled OpenFGA) can never make the seeder
# and the serving catalog disagree about which store the grants live in (CI flake 2026-07-15: the CLI
# verified can_create_warehouse=allowed on stores[0] while the catalog checked its own newer store → 403).
SID="$(fga store list --api-url http://localhost:8081 \
  | uv run python -c "import sys,json;s=[x for x in json.load(sys.stdin)['stores'] if x['name']=='lance-catalog'];print(max(s,key=lambda x:x['created_at'])['id'])")"
# project-admin => can_create_warehouse (the #3-A gate). bob gets NOTHING — he is the 403 leg.
fga tuple write --api-url http://localhost:8081 --store-id "$SID" "user:$SUB" admin project:acme >/dev/null
echo "   seeded user:${SUB:0:12}… admin project:acme (store ${SID:0:8}…)"

# WAIT until the grant is READABLE before running the suites. OpenFGA on Postgres is eventually consistent:
# `tuple write` returns BEFORE a subsequent `check` is guaranteed to see it, so the first warehouse-admin op
# (test_warehouses) can 403 on a fresh stack — a race that makes the job "green once, flaky forever", the
# exact trap this whole job exists to close. Poll the ACTUAL permission the test needs (can_create_warehouse
# on project:acme) until it reads allowed.
for i in $(seq 1 30); do
  allowed="$(fga query check --api-url http://localhost:8081 --store-id "$SID" \
    "user:$SUB" can_create_warehouse project:acme 2>/dev/null \
    | uv run python -c "import sys,json;print(json.load(sys.stdin).get('allowed', False))" 2>/dev/null || echo False)"
  [ "$allowed" = "True" ] && { echo "   grant is readable (can_create_warehouse=allowed)"; break; }
  [ "$i" = "30" ] && { echo "!! FGA grant never became readable — the seed did not propagate"; exit 1; }
  sleep 1
done

# The parent namespace the multibase suite creates tables under — alice must OWN it or the create 403s
# at the router before the allowlist check is ever reached.
curl -s -o /dev/null -X POST "http://localhost:2333/v1/namespace/mbns/create" -H "authorization: Bearer $ALICE"

step "7/8 the outbox suite's Dapr app token"
DAPR_TOKEN="$(kubectl get secret "$RELEASE-dapr-app-token" -o jsonpath='{.data.token}' | base64 -d)"
[ -n "$DAPR_TOKEN" ] || { echo "!! no dapr app token"; exit 1; }

step "8/8 run the five e2e suites against the live stack"
export LANCE_E2E_S3=http://localhost:9900
# The CAS suite reads LANCE_E2E_S3_ENDPOINT (a DIFFERENT name). It was never exported, so all 3 CAS tests
# skipped on every run — the suite named in the goal has, until now, never actually executed.
export LANCE_E2E_S3_ENDPOINT=http://localhost:9900
export LANCE_E2E_CATALOG_URL=http://localhost:2333
export LANCE_E2E_LINEAGE_URL=http://localhost:18000
# WITH the /dex path prefix — that is what every suite defaults to, and what Dex actually serves the OIDC
# discovery document under. Exporting the bare host OVERRODE the correct default with a broken one, so the
# client-direct suite's reachability probe 404'd and it SKIPPED — silently, on every run.
export LANCE_E2E_DEX=http://localhost:5556/dex
export LANCE_E2E_FGA=http://localhost:8081
export LANCE_E2E_TOKEN="$ALICE"
export LANCE_E2E_NONADMIN_TOKEN="$BOB"
export LANCE_E2E_DAPR_TOKEN="$DAPR_TOKEN"
export LANCE_E2E_PROJECT=acme
export LANCE_E2E_DELIM='$'
export LANCE_E2E_BASE_A="$BASE_A"
export LANCE_E2E_BASE_B="$BASE_B"
export LANCE_E2E_OUTBOX_URI="s3://lance-catalog/_lineage_outbox"
export LANCE_E2E_RECONCILE_BINDING=lineage-reconcile-cron
# The AGE Postgres, for the outbox-crash suite's recovery assertions (graph + /events feed at the source).
# User/db/password are READ FROM THE CLUSTER, never hardcoded — the password lives in the infra secret.
PG_USER=$(kubectl get sts "$RELEASE-age" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="POSTGRES_USER")].value}')
PG_DB=$(kubectl get sts "$RELEASE-age" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="POSTGRES_DB")].value}')
PG_PASS=$(kubectl get secret "$RELEASE-infra-credentials" -o jsonpath='{.data.postgres-password}' | base64 -d)
export LINEAGE_DATABASE_URL="postgresql://${PG_USER}:${PG_PASS}@localhost:5433/${PG_DB}"

PYTHONPATH=services uv run pytest \
  tests/e2e/test_object_store_cas_e2e.py \
  tests/e2e/test_client_direct_e2e.py \
  tests/e2e/test_warehouses_e2e.py \
  tests/e2e/test_multibase_e2e.py \
  tests/e2e/test_outbox_e2e.py \
  tests/e2e/test_outbox_crash_e2e.py \
  -v -rs -p no:cacheprovider | tee /tmp/e2e-stack.log

# NO SILENT SKIPS. Every suite above is skip-guarded on "is the stack reachable?" — which is the right
# behaviour on a laptop with nothing running, and a LIE here: the stack IS up, so a skip means an env var is
# misnamed and the suite silently did not run. That is not hypothetical. It is how the CAS suite (3 tests,
# wrong var name) and the client-direct suite (2 tests, Dex URL missing its /dex prefix) went COMPLETELY
# UNEXECUTED for the entire life of this job while it reported "6 passed" and a green check.
# A green tick over a suite that never ran is worse than a red one.
if grep -qE "[1-9][0-9]* skipped" /tmp/e2e-stack.log; then
  echo
  echo "!! FAIL: e2e suites SKIPPED against a LIVE stack — that is a misconfiguration, not 'not applicable':"
  grep -E "^SKIPPED|skipped" /tmp/e2e-stack.log || true
  exit 1
fi

# DuckDB external-consumer read (own step, separate log): proves DuckDB's core `lance` extension reads a
# pylance-8 dataset over this RustFS store. Kept OUT of the strict no-skip block on purpose — `INSTALL lance`
# fetches the extension from duckdb.org, so a transient CDN miss should skip (offline), not red the whole
# guarded suite; but a real READ failure returns non-zero and — under `set -o pipefail` — still fails the job.
PYTHONPATH=services uv run pytest \
  tests/e2e/test_duckdb_lance_e2e.py \
  -v -rs -p no:cacheprovider | tee /tmp/e2e-duckdb.log

# The two Ray-path suites (#53, test_ray_{train,batch}_e2e.py) are NOT run here on purpose. They need a
# real KubeRay cluster AND medallion.ray/compute flipped on — and flipping ray-on rolls the medallion
# movers into stage-compute-via-ray mode, which on a fresh CI stack races the OpenBao secret-store
# readiness and hangs the movers (secret 500 + "waiting on port 8000"). Forcing that into the shared,
# ray-OFF core-suite job destabilises the whole run for no isolation benefit. Those suites are proven
# live and kept green as dedicated targets — `make e2e-ray-train` / `make e2e-ray-batch` (deploy the ray
# cluster first with `make ray-demo`). A dedicated ray-enabled CI job is the right home and a scoped
# follow-up; wiring them into THIS job is a net regression.

# --- P5 chaos drill: prove a core-dependency OUTAGE fails CLOSED (never fail-open) and RECOVERS. The stack
# is auth-on, so this is the LIVE counterpart to the mocked OpenFGA-down unit test + the first test of the
# RustFS/S3 outage at all. Runs LAST (the suites already passed; the cluster is torn down after) and each
# probe RESTORES the dependency + verifies recovery, so it leaves the stack healthy. Fail-CLOSED assertions
# are STRICT (a 200 while a dep is down is a security/data hole → hard fail); recovery polls are GENEROUS (a
# slow reschedule must not false-red). Probes are CHILD namespace CREATEs under the alice-owned `mbns`
# parent (created above), NOT root creates: a child create runs the can_create_child FGA check on the parent
# THEN a genuine S3 write, so FGA-down → 503 AT THE CHECK before storage (a root create with lockRootCreate
# off would skip the FGA check entirely and only 500 later on the ownership-seed write — that would pass this
# drill even if the authz check itself fail-opened). RustFS-down → 5xx on the write. Both deterministic, no
# cache can mask them, no 409 on retry. E2E_CHAOS=0 disables it (iteration escape hatch).
if [ "${E2E_CHAOS:-1}" = "1" ]; then
  step "chaos: dependency-outage fail-closed drill (P5)"
  CAT=http://localhost:2333
  # mbns is alice's own namespace (created earlier), so can_create_child is authorized on the happy path;
  # the $-delimited id makes chaos-$1 a CHILD of mbns (not a root namespace).
  gov_create() { curl -s -o /dev/null -w '%{http_code}' -m15 -H "authorization: Bearer $ALICE" \
    -X POST "$CAT/v1/namespace/mbns\$chaos-$1/create"; }
  restore_dep() { kubectl scale "deploy/$1" --replicas=1 >/dev/null 2>&1 || true
                  kubectl rollout status "deploy/$1" --timeout=180s >/dev/null 2>&1 || true; }

  b=$(gov_create base); [ "$b" = "200" ] || { echo "!! chaos baseline create expected 200, got $b"; exit 1; }
  echo "   baseline governed create = 200 ✓"

  # 1. OpenFGA (authz chokepoint) down → the FGA check fails CLOSED (never fail-open 200).
  echo "   -- OpenFGA outage --"
  kubectl scale "deploy/$RELEASE-openfga" --replicas=0 >/dev/null
  n=0; down=""; for _ in $(seq 1 40); do n=$((n+1)); down=$(gov_create "f$n"); [ "$down" != "200" ] && break; sleep 2; done
  if [ "$down" = "200" ]; then restore_dep "$RELEASE-openfga"; echo "!! OpenFGA down but create STILL 200 — FAIL-OPEN (security hole)"; exit 1; fi
  echo "   OpenFGA down → governed create $down (fail-closed) ✓"
  restore_dep "$RELEASE-openfga"
  up=""; for _ in $(seq 1 40); do up=$(gov_create "fr$RANDOM"); [ "$up" = "200" ] && break; sleep 3; done
  [ "$up" = "200" ] || { echo "!! create did not recover after OpenFGA restored (got $up)"; exit 1; }
  echo "   OpenFGA restored → governed create 200 (recovered) ✓"

  # 2. RustFS (data plane) down → the S3 write fails CLOSED (a write with no object store would be data loss).
  echo "   -- RustFS outage --"
  kubectl scale "deploy/$RELEASE-rustfs" --replicas=0 >/dev/null
  n=0; down=""; for _ in $(seq 1 40); do n=$((n+1)); down=$(gov_create "s$n"); [ "$down" != "200" ] && break; sleep 2; done
  if [ "$down" = "200" ]; then restore_dep "$RELEASE-rustfs"; echo "!! RustFS down but create STILL 200 — a write with no object store is data loss"; exit 1; fi
  echo "   RustFS down → governed create $down (fail-closed) ✓"
  restore_dep "$RELEASE-rustfs"
  up=""; for _ in $(seq 1 50); do up=$(gov_create "sr$RANDOM"); [ "$up" = "200" ] && break; sleep 3; done
  [ "$up" = "200" ] || { echo "!! create did not recover after RustFS restored (got $up)"; exit 1; }
  echo "   RustFS restored → governed create 200 (recovered) ✓"
  echo "   ✓ chaos drill: both dependency outages fail closed + recover"
fi

# --- P4 restore drill: PROVE the AGE backup is restorable. docs/RUNBOOK-restore.md warns that a plain
# pg_dump of an Apache AGE database can restore the ag_catalog metadata but LOSE the graph's labels/data.
# Run the drill INSIDE the age-postgres pod (it has psql/pg_dump) on a throwaway DB, so it never touches the
# real lineage graph. A red here is a genuine finding (switch backup-pg.yaml to an AGE-aware dump), not a
# flake — E2E_RESTORE_DRILL=0 disables it while that follow-up is built. ---------------------------------
if [ "${E2E_RESTORE_DRILL:-1}" = "1" ]; then
  step "restore drill: AGE pg_dump → restore round-trips the graph (P4)"
  # The AGE StatefulSet ($RELEASE-age, 1 replica) → pod $RELEASE-age-0 deterministically (StatefulSet naming),
  # more robust than a label guess. Wait for it to be Ready (the suites already used it, so it is).
  AGE_POD="$RELEASE-age-0"
  kubectl wait --for=condition=ready "pod/$AGE_POD" --timeout=60s >/dev/null 2>&1 \
    || { echo "!! $AGE_POD not ready for the restore drill"; kubectl get pods | grep age || true; exit 1; }
  kubectl cp scripts/age_restore_drill.sh "$AGE_POD:/tmp/age_restore_drill.sh"
  kubectl exec "$AGE_POD" -- bash /tmp/age_restore_drill.sh
  echo "   ✓ restore drill passed — RUNBOOK-restore.md is proven, not just authored"
fi

echo
echo "✓ e2e stack suite green — the live proof is now an artifact, not an anecdote"
