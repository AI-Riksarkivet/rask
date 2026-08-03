#!/usr/bin/env bash
# Seed the dev estate so every surface SHOWS something: media corpus, RustFS + a governed
# lakehouse table, and a labeling task with items.
#
# WHY THIS EXISTS. A fresh install serves an EMPTY corpus (`explorer.corpus.mode` defaults to
# `emptyDir`), so `/media` finds nothing, `/annotator` has no page to draw on and `/lakehouse`
# lists no tables — and a developer cannot tell "not built" from "not seeded". Two seed scripts
# existed but were wired to no command and documented in no target, so nobody ran them.
#
#   make seed-dev            # against the current kube context
#   RASK_MEDIA_CORPUS=/data/corpus make seed-dev
#
# THE CORPUS IS SEEDED IN-CLUSTER, BY THE SERVING IMAGE, ON PURPOSE. Writing it from the host
# leaves files the services cannot read: they run as uid 10001, a host seed writes 0600, and Lance
# reports the resulting EACCES as **"Object at location …/tokens.lance not found"** — a not-found
# error for a file that is present, which reads exactly like a corrupt or version-skewed index.
# The seeder chmods what it writes for the same reason; the Job runs as the corpus owner so it may
# rewrite an existing fixture.
set -euo pipefail

NS="${RASK_NAMESPACE:-default}"
CORPUS_HOST_PATH="${RASK_MEDIA_CORPUS:-/home/blackwell/media-corpus}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

say "1/4 media corpus (in-cluster, serving image)"
IMAGE="$(kubectl get deployment rask-search -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].image}')"
# SELF-CONFIGURING: seed the dataset the DEPLOYMENT asks for. The chart points MEDIA_DB at
# `<root>/transcripts_v2.lance`, so a fixture named anything else leaves every media surface saying
# "dataset 'transcripts_v2' not found under /media-corpus" with the corpus sitting right there —
# the exact symptom that makes a seeded estate look broken. Reading it from the live deployment
# means the fixture can never drift from the config that reads it.
MEDIA_DB="$(kubectl get deployment rask-viewer -n "$NS" -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' | sed -n 's/^MEDIA_DB=//p')"
if [ -z "${RASK_SEED_DATASET:-}" ] && [ -z "$MEDIA_DB" ]; then
  echo "!! rask-viewer declares no MEDIA_DB — cannot know which dataset to seed." >&2
  echo "   Set RASK_SEED_DATASET explicitly, or fix the deployment. Falling back to a guess is the" >&2
  echo "   exact drift this reads the deployment to avoid." >&2
  exit 1
fi
DATASET_ID="${RASK_SEED_DATASET:-$(basename "$MEDIA_DB" .lance)}"
echo "  dataset id from the deployment: $DATASET_ID"

# The SAME lesson, applied to the path: the chart's default corpus mode is emptyDir, and the only
# thing in the repo that sets hostPath is the Tiltfile. Writing a perfect fixture into a directory
# no pod mounts is a silent no-op that ends with this script printing "seeded" — so ask the
# deployment what it actually mounts, and refuse when it disagrees with where we are about to write.
MOUNTED_HOST_PATH="$(kubectl get deployment rask-viewer -n "$NS" -o jsonpath='{range .spec.template.spec.volumes[*]}{.hostPath.path}{"\n"}{end}' | sed '/^$/d' | head -1)"
if [ -z "$MOUNTED_HOST_PATH" ]; then
  echo "!! rask-viewer mounts NO hostPath for its corpus (chart default is explorer.corpus.mode=emptyDir)." >&2
  echo "   Anything seeded to $CORPUS_HOST_PATH would be invisible to every service." >&2
  echo "   Install with --set explorer.corpus.mode=hostPath --set explorer.corpus.hostPath=$CORPUS_HOST_PATH" >&2
  exit 1
fi
if [ "$MOUNTED_HOST_PATH" != "$CORPUS_HOST_PATH" ]; then
  echo "!! the release mounts $MOUNTED_HOST_PATH but this run would seed $CORPUS_HOST_PATH." >&2
  echo "   RASK_MEDIA_CORPUS moves the SEEDER, not the chart. Re-run with RASK_MEDIA_CORPUS=$MOUNTED_HOST_PATH" >&2
  echo "   or reinstall the release with explorer.corpus.hostPath=$CORPUS_HOST_PATH." >&2
  exit 1
fi
echo "  corpus hostPath confirmed mounted by rask-viewer: $MOUNTED_HOST_PATH"
kubectl delete job rask-seed-corpus -n "$NS" --ignore-not-found >/dev/null 2>&1 || true
kubectl create configmap rask-seed-corpus -n "$NS" \
  --from-file=seed_demo_corpus.py="$REPO_ROOT/scripts/seed_demo_corpus.py" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl apply -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata: { name: rask-seed-corpus, namespace: $NS }
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      # As the corpus OWNER: the hostPath belongs to the developer, and the default pod uid
      # (10001) cannot rewrite an existing fixture. The seeder then chmods it readable for 10001.
      securityContext: { runAsUser: $(id -u), runAsGroup: $(id -g), fsGroup: $(id -g) }
      containers:
        - name: seed
          image: $IMAGE
          command: ["python", "/seed/seed_demo_corpus.py", "/media-corpus"]
          env:
            - { name: SEED_DATASET_ID, value: "$DATASET_ID" }
          volumeMounts:
            - { name: corpus, mountPath: /media-corpus }
            - { name: seed, mountPath: /seed }
      volumes:
        - name: corpus
          hostPath: { path: $CORPUS_HOST_PATH, type: DirectoryOrCreate }
        - name: seed
          configMap: { name: rask-seed-corpus }
EOF
# Wait on BOTH terminal conditions. `--for=condition=complete` alone blocks the full timeout on a
# job that already Failed and then dies under `set -e` — five minutes of silence, and the one line
# naming the cause (PermissionError, bad descriptor) never printed because the logs command is next.
for _ in $(seq 1 150); do
  SEED_JOB_STATE="$(kubectl get job rask-seed-corpus -n "$NS" -o jsonpath='{.status.conditions[?(@.status=="True")].type}' 2>/dev/null || true)"
  # SUBSTRING match, not prefix: kubectl 1.31+ reports every True condition, so a healthy job reads
  # "SuccessCriteriaMet Complete" — a `Complete*` prefix test calls that a failure.
  case "$SEED_JOB_STATE" in *Failed*|*Complete*) break ;; esac
  sleep 2
done
kubectl logs -n "$NS" job/rask-seed-corpus 2>/dev/null | grep -vE '^\[' || true
case "$SEED_JOB_STATE" in
  *Failed*) echo "!! corpus seed job FAILED — logs above" >&2; exit 1 ;;
  *Complete*) ;;
  *) echo "!! corpus seed job did not reach a terminal condition (saw: ${SEED_JOB_STATE:-none}) — logs above" >&2; exit 1 ;;
esac

say "2/4 restart the media trio so it re-opens the corpus"
# The registry caches dataset handles, so a reseed is invisible until the readers reopen.
kubectl rollout restart deployment/rask-search deployment/rask-viewer deployment/rask-annotator -n "$NS" >/dev/null
for d in rask-search rask-viewer rask-annotator; do
  kubectl rollout status "deployment/$d" -n "$NS" --timeout=240s | tail -1
done

say "3/4 lakehouse: real pages into RustFS, REGISTERED in the catalog"
# The governed half: bytes into the warehouse bucket AND a catalog table that owns them. Writing
# without registering is the bug this script's predecessor had — data inside a governed lakehouse
# that the catalog knows nothing about. Skipped (loudly, never silently) when the source is
# unreachable, because a seed that dies on someone's offline laptop is a seed nobody runs.
if [ "${RASK_SEED_SKIP_LAKEHOUSE:-0}" = "1" ]; then
  echo "  skipped (RASK_SEED_SKIP_LAKEHOUSE=1)"
else
  kubectl port-forward -n "$NS" svc/rask-rustfs-io 19900:9000 >/dev/null 2>&1 &
  RUSTFS_PID=$!
  kubectl port-forward -n "$NS" svc/rask-catalog 12433:2333 >/dev/null 2>&1 &
  CATALOG_PID=$!
  kubectl port-forward -n "$NS" svc/rask-dex 12434:5556 >/dev/null 2>&1 &
  DEX_PID=$!
  # A trap, not a trailing kill: any failure between here and there used to leave three
  # port-forwards holding 19900/12433/12434, and the next run's forwards then bind nothing.
  trap 'kill "$RUSTFS_PID" "$CATALOG_PID" "$DEX_PID" 2>/dev/null || true' EXIT INT TERM
  # Poll the forwards instead of sleeping at them — a fixed sleep is a race on a loaded machine.
  for _ in $(seq 1 30); do
    (exec 3<>/dev/tcp/127.0.0.1/12433) 2>/dev/null && exec 3<&- && break
    sleep 1
  done
  SEED_TOKEN="$(curl -s --max-time 15 -X POST "http://localhost:12434/dex/token" \
    -d grant_type=password -d "username=${RASK_SEED_USER:-alice@example.com}" \
    -d "password=${RASK_SEED_PASS:-password}" -d scope='openid profile email' \
    -u lance-catalog:lance-catalog-secret \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id_token",""))' 2>/dev/null || true)"
  if SEED_S3_ENDPOINT=http://127.0.0.1:19900 \
     SEED_CATALOG_URL=http://127.0.0.1:12433 \
     SEED_CATALOG_TOKEN="$SEED_TOKEN" \
     SEED_MAX_PAGES="${RASK_SEED_PAGES:-3}" \
     uv run --project "$REPO_ROOT" python "$REPO_ROOT/scripts/seed_bronze_pages.py" 2>&1 | sed 's/^/  /'; then
    :
  else
    echo "  !! lakehouse seeding FAILED — the corpus above is still seeded; see the error and re-run" >&2
    LAKEHOUSE_FAILED=1
  fi

  # The ANNOTATIONS table, through the catalog. Without this the annotate canvas cannot open at all,
  # and the reason is not obvious: the catalog authorises BEFORE it checks existence, so a table that
  # was never created answers 403 "can_get_metadata required" rather than 404. No grant can fix that
  # — there is no object to grant on. Creating it as the demo user makes that user its owner, and
  # `can_get_metadata: reader` follows from ownership.
  #
  # scripts/seed_annotations.py already did all of this and was wired to NOTHING — the same disease
  # as the corpus and bronze seeders before this script existed.
  echo "  annotations table (the canvas cannot open without it):"
  if MEDIA_CATALOG_TOKEN="$SEED_TOKEN" uv run --project "$REPO_ROOT" python -c "
import sys
sys.path.insert(0, '$REPO_ROOT/scripts')
from seed_annotations import seed_catalog
print('   ', seed_catalog('http://127.0.0.1:12433', '$DATASET_ID', 'fe00cd746463ad2c'), 'created')
" 2>&1 | sed 's/^/  /'; then
    :
  else
    echo "  !! annotations table creation FAILED — /annotator will 403 on open" >&2
    LAKEHOUSE_FAILED=1
  fi

  kill "$RUSTFS_PID" "$CATALOG_PID" "$DEX_PID" 2>/dev/null || true
  trap - EXIT INT TERM
fi

say "4/4 a labeling task with items"
# Through the ANNOTATOR's own API — the same door the UI uses, so a task seeded here is a task the
# UI can claim, review and publish. Auth is the estate's: a dex bearer, minted for the demo user.
RASK_SEED_DATASET="$DATASET_ID" "$REPO_ROOT/scripts/seed_labeling_task.sh"

say "5/5 VERIFY — ask the services whether they can actually see it"
# A Job that reached Complete, three rollouts that finished and two curls that did not crash are
# not evidence: a malformed descriptor, a wrong dataset name and an unmounted corpus all print the
# same banner. This repo's own rule ("SSR returning 200 is not working"; `make tilt-verify` is not
# optional ceremony) applies to its seeder too. Nothing below is reachable unless a service opened
# the fixture we just wrote.
VERIFY_FAILED=0
kubectl port-forward -n "$NS" svc/rask-search 18102:8102 >/dev/null 2>&1 &
SEARCH_PID=$!
trap 'kill "$SEARCH_PID" 2>/dev/null || true' EXIT INT TERM
# Poll the ANSWER, not the socket. `kubectl port-forward` binds the local port before the tunnel to
# the pod is up, so a /dev/tcp probe goes green while the very next curl still returns nothing — and
# `|| echo 0` then reports that as "0 hits", i.e. a verification failure indistinguishable from a
# genuinely unreadable corpus. Retry until the service actually answers.
# `protokoll`, not `arkiv`: the fixture's prose carries the DEFINITE form "arkivet" and Lance FTS
# matches whole tokens, so `arkiv` scores nothing. `protokoll` is the term the fixture is built to
# guarantee across several documents — and the bar is >1, because a single-row answer is exactly
# the emptiness this seeder exists to disprove. `n`, not `limit`: SearchSpec ignores unknown keys.
HITS=0
for _ in $(seq 1 30); do
  HITS="$(curl -s --max-time 10 "http://127.0.0.1:18102/api/search?dataset=$DATASET_ID&q=protokoll&n=20" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d if isinstance(d,list) else d.get("hits",[])))' 2>/dev/null || echo 0)"
  [ "${HITS:-0}" -gt 1 ] && break
  sleep 2
done
if [ "${HITS:-0}" -gt 1 ]; then
  echo "  search '$DATASET_ID' q=protokoll -> $HITS hits across the seeded documents"
else
  echo "  !! search returned ${HITS:-0} hits for '$DATASET_ID' (expected >1) — the corpus is not readable by rask-search" >&2
  VERIFY_FAILED=1
fi
kill "$SEARCH_PID" 2>/dev/null || true
trap - EXIT INT TERM

# The ANNOTATE canvas, end to end. Search returning hits proves the corpus is readable; it proves
# nothing about whether a user can open the annotator, which failed for a completely separate reason
# (an unauthenticated catalog read, then a table that did not exist). Both were invisible to every
# check this script had, so both get one here.
kubectl port-forward -n "$NS" svc/rask-annotator 18103:8103 >/dev/null 2>&1 &
ANNO_PID=$!
kubectl port-forward -n "$NS" svc/rask-dex 12434:5556 >/dev/null 2>&1 &
ANNO_DEX_PID=$!
trap 'kill "$ANNO_PID" "$ANNO_DEX_PID" 2>/dev/null || true' EXIT INT TERM
ANNO_CODE=000
for _ in $(seq 1 30); do
  ANNO_TOKEN="$(curl -s --max-time 10 -X POST "http://localhost:12434/dex/token" \
    -d grant_type=password -d "username=${RASK_SEED_USER:-alice@example.com}" \
    -d "password=${RASK_SEED_PASS:-password}" -d scope='openid profile email' \
    -u lance-catalog:lance-catalog-secret \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id_token",""))' 2>/dev/null || true)"
  if [ -n "$ANNO_TOKEN" ]; then
    ANNO_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H "Authorization: Bearer $ANNO_TOKEN" \
      "http://127.0.0.1:18103/api/annotations/fe00cd746463ad2c/0/0?dataset=$DATASET_ID" 2>/dev/null || echo 000)"
    [ "$ANNO_CODE" = "200" ] && break
  fi
  sleep 2
done
if [ "$ANNO_CODE" = "200" ]; then
  echo "  annotate canvas: GET /api/annotations/… -> 200 (readable by the demo user)"
else
  echo "  !! the annotate canvas is NOT openable: GET /api/annotations/… -> $ANNO_CODE" >&2
  echo "     403 = the annotations table is missing or ungranted; 401 = no bearer reached the catalog." >&2
  VERIFY_FAILED=1
fi
kill "$ANNO_PID" "$ANNO_DEX_PID" 2>/dev/null || true
trap - EXIT INT TERM

if [ "${LAKEHOUSE_FAILED:-0}" = "1" ] || [ "$VERIFY_FAILED" = "1" ]; then
  say "NOT seeded"
  [ "${LAKEHOUSE_FAILED:-0}" = "1" ] && echo "the lakehouse half failed (see above)" >&2
  [ "$VERIFY_FAILED" = "1" ] && echo "verification failed — do not trust this estate" >&2
  exit 1
fi

say "seeded"
echo "corpus:    $CORPUS_HOST_PATH  (mount confirmed on rask-viewer)"
echo "verified:  rask-search returns $HITS hits from '$DATASET_ID' (q=protokoll)"
echo "next:      open the ingress and look at /media, /annotator, /lakehouse"
