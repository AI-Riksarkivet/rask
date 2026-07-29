"""Grant a signed-in user ownership of the estate root — the BOOTSTRAP that does not exist.

Why this script has to exist: the catalog seeds FGA ownership at registration time, from the CALLER's
token (`fga_deps.seed_ownership(client, settings, token, ...)`). With `auth.enabled=false` there is no
token, so `seed_ownership` writes nothing — every namespace and table created in open dev mode is
registered with **no owner at all**. Turn auth on afterwards and the store is empty: every governed
list filters through `list_objects(user=sub, relation=...)` and returns `[]` with HTTP 200, so the UI
shows an empty catalog rather than a denial, for every user including the first one.

There is no bootstrap path in the estate today — nothing grants a first admin, and nothing attributes
data created before auth to anyone. That is the real defect; this script is the stopgap that makes a
dev cluster usable, and it is deliberately explicit about what it grants.

The grant chain, smallest that works (the model is concentric, so ownership flows down `parent`):

    warehouse:lance_catalog  owner   <- user:<sub>     # fga_root_object; owner => can_observe_events
    namespace:<ns>           parent  <- warehouse:...  # namespace inherits `owner from parent`
    table:<ns>$<table>       parent  <- namespace:<ns> # table inherits `owner from parent`

`can_observe_events` on the root is what `/v1/me` reads for `estate_admin`, so the shell paints admin
surfaces once this lands.

Usage (needs the OpenFGA API reachable — `kubectl port-forward svc/rask-openfga 18080:8080`):

    uv run python scripts/fga_bootstrap_owner.py --sub "<the OIDC sub>"
    uv run python scripts/fga_bootstrap_owner.py --sub "<sub>" --namespace bronze --table pages
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _post(base: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as res:  # noqa: S310 — a fixed localhost base
        return json.loads(res.read() or b"{}")


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base}{path}", timeout=20) as res:  # noqa: S310
        return json.loads(res.read() or b"{}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default="http://127.0.0.1:18080", help="OpenFGA HTTP API base")
    ap.add_argument("--sub", required=True, help="the OIDC subject to grant (from /v1/me)")
    ap.add_argument("--root", default="warehouse:lance_catalog", help="LANCE_FGA_ROOT_OBJECT")
    ap.add_argument("--namespace", default="bronze")
    ap.add_argument("--table", default="pages")
    ap.add_argument("--delimiter", default="$")
    args = ap.parse_args()

    stores = _get(args.api, "/stores").get("stores") or []
    if not stores:
        print("!! no FGA store — is auth.enabled and the catalog up?", file=sys.stderr)
        return 1
    store = stores[0]["id"]
    ns = f"namespace:{args.namespace}"
    tbl = f"table:{args.namespace}{args.delimiter}{args.table}"

    tuples = [
        {"user": f"user:{args.sub}", "relation": "owner", "object": args.root},
        {"user": args.root, "relation": "parent", "object": ns},
        {"user": ns, "relation": "parent", "object": tbl},
    ]

    # Idempotent by construction: OpenFGA rejects a duplicate write outright, and re-running this on a
    # partially-seeded store is the normal case (a later table lands, the root grant already exists).
    # So write one at a time and treat "already exists" as success rather than aborting the batch.
    for t in tuples:
        try:
            _post(args.api, f"/stores/{store}/write", {"writes": {"tuple_keys": [t]}})
            print(f"  + {t['object']}#{t['relation']}@{t['user'][:44]}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()[:200]
            if "already exists" in body or "write_failed_due_to_invalid_input" in body:
                print(f"  = {t['object']}#{t['relation']} (already present)")
            else:
                print(f"  !! {t['object']}#{t['relation']}: {body}", file=sys.stderr)
                return 1

    check = _post(
        args.api,
        f"/stores/{store}/check",
        {"tuple_key": {"user": f"user:{args.sub}", "relation": "can_read_data", "object": tbl}},
    )
    print(f"  check can_read_data on {tbl}: {check.get('allowed')}")
    return 0 if check.get("allowed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
