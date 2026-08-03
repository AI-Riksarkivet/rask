#!/usr/bin/env bash
# Seed ONE labeling task with items, through the annotator's own HTTP API.
#
# Through the API and not the actor store on purpose: a task seeded here is a task the UI can
# claim, review and publish, because it travelled the same door — FGA checks, send capture,
# template capture and all. Anything written under the API would be a fixture the product cannot
# actually operate on.
#
# Called by `seed_dev_estate.sh`; standalone-runnable for a re-seed:
#     scripts/seed_labeling_task.sh
set -euo pipefail

NS="${RASK_NAMESPACE:-default}"
# The tenant the UI will actually show. The annotator zone resolves it from the signed-in user's
# FIRST project (`me.projects[0]`), so seeding into a different one produces the worst possible
# outcome: a task that exists, that the API returns, and that the UI reports as "No labeling tasks
# yet". Resolved from the catalog below unless pinned.
TENANT="${RASK_SEED_TENANT:-}"
SLUG="${RASK_SEED_SLUG:-demo-labeling}"
DEX_USER="${RASK_SEED_USER:-alice@example.com}"
DEX_PASS="${RASK_SEED_PASS:-password}"
# Resolved from the deployment like the tenant is — a hardcoded default seeded items pointing at a
# dataset the estate does not serve, and the canvas then failed with "Failed to load image:
# /annotator/api/chunk-frame/...?dataset=demo" while the corpus was fine.
DATASET="${RASK_SEED_DATASET:-}"

port_forward() { # svc localport targetport -> echoes the pid
  kubectl port-forward -n "$NS" "svc/$1" "$2:$3" >/dev/null 2>&1 &
  echo $!
}

ANNOTATOR_PORT="${RASK_SEED_ANNOTATOR_PORT:-18310}"
DEX_PORT="${RASK_SEED_DEX_PORT:-18311}"
pids=()
cleanup() { for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

pids+=("$(port_forward rask-annotator "$ANNOTATOR_PORT" 8103)")
pids+=("$(port_forward rask-dex "$DEX_PORT" 5556)")
sleep 4

# The estate's own identity: dex's password grant. The catalog and annotator accept ONLY IdP
# bearers, so a seed without a token can create nothing on an auth-enabled stack.
TOKEN="$(curl -s --max-time 15 -X POST "http://localhost:$DEX_PORT/dex/token" \
  -d grant_type=password -d "username=$DEX_USER" -d "password=$DEX_PASS" \
  -d scope='openid profile email' -u lance-catalog:lance-catalog-secret \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id_token",""))')"
if [ -z "$TOKEN" ]; then echo "could not mint a dex token for $DEX_USER" >&2; exit 1; fi

if [ -z "$TENANT" ]; then
  CAT_PORT="${RASK_SEED_CATALOG_PORT:-18312}"
  kubectl port-forward -n "$NS" svc/rask-catalog "$CAT_PORT:2333" >/dev/null 2>&1 &
  pids+=("$!")
  # WAIT for the forward rather than sleeping a guess: a short sleep made this silently fall back
  # to "default" while the UI showed "acme", which is the drift this resolution exists to prevent.
  for _ in $(seq 1 20); do
    curl -sf --max-time 2 "http://localhost:$CAT_PORT/healthz" >/dev/null 2>&1 && break
    sleep 1
  done
  TENANT="$(curl -s --max-time 15 -H "Authorization: Bearer $TOKEN" "http://localhost:$CAT_PORT/v1/me" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); ps=d.get("projects") or []; print(ps[0]["project"] if ps else "")' 2>/dev/null || true)"
  if [ -z "$TENANT" ]; then
    echo "  !! could not resolve the tenant from the catalog — pin it with RASK_SEED_TENANT=<name>" >&2
    exit 1
  fi
fi
echo "  tenant the UI resolves: $TENANT"

if [ -z "$DATASET" ]; then
  MEDIA_DB="$(kubectl get deployment rask-viewer -n "$NS" -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' | sed -n 's/^MEDIA_DB=//p')"
  # No silent "demo" fallback: seeding items that name a dataset nothing serves produces a task
  # whose every image 404s, which looks like a broken canvas rather than a bad seed.
  [ -n "$MEDIA_DB" ] || { echo "!! rask-viewer declares no MEDIA_DB — pass RASK_SEED_DATASET" >&2; exit 1; }
  DATASET="$(basename "$MEDIA_DB" .lance)"
fi
echo "  dataset the deployment serves: $DATASET"

API="http://localhost:$ANNOTATOR_PORT"
AUTH=(-H "Authorization: Bearer $TOKEN" -H 'content-type: application/json')

# Idempotent: a re-seed reuses the task rather than piling up duplicates, so `make seed-dev` is
# safe to run twice.
PROJECT_ID="$(curl -s --max-time 15 "${AUTH[@]}" "$API/projects?tenant=$TENANT" \
  | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(next((p['project_id'] for p in d.get('projects',[]) if p.get('slug')=='$SLUG'), ''))" 2>/dev/null || true)"
# `|| true` above is deliberate — "no such project yet" is the normal first-run answer. But the
# CREATE below is not allowed to fail quietly, so its response is checked rather than parsed for a
# key that a 4xx body simply lacks (which yielded PROJECT_ID="" and a POST to `/projects//items`).

if [ -z "$PROJECT_ID" ]; then
  CREATE_BODY="$(curl -s --max-time 15 -w '\n%{http_code}' -X POST "$API/projects" "${AUTH[@]}" -d "{
      \"tenant\": \"$TENANT\",
      \"slug\": \"$SLUG\",
      \"title\": \"Demo labeling task\",
      \"description\": \"Seeded by scripts/seed_dev_estate.sh — claim, annotate, review, publish.\",
      \"instructions\": \"Draw a box around each block of writing. Skip stamps and marginalia.\",
      \"review_required\": true,
      \"label_schema\": {\"classes\": [{\"name\": \"text-region\", \"shape_types\": [\"bbox\"]}]}
    }")"
  CREATE_CODE="$(printf '%s' "$CREATE_BODY" | tail -1)"
  if [ "$CREATE_CODE" != "200" ] && [ "$CREATE_CODE" != "201" ]; then
    echo "!! create project failed: HTTP $CREATE_CODE $(printf '%s' "$CREATE_BODY" | head -n -1 | head -c 300)" >&2
    exit 1
  fi
  PROJECT_ID="$(printf '%s' "$CREATE_BODY" | head -n -1 | python3 -c 'import sys,json; print(json.load(sys.stdin).get("project_id",""))')"
  [ -n "$PROJECT_ID" ] || { echo "!! create project returned no project_id" >&2; exit 1; }
  curl -s --max-time 15 -X POST "$API/projects/$PROJECT_ID/events" "${AUTH[@]}" \
    -d '{"event":"open"}' >/dev/null
fi
[ -n "$PROJECT_ID" ] || { echo "!! no project id — refusing to POST to /projects//items" >&2; exit 1; }

# STABLE item ids, so a re-seed converges instead of piling up duplicates: `send` is idempotent on
# task_id in the project actor, so the second run reports created=0 and the queue keeps its shape.
# SCOPED to tenant+slug, because a task id is estate-global: an unscoped `seed-demo-0` seeded into
# one tenant makes the same seed into another tenant 409 ("already belongs to project …"), which is
# the actor correctly refusing to index one task into two projects.
ITEM_PREFIX="$(printf 'seed-%s-%s-%s' "$TENANT" "$SLUG" "$DATASET" | tr -c 'A-Za-z0-9_-' '-')"
# Items from the seeded corpus, one per page of the first document — the same key-path shape the
# media zone's "Send to labeling task" produces, including the dataset version for the §7.2 pin.
SENT="$(curl -s --max-time 20 -X POST "$API/projects/$PROJECT_ID/items" "${AUTH[@]}" -d "{
  \"items\": [
    {\"task_id\": \"${ITEM_PREFIX}-0\", \"source\": {\"kind\": \"chunks\", \"keys\": [\"fe00cd746463ad2c/0/0\"], \"where\": \"$DATASET\", \"dataset_version\": 1}, \"media\": {\"kind\": \"image\"}},
    {\"task_id\": \"${ITEM_PREFIX}-1\", \"source\": {\"kind\": \"chunks\", \"keys\": [\"fe00cd746463ad2c/1/1\"], \"where\": \"$DATASET\", \"dataset_version\": 1}, \"media\": {\"kind\": \"image\"}},
    {\"task_id\": \"${ITEM_PREFIX}-2\", \"source\": {\"kind\": \"chunks\", \"keys\": [\"fe00cd746463ad2c/2/2\"], \"where\": \"$DATASET\", \"dataset_version\": 1}, \"media\": {\"kind\": \"image\"}}
  ]}")"
# A 4xx body is still valid JSON, so parsing it for `sent`/`created` yielded "sent=None created=None"
# and an exit 0 — the caller then printed "seeded" over a project with no items. Assert the shape
# instead of reporting whatever came back.
SENT="$(printf '%s' "$SENT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as exc:
    sys.exit(f"items response was not JSON: {exc}")
if not isinstance(d, dict) or d.get("sent") is None:
    sys.exit(f"items POST did not report a send: {json.dumps(d)[:300]}")
print("sent=%s created=%s" % (d["sent"], d.get("created")))')" || {
  echo "!! seeding items FAILED — the project exists but has no items" >&2
  exit 1
}

echo "  labeling task $SLUG ($PROJECT_ID): $SENT"
