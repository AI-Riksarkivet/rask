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

THE SUBJECT MAPPING is the part that matters. The fixtures say ``user:alice``; a real OpenFGA store keys a person on
their principal, ``<idp-id>~<user id>`` ([[LH-063]]), and no door seeds a bare name. So the development Dex's identities
are rewritten to their principals (``dex~<userID>``), and every other name is a persona with no IdP account, a principal
of the ``demo`` idp id no connector maps: it still makes the graph worth reading, and still names an identity
provider.

Usage (needs the OpenFGA API reachable — ``kubectl port-forward svc/rask-openfga 18080:8080``):

    uv run python scripts/fga_seed_demo.py
    uv run python scripts/fga_seed_demo.py --dry-run
    uv run python scripts/fga_seed_demo.py --map alice=<sub> --map bob=<sub>

Idempotent: a duplicate write is a success, so re-running after a partial seed is the normal case.

The store and model are the ones every service uses (`write_model.carried_model`, [[LH-201]]): the
``RASK_FGA_STORE_ID`` pin, else the newest store named ``lance-catalog``, and in it the model whose body
is this checkout's ``model.json``. A write naming no model is validated against the store's newest, and
a legacy image's narrower body can be the newest.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from service_kit.governed.auth.write_model import carried_model, fga_headers
from service_kit.governed.fga import user_object


_FIXTURES = Path(__file__).resolve().parents[1] / "packages/service-kit/src/service_kit/governed/auth/model.fga.yaml"

#: The development Dex's two static demo identities as the principals they are ([[LH-063]]): `dex~` and the userID the
#: Dex fixture declares (deploy/fixtures/dex/chart/values.yaml). Kept in step with ``seed_estate.DEX_PRINCIPALS`` — a
#: seed that drifted from them would grant a principal nobody can sign in as.
_DEX_PRINCIPALS = {
    "alice": "dex~08a8684b-db88-4b73-90a9-3cd1661f5466",
    "bob": "dex~1b6e2f5a-7c4d-4e9b-9f1a-2d3c4b5a6e7f",
}

#: The idp id a fixture persona is a principal of: no connector maps it, so no token signs in as one.
_DEMO_IDP = "demo"

_TUPLE_RE = re.compile(r"-\s*\{\s*user:\s*\"?(?P<user>[^,\"]+)\"?\s*,\s*relation:\s*\"?(?P<relation>[^,\"]+)\"?\s*,\s*object:\s*\"?(?P<object>[^}\"]+)\"?\s*\}")


def _post(base: str, path: str, payload: dict, *, token_file: str | None) -> dict:
    req = urllib.request.Request(  # noqa: S310 — a fixed localhost base, not user-controlled
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", **fga_headers(token_file)},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as res:  # noqa: S310 — a fixed localhost base
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


def remap(tuples: list[dict[str, str]], principals: dict[str, str]) -> list[dict[str, str]]:
    """Rewrite ``user:<name>`` to the OpenFGA user of its principal: an IdP identity's own, else a persona's under ``demo``.

    Every person is written as the principal it is (``fga.user_object``), so this seed writes no tuple naming a bare
    name ([[LH-063]]). Only the subject side is rewritten, and never the wildcard ``user:*``: a `user:` id never appears
    as an OBJECT in this model, and rewriting one would invent a resource rather than name a principal.
    """
    out: list[dict[str, str]] = []
    for t in tuples:
        user = t["user"]
        if user.startswith("user:") and user != "user:*":
            name = user.removeprefix("user:")
            user = user_object(principals.get(name, f"{_DEMO_IDP}~{name}"))
        out.append({"user": user, "relation": t["relation"], "object": t["object"]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default="http://127.0.0.1:18080", help="OpenFGA HTTP API base")
    ap.add_argument(
        "--token-file",
        default=os.environ.get("OPENFGA_TOKEN_FILE"),
        help="a rask-openfga token, e.g. `kubectl create token rask-sa-jobs --audience rask-openfga` written to a file (XC-077)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print what would be written, write nothing")
    ap.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="NAME=PRINCIPAL",
        help="override a fixture identity's principal, <idp-id>~<user id> (repeatable)",
    )
    args = ap.parse_args()

    principals = dict(_DEX_PRINCIPALS)
    for pair in args.map:
        name, _, principal = pair.partition("=")
        if not principal:
            print(f"!! --map wants NAME=PRINCIPAL, got {pair!r}", file=sys.stderr)
            return 1
        principals[name] = principal

    tuples = remap(load_fixtures(), principals)
    print(f"  {len(tuples)} fixture tuples from {_FIXTURES.name}")
    print(f"  identities mapped to their principal: {', '.join(sorted(principals))}")

    if args.dry_run:
        for t in tuples:
            print(f"    {t['user'][:44]:44} --{t['relation']:12}--> {t['object']}")
        return 0

    try:
        store, model = carried_model(args.api, token_file=args.token_file, pinned=os.environ.get("RASK_FGA_STORE_ID", ""))
    except LookupError as exc:
        print(f"!! {exc} — is auth.enabled and the catalog up?", file=sys.stderr)
        return 1
    print(f"  store {store}, model {model}")

    written = skipped = failed = 0
    for t in tuples:
        try:
            _post(args.api, f"/stores/{store}/write", {"writes": {"tuple_keys": [t]}, "authorization_model_id": model}, token_file=args.token_file)
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
