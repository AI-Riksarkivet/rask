#!/usr/bin/env bash
# Does the RUNNING OpenFGA store hold the model this repo declares?
#
# `make fga-test` diffs `model.fga` against `model.json` — two files in the repo that agree with each
# other. Nothing compares either against the model the store actually loaded, and that is the copy
# every live authorization check is answered from.
#
# MEASURED 2026-09-22, which is why this exists. With `model.fga` and `model.json` both declaring
# `type estate`, `make fga-test` green and `ms-authz` green in CI, the store held model
# 01M31N78PTV07F5986A0Y2Q0JY with ten types and no `estate`. The chart's `openfga-model` hook had not
# run yet. Nothing in the repo could tell that state from the one after it rolled
# (01M34P37P53P9HHY2E658MQ7JX, eleven types) — the gates are identical in both.
#
# READ FROM INSIDE THE CLUSTER, through the catalog's own pod, for the reason this estate keeps
# relearning: a port-forward answers for a different address than the service uses, and the address
# the code reads is the one that matters (`RASK_FGA_API_URL`).
set -euo pipefail

# THE CLUSTER THIS SCRIPT MEANS ([[XC-057]]). This one targets the deployed estate on purpose, and
# saying so is the point: an absent declaration and a deliberate one used to look identical, so
# "I meant the live cluster" was indistinguishable from "I never thought about it". Overridable, so a
# second estate (another host, another context) is a variable rather than an edit.
: "${RASK_EXPECT_CONTEXT:=default}"
export RASK_EXPECT_CONTEXT


NS="${NS:-default}"
POD="$(kubectl -n "$NS" get pods -o name | grep -m1 'rask-catalog' || true)"
[ -n "$POD" ] || { echo "!! no rask-catalog pod in namespace $NS"; exit 1; }
POD="${POD#pod/}"
API="$(kubectl -n "$NS" exec "$POD" -c catalog -- printenv RASK_FGA_API_URL)"
[ -n "$API" ] || { echo "!! the catalog declares no RASK_FGA_API_URL"; exit 1; }

# COMPARED PER RELATION, not per type name. A type set alone cannot see a rung MOVING between two
# types that both already exist — which is exactly what the estate-root port does: `can_observe_events`
# leaves `warehouse` and appears on `estate`, eleven types before and eleven after. A store that kept
# the old model would answer every estate check from a `warehouse` definition the repo no longer has,
# and a type-name diff would call that a match.
#
# THE STORE'S MODEL IS FETCHED AS JSON AND FLATTENED BY THE SAME CODE as the repo's
# (`scripts/_fga_model_rungs.py`). Two flatteners would be two more copies of the thing this check
# exists to catch.
REPO_RUNGS="$(python3 scripts/_fga_model_rungs.py packages/service-kit/src/service_kit/governed/auth/model.json)"

STORE_MODEL="$(kubectl -n "$NS" exec "$POD" -c catalog -- python -c '
import json, os, sys, urllib.request
api = os.environ["RASK_FGA_API_URL"].rstrip("/")
with urllib.request.urlopen(api + "/stores", timeout=15) as r:
    stores = json.load(r).get("stores") or []
if not stores:
    print("{}"); sys.exit(0)
sid = stores[0]["id"]
with urllib.request.urlopen(f"{api}/stores/{sid}/authorization-models?page_size=1", timeout=15) as r:
    models = json.load(r).get("authorization_models") or []
print(json.dumps(models[0] if models else {}))
')"

STORE_RUNGS="$(printf '%s' "$STORE_MODEL" | python3 scripts/_fga_model_rungs.py)"
[ -n "$STORE_RUNGS" ] || { echo "!! the store holds no authorization model — the openfga-model hook has not run"; exit 1; }

if [ "$REPO_RUNGS" = "$STORE_RUNGS" ]; then
  echo ">> the store's model matches the repo ($(echo "$REPO_RUNGS" | wc -l) relations over $(echo "$REPO_RUNGS" | cut -d'#' -f1 | sort -u | wc -l) types)"
  exit 0
fi

echo "!! THE STORE'S AUTHORIZATION MODEL DOES NOT MATCH THIS REPO."
echo "   in the repo and NOT in the store:"
LC_ALL=C comm -23 <(echo "$REPO_RUNGS") <(echo "$STORE_RUNGS") | sed 's/^/     /'
echo "   in the store and NOT in the repo:"
LC_ALL=C comm -13 <(echo "$REPO_RUNGS") <(echo "$STORE_RUNGS") | sed 's/^/     /'
echo "   Every live check is answered from the STORE. Re-run the chart's openfga-model hook"
echo "   (helm upgrade, or the Job directly) and check it did not fail silently."
exit 1
