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


REPO = pathlib.Path(__file__).resolve().parents[2]
LOCK = "LANCE_FGA_LOCK_ROOT_CREATE"

#: A real deployment needs real credentials, or `prod-credentials.yaml` refuses the render first —
#: which is the OTHER half of the same signal working, and would make this gate unable to render.
_REAL = (
    "openbao.devMode=false",
    "dapr.appToken=a-real-app-token",
    "age.password=a-real-age-password",
    "minio.secretKey=a-real-rustfs-secret",
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


def test_the_gate_can_see_the_value() -> None:
    """A renamed env or a moved template would make every assertion below vacuous."""
    assert _lock_value() is not None, f"no {LOCK} rendered on the default profile — this gate is blind"


def test_the_chart_ships_NO_default_for_the_lock() -> None:
    """THE DEFECT ITSELF. A key in `values.yaml` is a key `hasKey` finds, so shipping one — at any
    value — puts the chart's opinion ahead of the derivation and restores the opt-in this closes."""
    text = (REPO / "chart/values.yaml").read_text(encoding="utf-8")
    offending = [line for line in text.splitlines() if line.strip().startswith("lockRootCreate:")]
    assert not offending, (
        f"chart/values.yaml ships a lockRootCreate default ({offending}) — with the key present the "
        "derivation never runs and an estate that never mentioned the lock silently gets the unsafe answer"
    )


def test_the_local_loop_stays_OPEN_for_self_serve() -> None:
    """The render this control must never break. `make k3s-up` is side-loaded images plus devMode, and
    a lock there would make the dev loop refuse the namespaces it exists to create."""
    assert _lock_value() == "false", "the local loop lost self-serve root creation"


def test_a_REAL_deployment_is_LOCKED_without_anyone_arming_it() -> None:
    """THE GATE. Nothing in these values mentions the lock; the deployment being real is what arms it."""
    assert _lock_value(*_REAL) == "true", (
        "a real deployment renders root-create UNLOCKED — any authenticated subject may mint a "
        "top-level namespace, which is exactly the state measured live on 2026-09-07"
    )


def test_an_EXPLICIT_choice_still_wins_in_BOTH_directions() -> None:
    """A derived default that cannot be overridden is a different defect. An operator who has thought
    about it must be able to say so either way, and be believed."""
    assert _lock_value(*_REAL, "auth.lockRootCreate=false") == "false", "a real deployment cannot opt OUT"
    assert _lock_value("auth.lockRootCreate=true") == "true", "a local estate cannot opt IN"
