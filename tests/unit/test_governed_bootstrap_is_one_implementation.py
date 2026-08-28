"""The governed-auth bootstrap has ONE implementation, and every door's posture is declared at it.

`tests/unit/test_governed_services_wire_their_gate.py` asks whether a door is CONNECTED. This asks
whether it is connected to the same thing as every other door — the finding one layer down.

THE RECORD. `service_kit.governed.auth_lifespan.attach_auth` was extracted for `compute` and
`controlplane` and the ten older services kept their own ~30-line copy: build an `OIDCVerifier`,
pinned-store-else-`fga.provision`, `fga.make_client`. Twelve doors, two mechanisms. That is not a
tidiness complaint — the estate has already paid for it twice, in the same shape both times:

* ING-02's blocking-verify fix landed on the ingest copy of a duplicated auth function and never
  reached the medallion copy.
* Split-horizon discovery (`tests/unit/test_oidc_discovery_parity.py`) was written into five copies
  and omitted from the sixth, and every service-to-service test passed anyway because the
  service-token path returns before the verifier is touched.

A helper that ten services do not call cannot carry a fix to them.

WHY THIS IS A SOURCE-LEVEL GATE. The defect is an OMISSION — a call that should have been made and
was not — so there is no behaviour to assert on; the same reason the discovery-parity gate reads
source. Behaviour lives where behaviour can be run: the postures themselves are exercised against the
helper in `packages/service-kit/tests/test_attach_auth_postures.py`, and each service's own suite runs
its real lifespan.

THE POSTURE MAP IS THE POINT, not a footnote. Collapsing the copies is only correct if the semantic
divergences survive as PARAMETERS: `catalog`, `lineage` and both medallion apps crash on a failed
build (no `try` at all today), and `ingest` and `maintenance` must never author an authorization
model. A migration that flips either is a regression this gate refuses, because both fail SILENTLY —
a crash-on-boot quietly becoming a 503 posture removes the only signal anybody watches.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
SERVICES = REPO / "services"

#: A construction or a store call that belongs to the shared kernel, not to a service. Matched by
#: name rather than by import, because the copies reach it three different ways (`from ... import
#: OIDCVerifier`, a function-local import, `fga.provision`).
_HAND_ROLLED = re.compile(r"OIDCVerifier\(|fga\.provision\(|fga\.resolve\(|fga\.make_client\(")

#: service -> the flag its `attach_auth` / `build_fga_client` call MUST carry, and why.
_REQUIRED_POSTURE = {
    "catalog": ("fatal=True", "catalog raises on a failed build today — a 503 posture would hide a broken authz plane behind a healthy-looking pod"),
    "lineage": ("fatal=True", "lineage raises on a failed build today — same posture, same reason"),
    "medallion": ("fatal=True", "both medallion apps build with no `try`; the cascade must not run with authorization half-built"),
    "ingest": ("provision=False", "a data writer that mints a store becomes the source of truth for everyone else's permissions"),
    "maintenance": ("provision=False", "the sweep reads and revokes tuples; it must never author the estate's model"),
}


def _sources(service: Path) -> list[Path]:
    src = service / "src"
    return [p for p in src.rglob("*.py") if "test" not in p.parts and not p.name.startswith("test_")] if src.is_dir() else []


def _governed_services() -> list[Path]:
    """Every service that bootstraps governed auth at all — by the helper OR by hand.

    Derived, never listed. A hardcoded roster is the same shape of omission as the bug: it exempts
    the next door silently, which is exactly how `search` shipped a gate wired to nothing.
    """
    out = []
    for service in sorted(SERVICES.iterdir()):
        if not service.is_dir():
            continue
        if any(("attach_auth" in p.read_text() or "build_fga_client" in p.read_text() or _HAND_ROLLED.search(p.read_text())) for p in _sources(service)):
            out.append(service)
    return out


def test_the_gate_can_see_the_estates_doors() -> None:
    """A guard on the guard: a layout change that emptied this walk would pass everything below."""
    found = [s.name for s in _governed_services()]
    assert len(found) >= 10, f"only {found} appear to bootstrap governed auth — the walk is not seeing the estate"


def test_no_service_hand_rolls_the_governed_auth_bootstrap() -> None:
    """One implementation, in `service_kit`, so a fix to it reaches every door that has one."""
    offenders: list[str] = []
    for service in _governed_services():
        for path in _sources(service):
            if _HAND_ROLLED.search(path.read_text()):
                offenders.append(path.relative_to(REPO).as_posix())

    assert not offenders, (
        "these modules build the OIDC verifier or the FGA client themselves instead of calling "
        "`service_kit.governed.auth_lifespan.attach_auth` / `build_fga_client`, so every fix to the "
        "bootstrap — split-horizon discovery, a blocking verify, a disposal — has to be applied to each "
        "of them by hand, and the estate has twice found out that it was not:\n  " + "\n  ".join(sorted(set(offenders)))
    )


@pytest.mark.parametrize(("service", "flag_and_why"), sorted(_REQUIRED_POSTURE.items()))
def test_a_divergent_posture_is_declared_at_the_call_site(service: str, flag_and_why: tuple[str, str]) -> None:
    """The divergences are parameters, not casualties. Each of these five services differs from the
    default for a stated reason, and the difference has to be visible where the call is made."""
    flag, why = flag_and_why
    sources = _sources(SERVICES / service)
    callers = [p for p in sources if "attach_auth(" in p.read_text() or "build_fga_client(" in p.read_text()]

    assert callers, f"{service} does not call the shared bootstrap at all"
    for path in callers:
        assert flag in path.read_text(), f"{path.relative_to(REPO)} must pass `{flag}`: {why}"
