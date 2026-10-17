#!/usr/bin/env python3
"""The principal one of the Dex dev fixture's static users is, derived without asking Dex ([[LH-063]]).

A person is `<idp-id>~<user id>`: the idp id the deployment names their Dex connector (`auth.oidc.connectors`; the
development Dex's password database, connector `local`, is `dex`) and the user id that connector reports, which for a
static password is its `userID`. The live lanes need it BEFORE the release exists: `auth.bootstrapAdmin` is read by the
post-install hook that grants the estate admin, so it has to be on the install's command line, and no token can be
minted until Dex is up. Reading the userID from the fixture's own values keeps the fixture the one place a user is
defined.

`scripts/e2e_stack.sh` compares this against the principal the catalog answers for the token Dex actually mints
(`GET /management/v1/me`), so a change in what Dex reports fails that lane at the seed step rather than as a governance
refusal in every suite.

Usage:  uv run python scripts/dex_principal.py alice@example.com
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


FIXTURE_VALUES = Path(__file__).resolve().parents[1] / "deploy" / "fixtures" / "dex" / "chart" / "values.yaml"

#: The idp id the chart gives the development Dex's password database (`lance.oidcConnectors`).
DEV_IDP = "dex"


def fixture_user_id(email: str, values: Path = FIXTURE_VALUES) -> str:
    """The userID the Dex fixture declares for `email`."""
    users = yaml.safe_load(values.read_text(encoding="utf-8"))["users"]
    for user in users:
        if user["email"] == email:
            return str(user["userID"])
    raise LookupError(f"the Dex fixture ({values}) declares no user {email!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="The principal one of the Dex fixture's static users is.")
    parser.add_argument("email", help="the user's email, as deploy/fixtures/dex/chart/values.yaml lists it")
    args = parser.parse_args()
    print(f"{DEV_IDP}~{fixture_user_id(args.email)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
