"""Every governed door must honour SPLIT-HORIZON DISCOVERY, and one of them did not.

THE FAILURE. An ingest submitted from `/compute/etl` by a signed-in user died with

    httpx.ConnectError: [Errno 111] Connection refused

surfaced to the browser as `{"message":"Internal Error"}` — a 500 carrying nothing about auth. The
cause: `OIDCVerifier` fetched the discovery document from the ISSUER, which in k3s is the
browser-facing `http://localhost:8080/dex`. Inside a pod that resolves to the pod itself.

The chart had been setting `LANCE_OIDC_DISCOVERY_URL=http://rask-dex:5556/dex` all along, and
`GovernedAuthSettings` had been parsing it into `oidc_discovery_url`. The ingest door simply never
passed it to the verifier — while catalog, lineage, viewer, annotator and medallion all did, with the
identical expression at five sites.

WHY NOTHING CAUGHT IT. The service-token path (`dapr-api-token`) returns before the verifier is ever
touched, and that is the path every in-cluster check used — including the nine ingest runs proven
end-to-end that same day. Only a real signed-in browser submit reaches line 151 of `auth.py`. A door
can therefore be completely unable to verify a user token while every service-to-service test passes.

WHAT THIS GATE IS. Not a test of ingest — a test that the doors AGREE. It reads the source of every
`OIDCVerifier(...)` construction in the estate and requires each to pass `discovery_overrides`. Written
against the source rather than behaviour because the defect is an omission: there is no call to assert
on, only one that should have been made and was not. A behavioural test would need a live IdP with a
split horizon to reproduce, which is exactly why this went unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]

#: `OIDCVerifier(` through its closing paren. Non-greedy across newlines: these constructions are
#: multi-line keyword calls, so a line-oriented scan would see the opening line and none of the
#: arguments — and would pass on exactly the code that fails.
CONSTRUCTION = re.compile(r"OIDCVerifier\((.*?)\n\s*\)", re.DOTALL)


def _sources() -> list[Path]:
    """Every non-test module under `services/` that CONSTRUCTS a verifier.

    Deliberately discovered, not listed. A new governed service is the case this gate exists for, and a
    hardcoded list would silently exempt it — the same shape of omission as the bug.
    """
    found = []
    for path in (ROOT / "services").rglob("*.py"):
        if "test" in path.parts or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if CONSTRUCTION.search(text):
            found.append(path)
    return sorted(found)


def test_the_estate_has_governed_doors_to_check() -> None:
    """A guard on the guard: if the regex stops matching, every test below passes vacuously."""
    doors = _sources()
    assert len(doors) >= 5, f"expected the estate's governed doors, found {[str(p) for p in doors]} — the scan is broken, not the code"


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_governed_door_passes_discovery_overrides(path: Path) -> None:
    """The issuer names the token's `iss`; the discovery URL is where the document is FETCHED. A door
    that conflates them cannot verify a user token behind a reverse-proxied IdP — and fails at runtime
    with a connection error that says nothing about authentication."""
    for call in CONSTRUCTION.findall(path.read_text(encoding="utf-8")):
        assert "discovery_overrides" in call, (
            f"{path.relative_to(ROOT)} builds an OIDCVerifier without discovery_overrides — behind a reverse-proxied IdP "
            f"it will fetch discovery from the browser-facing issuer and every user-bearer request will 500"
        )


def test_the_settings_field_is_the_SHARED_one() -> None:
    """The override must read `GovernedAuthSettings.oidc_discovery_url` — the estate's shared vocabulary.

    Pinned because the tempting local fix is a service-private `RASK_INGEST_OIDC_DISCOVERY_URL`, which
    works and gives one service its own dialect for a setting the chart already sets estate-wide. The
    field exists; the door just has to read it.
    """
    settings = (ROOT / "packages/service-kit/src/service_kit/governed/settings.py").read_text(encoding="utf-8")

    assert 'oidc_discovery_url: str | None = Field(default=None, alias="LANCE_OIDC_DISCOVERY_URL")' in settings, (
        "the shared discovery-url setting moved or was renamed — every door's override reads it, and they will fall back to the issuer"
    )
