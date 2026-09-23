"""A local estate comes up with an administrator — the dex dev user a person can actually sign in as.

`auth.bootstrapAdmin` defaults to empty in the chart, and must: a security default fails closed, and a
real deployment names its own first owner. But `values-local.yaml` left it empty too, so a fresh local
estate granted no human anything. Measured 2026-09-23 on a fresh host: signing in as the dev user
worked, `/v1/me` answered `estate_admin: false, projects: []`, and every governed surface answered 403
— an estate nobody could use until someone wrote FGA tuples by hand.

The subject is DERIVED, not pasted: dex issues `sub = base64url(protobuf{1: userID, 2: connector id})`
for a static user, so the test computes it from the userID `templates/dex.yaml` gives the static user
the chart signs in as. A dex change that re-keys that user fails here instead of leaving the admin grant
pointing at a subject nobody holds.
"""

from __future__ import annotations

import base64
import re

import yaml

from tests.unit import chart_render


REPO = chart_render.REPO
LOCAL = ("-f", str(REPO / "chart" / "values-local.yaml"))


def _dex_subject(user_id: str, connector: str = "local") -> str:
    """The `sub` dex mints for a static-password user: its internal ID proto, base64url, unpadded."""
    uid, conn = user_id.encode(), connector.encode()
    raw = b"\x0a" + bytes([len(uid)]) + uid + b"\x12" + bytes([len(conn)]) + conn
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _static_user_ids(docs: tuple[dict, ...]) -> dict[str, str]:
    """`{email: userID}` for every static-password user in the rendered dex config."""
    for doc in docs:
        for blob in list((doc.get("data") or {}).values()) + list((doc.get("stringData") or {}).values()):
            if isinstance(blob, str) and "staticPasswords" in blob:
                config = yaml.safe_load(blob)
                return {entry["email"]: entry["userID"] for entry in config.get("staticPasswords") or []}
    raise AssertionError("no rendered dex config carries staticPasswords")


def _bootstrap_subject(docs: tuple[dict, ...]) -> str:
    for doc in docs:
        if doc.get("kind") == "Job" and doc["metadata"]["name"].endswith("bootstrap-admin"):
            script = str(doc["spec"]["template"]["spec"]["containers"])
            match = re.search(r"subject = \\?\"([^\"\\]*)\\?\"", script)
            assert match, 'the bootstrap-admin script no longer assigns `subject = "..."`'
            return match.group(1)
    raise AssertionError("no bootstrap-admin Job rendered")


def test_the_local_estate_makes_the_dev_sign_in_user_its_admin() -> None:
    docs = chart_render.render(*chart_render.DEFAULT_ARGS, *LOCAL)
    dev_user = yaml.safe_load((REPO / "chart" / "values.yaml").read_text(encoding="utf-8"))["dex"]["staticUser"]

    expected = _dex_subject(_static_user_ids(docs)[dev_user])

    assert _bootstrap_subject(docs) == expected, (
        f"values-local.yaml must name the dex subject of {dev_user} ({expected}) as auth.bootstrapAdmin, "
        "or a fresh local estate has no human who can administer it"
    )


def test_the_chart_default_still_names_nobody() -> None:
    """Fail closed where it matters: only the local overlay names a dev admin."""
    assert _bootstrap_subject(chart_render.render(*chart_render.DEFAULT_ARGS)) == ""
