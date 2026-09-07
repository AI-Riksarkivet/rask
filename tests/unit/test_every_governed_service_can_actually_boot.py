"""Every service that refuses to boot unauthenticated is given an answer by the chart — in BOTH modes.

`assert_authentication_configured` (Q17-6 / §F2-2) refuses to start a service whose authentication is
off with nobody having acknowledged it. That is the control; this is the thing that stops the control
being an outage.

TWO MODES, BECAUSE THE FAILURE IS IN THE SECOND. With `auth.enabled: true` the chart renders
`RASK_OIDC_ENABLED` and the assertion passes. With `auth.enabled: false` — a SUPPORTED toggle, the
open dev/demo profile — it renders neither, and every wired service would crash-loop on a values
change that is documented as safe. Measured while writing this: the first pass guarded the catalog
and the medallion producer and missed lineage, which inlines its own copy of `governedOidcEnv`
instead of including the helper, so a grep for the helper could not see it.

THE SERVICE LIST IS DISCOVERED, NOT WRITTEN DOWN. A hand-maintained list is exactly what fails here:
the next service wired to the assertion is the one nobody adds, and it fails only under the profile
nobody renders in CI. So this greps the sources for the call and demands the chart answer for each —
wiring a new service without its env now reds this test rather than the estate.

WHAT IT DOES NOT ASSERT: which answer. A service may be authenticated or may be declared open; both
are legitimate and the choice belongs to the values. What is never legitimate is neither.
"""

from __future__ import annotations

import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _rendered_docs  # noqa: E402


REPO = pathlib.Path(__file__).resolve().parents[2]
#: The env either of which lets a governed service start. `assert_authentication_configured` accepts
#: OIDC being on, or an explicit acknowledgement that it is deliberately off.
SATISFIES = ("RASK_OIDC_ENABLED", "RASK_INSECURE_ALLOW_UNAUTHENTICATED")


def _services_that_refuse_to_boot_unauthenticated() -> set[str]:
    """The services whose boot calls the assertion, read off the source rather than a list."""
    found: set[str] = set()
    for path in (REPO / "services").rglob("*.py"):
        if "/tests/" in str(path) or "assert_authentication_configured(" not in path.read_text():
            continue
        found.add(path.relative_to(REPO / "services").parts[0])
    return found


def _deployments_of(service: str, docs: list[dict]) -> list[dict]:
    """Deployments whose name carries the service's own name — the chart's naming convention."""
    return [d for d in docs if d.get("kind") == "Deployment" and service in d["metadata"]["name"]]


def test_the_discovery_finds_something() -> None:
    """A grep that silently matches nothing would make every assertion below vacuous."""
    assert _services_that_refuse_to_boot_unauthenticated(), "no service calls assert_authentication_configured — is the guard gone?"


@pytest.mark.parametrize("auth_enabled", ["true", "false"])
def test_every_such_service_is_given_an_answer_by_the_chart(auth_enabled: str) -> None:
    docs = _rendered_docs(f"auth.enabled={auth_enabled}")
    unanswered: list[str] = []

    for service in sorted(_services_that_refuse_to_boot_unauthenticated()):
        for deployment in _deployments_of(service, docs):
            env = {
                e["name"]
                for container in deployment["spec"]["template"]["spec"]["containers"]
                for e in (container.get("env") or [])
            }
            if not env & set(SATISFIES):
                unanswered.append(deployment["metadata"]["name"])

    assert not unanswered, (
        f"with auth.enabled={auth_enabled} these Deployments render NEITHER {SATISFIES[0]} nor "
        f"{SATISFIES[1]}, so their boot assertion refuses and they crash-loop: {sorted(unanswered)}"
    )


def test_a_service_with_no_human_door_is_NOT_wired_to_the_assertion() -> None:
    """The other direction, and it cost two reverts to learn. `maintenance` and `notifications`
    mix in `FgaSettings` alone — "no human door: its routes are gated by the Dapr app token and it
    only ever READS tuples, as itself" — and the medallion MOVERS render no OIDC either. Wiring the
    assertion into any of them refuses a boot over an authentication mode they never had."""
    doorless = {"maintenance", "notifications"}
    wired = _services_that_refuse_to_boot_unauthenticated()
    assert not (wired & doorless), (
        f"{sorted(wired & doorless)} carry the assertion but have no human door — the chart renders "
        "them no OIDC, so this refuses a boot over a mode they never had"
    )


def test_the_ack_is_absent_when_the_estate_is_actually_governed() -> None:
    """An acknowledgement that authentication is off, rendered on an authenticated estate, would be a
    standing lie in `kubectl describe` — and the one an operator would find while investigating."""
    docs = _rendered_docs("auth.enabled=true")
    leaked = [
        d["metadata"]["name"]
        for d in docs
        if d.get("kind") == "Deployment"
        for c in d["spec"]["template"]["spec"]["containers"]
        if any(e["name"] == "RASK_INSECURE_ALLOW_UNAUTHENTICATED" for e in (c.get("env") or []))
    ]
    assert not leaked, f"a governed estate renders the unauthenticated acknowledgement on: {sorted(set(leaked))}"
