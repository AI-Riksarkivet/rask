#!/usr/bin/env python3
"""The `sub` Dex's dev fixture issues for one of its static users, derived without asking Dex.

A Dex password-DB login carries `sub` = base64url(protobuf `{1: userID, 2: connector id}`), unpadded, and
the connector of a static password is `local`. The live lanes need that value BEFORE the release exists:
`auth.bootstrapAdmin` is read by the post-install hook that grants the estate admin, so it has to be on
the install's command line, and no token can be minted until Dex is up. Reading the userID from the
fixture's own values keeps the fixture the one place a user is defined.

`scripts/e2e_stack.sh` compares this against the `sub` of the token Dex actually mints, so a change in
Dex's encoding fails that lane at the seed step rather than as a governance refusal in every suite.

Usage:  uv run python scripts/dex_subject.py alice@example.com
"""

from __future__ import annotations

import argparse
import base64
from pathlib import Path

import yaml


FIXTURE_VALUES = Path(__file__).resolve().parents[1] / "deploy" / "fixtures" / "dex" / "chart" / "values.yaml"

#: The connector id Dex gives its built-in password database.
LOCAL_CONNECTOR = "local"


def _field(number: int, value: str) -> bytes:
    """One length-delimited protobuf field; both values here are far below the 128-byte varint boundary."""
    raw = value.encode()
    if len(raw) > 127:
        raise ValueError(f"field {number} is {len(raw)} bytes; this encoder writes single-byte lengths")
    return bytes([number << 3 | 2, len(raw)]) + raw


def dex_subject(user_id: str, connector: str = LOCAL_CONNECTOR) -> str:
    """The `sub` Dex issues for `user_id` logged in through `connector`."""
    return base64.urlsafe_b64encode(_field(1, user_id) + _field(2, connector)).rstrip(b"=").decode()


def fixture_user_id(email: str, values: Path = FIXTURE_VALUES) -> str:
    """The userID the Dex fixture declares for `email`."""
    users = yaml.safe_load(values.read_text(encoding="utf-8"))["users"]
    for user in users:
        if user["email"] == email:
            return str(user["userID"])
    raise LookupError(f"the Dex fixture ({values}) declares no user {email!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="The Dex `sub` of one of the Dex fixture's static users.")
    parser.add_argument("email", help="the user's email, as deploy/fixtures/dex/chart/values.yaml lists it")
    args = parser.parse_args()
    print(dex_subject(fixture_user_id(args.email)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
