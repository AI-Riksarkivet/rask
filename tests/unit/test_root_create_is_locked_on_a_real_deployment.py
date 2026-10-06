"""Root namespace creation is locked wherever the deployment is real, without anyone remembering to.

§F2-11. The control existed and was an OPT-IN: `values.yaml` shipped `auth.lockRootCreate: false` and
only `values-prod.yaml` set it true, so every estate that did not think about it let ANY authenticated
subject mint a top-level namespace. Measured on the live catalog 2026-09-07 —
`LANCE_FGA_LOCK_ROOT_CREATE=false` — which is why the row says "on THIS estate, not merely by chart
default".

THE DEFAULT KEY WAS THE DEFECT, not the value. `hasKey` finds a key whether or not anyone chose it,
so a shipped `false` beat any derivation that might have been added later: the control could only ever
be armed by an operator who already knew to arm it. Removing the key is what lets the chart answer for
an estate that never mentioned it.

SAME SHAPE F2-9 ALREADY FIXED, one layer over. `prod-credentials.yaml` answers "is this a real
deployment?" on signals the deploy already depends on — `openbao.devMode`, and images pulled from a
registry off this node — rather than a flag someone must remember. That answer now lives in ONE
helper with two consumers, so the credential guard and the root-create lock cannot disagree about what
"real" means.

BOTH DIRECTIONS STAY OPERATOR-CONTROLLED. A real deployment that genuinely wants open self-serve says
so explicitly, and a local loop that wants the lock says so too. What is removed is the silent middle:
an estate that never expressed a preference no longer gets the unsafe one.
"""

from __future__ import annotations

import pathlib
import sys


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _rendered_docs  # noqa: E402


LOCK = "LANCE_FGA_LOCK_ROOT_CREATE"

#: A real deployment: the platform's sealed store, whose operator attests the keys it provisioned.
_REAL = (
    "openbao.devMode=false",
    "dex.enabled=false",
    "signing.provisioned=true",
    "nats.auth.provisioned=true",
)


def _lock_value(*sets: str) -> str | None:
    for doc in _rendered_docs(*sets):
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for env in container.get("env") or []:
                if env["name"] == LOCK:
                    return str(env.get("value"))
    return None


def test_a_REAL_deployment_is_LOCKED_without_anyone_arming_it() -> None:
    """THE GATE. Nothing in these values mentions the lock; the deployment being real is what arms it."""
    assert _lock_value(*_REAL) == "true", (
        "a real deployment renders root-create UNLOCKED — any authenticated subject may mint a "
        "top-level namespace, which is exactly the state measured live on 2026-09-07"
    )
