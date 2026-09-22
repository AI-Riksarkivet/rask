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

NS="${NS:-default}"
POD="$(kubectl -n "$NS" get pods -o name | grep -m1 'rask-catalog' || true)"
[ -n "$POD" ] || { echo "!! no rask-catalog pod in namespace $NS"; exit 1; }
POD="${POD#pod/}"
API="$(kubectl -n "$NS" exec "$POD" -c catalog -- printenv RASK_FGA_API_URL)"
[ -n "$API" ] || { echo "!! the catalog declares no RASK_FGA_API_URL"; exit 1; }

REPO_TYPES="$(python3 -c "
import json,sys
m=json.load(open('packages/service-kit/src/service_kit/governed/auth/model.json'))
print(','.join(sorted(t['type'] for t in m['type_definitions'])))
")"

STORE_TYPES="$(kubectl -n "$NS" exec "$POD" -c catalog -- python -c "
import json,urllib.request,sys
api='$API'
with urllib.request.urlopen(api+'/stores',timeout=15) as r: stores=json.load(r).get('stores') or []
if not stores: print('NO-STORE'); sys.exit(0)
sid=stores[0]['id']
with urllib.request.urlopen(f'{api}/stores/{sid}/authorization-models?page_size=1',timeout=15) as r:
    models=json.load(r).get('authorization_models') or []
if not models: print('NO-MODEL'); sys.exit(0)
print(','.join(sorted(t['type'] for t in models[0].get('type_definitions',[]))))
")"

if [ "$REPO_TYPES" = "$STORE_TYPES" ]; then
  echo ">> the store's model matches the repo ($(echo "$REPO_TYPES" | tr ',' '\n' | wc -l) types)"
  exit 0
fi

echo "!! THE STORE'S AUTHORIZATION MODEL DOES NOT MATCH THIS REPO."
echo "   repo  : $REPO_TYPES"
echo "   store : $STORE_TYPES"
echo "   Every live check is answered from the STORE. Re-run the chart's openfga-model hook"
echo "   (helm upgrade, or the Job directly) and check it did not fail silently."
exit 1
