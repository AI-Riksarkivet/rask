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

# THE POD HALF IS STDLIB ONLY AND DECIDES NOTHING. The store choice and the comparison run repo-side
# (`scripts/_fga_store_check.py`) from the CHECKOUT's rule, because the image can lag the checkout, which
# is the state this check exists to report. It streams raw JSON one page per line and holds one page at
# a time: it runs in the catalog's container, so what it buffers is charged to that pod's memory limit,
# and the live store held 1,353 models on 2026-09-27.
FETCH='
import json, os, sys, urllib.parse, urllib.request
api = os.environ["RASK_FGA_API_URL"].rstrip("/")
token_file = os.environ.get("RASK_FGA_TOKEN_FILE")
def get(path):
    # The projected rask-openfga token of the catalog pod, read per request: OpenFGA refuses a call without it, XC-077.
    headers = {"Authorization": "Bearer " + open(token_file).read().strip()} if token_file else {}
    with urllib.request.urlopen(urllib.request.Request(api + path, headers=headers), timeout=30) as r:
        return json.load(r)
if sys.argv[1] == "stores":
    print(json.dumps({"pinned": os.environ.get("RASK_FGA_STORE_ID", ""), "stores": get("/stores").get("stores") or []}))
    raise SystemExit(0)
store, bound, size, token = sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), ""
for _ in range(bound):
    page = get(f"/stores/{store}/authorization-models?page_size={size}" + (f"&continuation_token={urllib.parse.quote(token)}" if token else ""))
    print(json.dumps(page), flush=True)
    token = page.get("continuation_token") or ""
    if not token:
        raise SystemExit(0)
raise SystemExit(f"store {store}: model history longer than {bound} pages of {size}; this check reads it whole and refuses past the page bound the writers use")
'

# THE STORE IS THE ONE THE ESTATE USES: the catalog's `RASK_FGA_STORE_ID` pin, else the newest store
# named `lance-catalog` (`fga.newest_store`), the rule the hook and every service share ([[LH-201]]).
PLAN="$(kubectl -n "$NS" exec "$POD" -c catalog -- python -c "$FETCH" stores | uv run python scripts/_fga_store_check.py plan)"
read -r STORE MAX_PAGES PAGE_SIZE <<<"$PLAN"

# THE QUESTION IS WHETHER THE STORE HOLDS THIS CHECKOUT'S MODEL, at any depth of its history, not
# whether its newest is this checkout's. Each service checks against the model its own image carries,
# so a body held below the newest is one a pod built from this checkout resolves. When it is absent,
# the report is PER RELATION against the newest (`scripts/_fga_model_rungs.py` flattens both sides): a
# type-name diff cannot see a rung MOVING between two types that both exist, as `can_observe_events` did
# from `warehouse` to `estate` with eleven types before and eleven after.
PAGES="$(mktemp)"
trap 'rm -f "$PAGES"' EXIT
kubectl -n "$NS" exec "$POD" -c catalog -- python -c "$FETCH" models "$STORE" "$MAX_PAGES" "$PAGE_SIZE" > "$PAGES"
uv run python scripts/_fga_store_check.py compare "$STORE" < "$PAGES"
