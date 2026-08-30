"""Every governed door must honour SPLIT-HORIZON DISCOVERY, and one of them did not.

THE FAILURE. An ingest submitted from `/compute/etl` by a signed-in user died with

    httpx.ConnectError: [Errno 111] Connection refused

surfaced to the browser as `{"message":"Internal Error"}` — a 500 carrying nothing about auth. The
cause: `OIDCVerifier` fetched the discovery document from the ISSUER, which in k3s is the
browser-facing `http://localhost:8080/dex`. Inside a pod that resolves to the pod itself.

The chart had been setting `RASK_OIDC_DISCOVERY_URL=http://rask-dex:5556/dex` all along, and
`GovernedAuthSettings` had been parsing it into `oidc_discovery_url`. The ingest door simply never
passed it to the verifier — while catalog, lineage, viewer, annotator and medallion all did, with the
identical expression at five sites.

WHY NOTHING CAUGHT IT. The service-token path (`dapr-api-token`) returns before the verifier is ever
touched, and that is the path every in-cluster check used — including the nine ingest runs proven
end-to-end that same day. Only a real signed-in browser submit reaches line 151 of `auth.py`. A door
can therefore be completely unable to verify a user token while every service-to-service test passes.

WHAT THIS GATE IS. Not a test of ingest — a test that the doors AGREE. It reads the source of every
`OIDCVerifier(...)` construction and requires each to pass `discovery_overrides`. Written against the
source rather than behaviour because the defect is an omission: there is no call to assert on, only one
that should have been made and was not. A behavioural test would need a live IdP with a split horizon
to reproduce, which is exactly why this went unnoticed.

IT NOW SCANS THE KERNEL, NOT `services/`, and that is the fix landing rather than the gate weakening.
When this was written there were eight hand-rolled constructions across the estate and "do they agree"
was a real question with eight answers. DUP-01 collapsed all of them onto
`service_kit.governed.auth_lifespan.attach_auth`, so today there is ONE construction and agreement is
structural: a door cannot omit an argument it does not pass. Pointing the walk at `services/` after
that would have found zero constructions and passed vacuously — which is why the guard below asserts
the walk found something, and why the scan root moved to where the code went. If a service ever builds
its own verifier again, `tests/unit/test_governed_bootstrap_is_one_implementation.py` refuses it there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]

#: The opening of a construction. The ARGUMENTS are read by `_constructions()` below with a depth
#: counter rather than by this pattern.
#:
#: The previous pattern was `OIDCVerifier\((.*?)\n\s*\)`, which required a NEWLINE before the closing
#: paren — so `OIDCVerifier(iss, aud)` written on one line was invisible to the whole gate. Every
#: construction in the estate happens to be multi-line today, so the gate was green and would have
#: stayed green through a single-line one; and the `>= 5` floor against 8 doors could absorb three
#: disappearances before it noticed. A pattern anchored on incidental FORMATTING cannot be a contract.
CONSTRUCTION_START = re.compile(r"OIDCVerifier\(")


def _constructions(text: str) -> list[str]:
    """The argument text of every `OIDCVerifier(...)`, matched by depth rather than by layout.

    Walking the parens has no formatting assumption to violate and no window to tune — the same fix,
    and the same reason, as the estate's single-flight-keys gate, whose regex could not cross a `)`
    and judged 15 of 40 sites.
    """
    out: list[str] = []
    for match in CONSTRUCTION_START.finditer(text):
        open_paren = match.end() - 1
        depth = 0
        for i in range(open_paren, len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    out.append(text[open_paren + 1 : i])
                    break
    return out


#: Where the estate's governed-auth bootstrap lives. Every door reaches its verifier through here.
GOVERNED = ROOT / "packages/service-kit/src/service_kit/governed"


def _sources() -> list[Path]:
    """Every non-test module in the governed kernel that CONSTRUCTS a verifier.

    Deliberately discovered, not listed. A second construction appearing beside the first is the case
    this gate exists for, and a hardcoded list would silently exempt it — the same shape of omission as
    the bug.
    """
    found = []
    for path in GOVERNED.rglob("*.py"):
        if "test" in path.parts or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _constructions(text):
            found.append(path)
    return sorted(found)


def test_the_estate_has_governed_doors_to_check() -> None:
    """A guard on the guard: if the regex stops matching, every test below passes vacuously."""
    doors = _sources()
    # DERIVED, not a remembered floor. `>= 5` against eight real doors could absorb three
    # disappearances silently — and a floor cannot know what it is missing, which is the failure this
    # audit found in `nav-truth.test.ts` as well. Counting the literal occurrences of the constructor
    # name and requiring the parser to reach all of them means the two can only agree if the walk works.
    mentions = sum(
        len(CONSTRUCTION_START.findall(p.read_text(encoding="utf-8", errors="ignore")))
        for p in GOVERNED.rglob("*.py")
        if "test" not in p.parts and not p.name.startswith("test_")
    )
    parsed = sum(len(_constructions(p.read_text(encoding="utf-8"))) for p in doors)

    assert doors, "no verifier construction found in the governed kernel — the scan is broken, or the bootstrap moved and this gate is now asserting nothing"
    assert parsed == mentions, (
        f"the estate names OIDCVerifier( {mentions} times but the paren walk parsed {parsed} — a "
        "construction is being skipped, so the assertions below run on a subset of the doors"
    )


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_governed_door_passes_discovery_overrides(path: Path) -> None:
    """The issuer names the token's `iss`; the discovery URL is where the document is FETCHED. A door
    that conflates them cannot verify a user token behind a reverse-proxied IdP — and fails at runtime
    with a connection error that says nothing about authentication."""
    for call in _constructions(path.read_text(encoding="utf-8")):
        assert "discovery_overrides" in call, (
            f"{path.relative_to(ROOT)} builds an OIDCVerifier without discovery_overrides — behind a reverse-proxied IdP "
            f"it will fetch discovery from the browser-facing issuer and every user-bearer request in the estate will 500"
        )


def test_the_settings_field_is_the_SHARED_one() -> None:
    """The override must read `GovernedAuthSettings.oidc_discovery_url` — the estate's shared vocabulary.

    Pinned because the tempting local fix is a service-private `RASK_INGEST_OIDC_DISCOVERY_URL`, which
    works and gives one service its own dialect for a setting the chart already sets estate-wide. The
    field exists; the door just has to read it.
    """
    settings = (ROOT / "packages/service-kit/src/service_kit/governed/settings.py").read_text(encoding="utf-8")

    assert 'oidc_discovery_url: str | None = Field(default=None, alias="RASK_OIDC_DISCOVERY_URL")' in settings, (
        "the shared discovery-url setting moved or was renamed — every door's override reads it, and they will fall back to the issuer"
    )
