"""Every door that STARTS work refuses it while this replica is draining — and a new one cannot slip in.

The unit behaviour lives in `packages/service-kit/tests/test_draining_refuses_admission.py`. This is
the coverage half, and it is the half that rots: the dependency can be perfect and a door added next
month simply never asks for it. Nothing else would notice, because an ungated door behaves correctly
in every test that is not about shutdown.

The split matters and is asserted per door. An HTTP door answers 503 so the caller can retry; a
SIDECAR-delivered door answers RETRY, because a 503 at a Dapr sidecar is read as a delivery failure —
which happens to retry today and would silently become a drop the moment a resiliency policy treated
5xx as terminal. Getting it backwards on the subscription is the expensive direction: these topics
carry no DLQ, so a dropped trigger cancels a bronze→silver→gold run with nothing reporting it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


API = Path(__file__).resolve().parents[1] / "src" / "medallion" / "api"

#: The doors that CREATE work. `/produce` seeds bronze and fires the cascade head, `/ingest-media`
#: drives the media chain, `/train` spends GPU. A promotion DECISION is deliberately absent: it
#: completes work already held, and refusing it while draining would strand a promotion an approver
#: has already answered — the opposite of the failure this gate exists for.
HTTP_RUN_DOORS = {
    ("produce.py", "produce"),
    ("ingest_media.py", "ingest_media"),
    ("train.py", "train"),
}

#: Sidecar-delivered. Dapr does not consult a readiness probe, so these are the ones the flag could
#: never have protected before.
SUBSCRIPTION_DOORS = {("bronze_arrival.py", "on_bronze_arrival")}


def _fn(module: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse((API / module).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {module} — the door was renamed and this gate now asserts nothing")


def _mentions(node: ast.AST, symbol: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == symbol for n in ast.walk(node))


class TestEveryHttpRunDoorIsGated:
    @pytest.mark.parametrize(("module", "name"), sorted(HTTP_RUN_DOORS))
    def test_it_depends_on_the_http_refusal(self, module: str, name: str) -> None:
        node = _fn(module, name)
        gated = any(_mentions(d, "refuse_when_draining") for d in node.decorator_list)
        assert gated, f"{module}::{name} starts work and does not refuse while draining"

    @pytest.mark.parametrize(("module", "name"), sorted(HTTP_RUN_DOORS))
    def test_it_does_not_use_the_subscription_answer(self, module: str, name: str) -> None:
        """RETRY is a pub/sub verdict. Returned from an HTTP route it is a 200 body saying "RETRY",
        which every caller reads as success."""
        node = _fn(module, name)
        assert not _mentions(node, "retry_when_draining"), f"{module}::{name} is HTTP and must answer 503"


class TestEverySubscriptionAsksForRedelivery:
    @pytest.mark.parametrize(("module", "name"), sorted(SUBSCRIPTION_DOORS))
    def test_it_depends_on_the_retry_verdict(self, module: str, name: str) -> None:
        node = _fn(module, name)
        assert _mentions(node, "retry_when_draining"), f"{module}::{name} handles deliveries while draining"

    @pytest.mark.parametrize(("module", "name"), sorted(SUBSCRIPTION_DOORS))
    def test_it_does_not_raise_503_at_a_sidecar(self, module: str, name: str) -> None:
        node = _fn(module, name)
        assert not _mentions(node, "refuse_when_draining"), f"{module}::{name} is sidecar-delivered — a 503 is a DELIVERY failure to Dapr, not an answer"


class TestTheGateCannotGoVacuous:
    """Both failure modes of a name-list gate: the names stop resolving, or a door is added and the
    list is not."""

    def test_every_named_door_still_exists(self) -> None:
        for module, name in HTTP_RUN_DOORS | SUBSCRIPTION_DOORS:
            _fn(module, name)

    def test_no_unlisted_router_post_creates_work_ungated(self) -> None:
        """Catches the door added next month. Every `@router.post` in the api package is either in a
        list above, or must justify itself by carrying one of the two dependencies anyway.

        Three categories are legitimate here, and the expected list must say WHICH for each entry:
        a door that starts work is gated; a door that completes work already held is listed; a door
        that STOPS work is listed, because refusing it while draining removes the lever precisely
        when it is needed."""
        listed = {name for _, name in HTTP_RUN_DOORS | SUBSCRIPTION_DOORS}
        ungated: list[str] = []
        for path in sorted(API.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or node.name in listed:
                    continue
                posts = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "post" for d in node.decorator_list)
                if posts and not any(_mentions(d, "refuse_when_draining") or _mentions(d, "retry_when_draining") for d in node.decorator_list):
                    ungated.append(f"{path.name}::{node.name}")
        # `train.py::terminate_train` is the THIRD category this gate did not have. B6 refuses a door
        # that STARTS work a draining pod cannot finish, and `promotions.py::decide` is listed because
        # it COMPLETES work already held. A terminate does neither — it stops work — and gating it
        # would take the runaway-stopping lever away at exactly the moment an operator reaches for it,
        # since a rollout is when runaways are noticed. It is also idempotent against a pod that
        # leaves mid-call: the workflow is durable and the terminate is recorded by the sidecar.
        # `mover_ops.py::terminate_stage` and `stage_ops.py::terminate_stage` join `terminate_train` in
        # the third category: they STOP work. The mover-side one is additionally sidecar-unreachable —
        # it is called by the producer over ClusterIP, not delivered by Dapr — so B6's admission
        # question does not even apply to it.
        assert ungated == [
            "mover_ops.py::terminate_stage",
            "promotions.py::decide",
            "stage_ops.py::terminate_stage",
            "train.py::terminate_train",
        ], (
            "a new POST door appeared that neither this gate lists nor refuses while draining: "
            f"{ungated}. If it starts work, gate it; if it completes work already held (like the "
            "promotion decision), add it to this expected list with that reasoning."
        )
