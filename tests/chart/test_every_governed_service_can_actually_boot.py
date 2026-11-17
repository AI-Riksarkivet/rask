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

THE SERVICES ARE NAMED. `REFUSE_TO_BOOT_UNAUTHENTICATED` lists the services whose boot calls the
assertion, and a service wired to it joins the list in the same change. A named service with no
Deployment in the render fails, so the list cannot rot into naming nothing.

WHAT IT DOES NOT ASSERT: which answer. A service may be authenticated or may be declared open; both
are legitimate and the choice belongs to the values. What is never legitimate is neither.
"""

from __future__ import annotations

import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _rendered_docs  # noqa: E402


#: The env either of which lets a governed service start. `assert_authentication_configured` accepts
#: OIDC being on, or an explicit acknowledgement that it is deliberately off.
SATISFIES = ("RASK_OIDC_ENABLED", "RASK_INSECURE_ALLOW_UNAUTHENTICATED")
#: The services whose boot calls `assert_authentication_configured`.
REFUSE_TO_BOOT_UNAUTHENTICATED = ("catalog", "lineage")


def _deployments_of(service: str, docs: list[dict]) -> list[dict]:
    """Deployments whose name carries the service's own name — the chart's naming convention."""
    return [d for d in docs if d.get("kind") == "Deployment" and service in d["metadata"]["name"]]


@pytest.mark.parametrize("auth_enabled", ["true", "false"])
def test_every_such_service_is_given_an_answer_by_the_chart(auth_enabled: str) -> None:
    docs = _rendered_docs(f"auth.enabled={auth_enabled}")
    unanswered: list[str] = []

    for service in REFUSE_TO_BOOT_UNAUTHENTICATED:
        deployments = _deployments_of(service, docs)
        assert deployments, f"{service} renders no Deployment"
        for deployment in deployments:
            env = {e["name"] for container in deployment["spec"]["template"]["spec"]["containers"] for e in (container.get("env") or [])}
            if not env & set(SATISFIES):
                unanswered.append(deployment["metadata"]["name"])

    assert not unanswered, (
        f"with auth.enabled={auth_enabled} these Deployments render NEITHER {SATISFIES[0]} nor "
        f"{SATISFIES[1]}, so their boot assertion refuses and they crash-loop: {sorted(unanswered)}"
    )
