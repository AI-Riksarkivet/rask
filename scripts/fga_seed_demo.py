"""Load a REALISTIC estate into the live OpenFGA store — the demo seed that does not exist.

Why this is separate from the bootstrap (the ``bootstrap-admin`` Helm Job, which superseded the old
``fga_bootstrap_owner.py`` script): bootstrap grants a first admin and nothing else — three tuples
(one ``owner`` on the root plus two structural ``parent`` edges), because
its job is to make a locked-out cluster usable, not to populate it. The consequence is that a fresh
cluster's authorization graph has **four nodes** — one user, one warehouse, one namespace, one table —
and every surface built to explore relationships has essentially nothing to show. The governance
explorer renders that faithfully, which reads as a broken view rather than an empty estate.

Meanwhile the model already ships a rich, deliberately-shaped fixture set in ``model.fga.yaml``: three
teams, three projects, three warehouses, a bronze/silver/gold cascade, a `role:validators` persona,
plus users who exercise the interesting cases (a plain writer who is NOT a validator, a validator who
reaches gold only through a role, a grant on a parentless transaction). Those are CI fixtures and have
never been loadable into a running store. This script is that missing half — it reads the SAME file
CI asserts against, so the demo estate and the tested estate cannot drift.

THE SUBJECT MAPPING is the part that matters. The fixtures say ``user:alice``; a real OpenFGA store
keys on the OIDC ``sub``, which under Dex is a base64 protobuf (``CiQwOGE4Njg0Yi1kYjg4…``). Seed the
literal ``user:alice`` and every check for the signed-in alice denies — correctly, and confusingly.
So the demo identities are rewritten to their real subs, and the rest are left literal (they are
personas with no IdP account, and they still make the graph worth reading).

Usage (needs the OpenFGA API reachable — ``kubectl port-forward svc/rask-openfga 18080:8080``):

    uv run python scripts/fga_seed_demo.py
    uv run python scripts/fga_seed_demo.py --dry-run
    uv run python scripts/fga_seed_demo.py --map alice=<sub> --map bob=<sub>

Idempotent: a duplicate write is a success, so re-running after a partial seed is the normal case.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


_FIXTURES = Path(__file__).resolve().parents[1] / "packages/service-kit/src/service_kit/governed/auth/model.fga.yaml"

#: Dex's two static demo identities, encoded the way an OIDC `sub` actually arrives. Kept here rather
#: than derived, because the chart pins these userIDs (chart/templates/dex.yaml) and a seed that
#: silently drifted from them would grant a subject nobody can sign in as.
_DEX_SUBS = {
    "alice": "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fs",
    "bob": "CiQxYjZlMmY1YS03YzRkLTRlOWItOWYxYS0yZDNjNGI1YTZlN2YSBWxvY2Fs",
}

_TUPLE_RE = re.compile(r"-\s*\{\s*user:\s*\"?(?P<user>[^,\"]+)\"?\s*,\s*relation:\s*\"?(?P<relation>[^,\"]+)\"?\s*,\s*object:\s*\"?(?P<object>[^}\"]+)\"?\s*\}")


def _post(base: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(  # noqa: S310 — a fixed localhost base, not user-controlled
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


def load_fixtures() -> list[dict[str, str]]:
    """The tuples block of ``model.fga.yaml`` — the same fixtures ``fga model test`` asserts against."""
    text = _FIXTURES.read_text()
    block = re.search(r"^tuples:(.*?)^tests:", text, re.DOTALL | re.MULTILINE)
    if block is None:
        raise SystemExit(f"!! no `tuples:` block in {_FIXTURES}")
    # STRIP every field. The YAML is written `{ user: x, relation: y, object: z }`, so the object
    # capture runs up to the closing brace and swallows the space before it — OpenFGA then rejects the
    # whole batch on its `^[^\s]{2,256}$` object pattern, which reads as "the fixtures are invalid"
    # rather than "the parser kept a space".
    return [{k: v.strip() for k, v in m.groupdict().items()} for m in _TUPLE_RE.finditer(block.group(1))]


def remap(tuples: list[dict[str, str]], subs: dict[str, str]) -> list[dict[str, str]]:
    """Rewrite ``user:<name>`` to ``user:<oidc-sub>`` for identities that really exist in the IdP.

    Only the subject side is rewritten. A `user:` id never appears as an OBJECT in this model, and
    rewriting one would invent a resource rather than name a principal.
    """
    out: list[dict[str, str]] = []
    for t in tuples:
        user = t["user"]
        if user.startswith("user:"):
            name = user.removeprefix("user:")
            if name in subs:
                user = f"user:{subs[name]}"
        out.append({"user": user, "relation": t["relation"], "object": t["object"]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default="http://127.0.0.1:18080", help="OpenFGA HTTP API base")
    ap.add_argument("--dry-run", action="store_true", help="print what would be written, write nothing")
    ap.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="NAME=SUB",
        help="override a fixture identity's OIDC subject (repeatable)",
    )
    args = ap.parse_args()

    subs = dict(_DEX_SUBS)
    for pair in args.map:
        name, _, sub = pair.partition("=")
        if not sub:
            print(f"!! --map wants NAME=SUB, got {pair!r}", file=sys.stderr)
            return 1
        subs[name] = sub

    tuples = remap(load_fixtures(), subs)
    print(f"  {len(tuples)} fixture tuples from {_FIXTURES.name}")
    print(f"  identities mapped to their OIDC subject: {', '.join(sorted(subs))}")

    if args.dry_run:
        for t in tuples:
            print(f"    {t['user'][:44]:44} --{t['relation']:12}--> {t['object']}")
        return 0

    stores = _get(args.api, "/stores").get("stores") or []
    if not stores:
        print("!! no FGA store — is auth.enabled and the catalog up?", file=sys.stderr)
        return 1
    store = stores[0]["id"]

    written = skipped = failed = 0
    for t in tuples:
        try:
            _post(args.api, f"/stores/{store}/write", {"writes": {"tuple_keys": [t]}})
            written += 1
            print(f"  + {t['object']}#{t['relation']}@{t['user'][:40]}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()[:200]
            # A duplicate is success — re-running over a partial seed is the normal case, not an error.
            if "already exists" in body or "write_failed_due_to_invalid_input" in body:
                skipped += 1
            else:
                failed += 1
                print(f"  !! {t['object']}#{t['relation']}: {body}", file=sys.stderr)

    print(f"\n  written={written} already-present={skipped} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
