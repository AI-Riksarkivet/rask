"""The interactive AI-assist routes must have a door, like every router beside them.

open_fastapi-audit — "The annotator's interactive AI-assist plane is three routes with no subject,
no checker and no catalog delegation — it spends model compute for any caller who can reach the pod".

`assist.py` declared `router = APIRouter(prefix="/api", tags=["assist"])` with no `dependencies=`,
and `grep -n 'CurrentSubject\\|CheckerDep\\|RawBearerToken' assist.py` returned nothing — the module
did not import `annotator.api.security` at all. Its siblings (projects, members, tasks,
project_events) all do.

WHAT IT IS AND IS NOT. This is not the flows situation, and the finding says so: there is no gateway
row for `/api/assist`, and the browser path gates first in `assist.remote.ts`. The reachable callers
are in-namespace pods — `chart/templates/network-policy.yaml` grants plain intra-namespace ingress —
and anyone port-forwarding. So it is defence in depth rather than an open edge. It still matters:
the POST reads a corpus unit and drives a model backend, spending GPU for a caller nobody checked,
with no audit row written.

WHY `can_write_data` ON THE UNIT'S TABLE. An assist prediction is a PROPOSED WRITE — it comes back
as `status="prediction"`, `source="model:<name>"` rows the annotator renders and a reviewer accepts,
which is the same provenance path a batch deriver's output takes. Gating it on the read rung would
let anyone who may look at a corpus spend model compute against it and queue work for its reviewers.

The two GETs disclose the estate's model-backend topology (`/assist/producers`) and a task's ontology
(`/assist/generation-schema`), so they sit behind the same authenticated floor even though they touch
no unit — hence the router-level dependency, which is the reference's own rule: apply at the router
level when every route in the group needs it, "cheaper to read and harder to forget".
"""

from __future__ import annotations

from typing import Any

import pytest
from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import assist as assist_ep
from annotator.api.v1.endpoints.assist import router
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import register_handlers
from service_kit.media.config import Settings
from service_kit.media.deps import get_state


class _Binding:
    table = "documents"


class _Declared:
    document = _Binding()


class _Descriptor:
    declared = _Declared()


class _Handle:
    """The minimum `require_assist` reads: an id, a settings object and a document binding.

    A fake rather than a real dataset because the gate must be provable without a corpus on disk —
    the whole point is that the check happens BEFORE any unit is read.
    """

    id = "corpus-1"
    descriptor = _Descriptor()

    def __init__(self) -> None:
        self.settings = Settings()


def _app(*, allow: bool, record: list[dict[str, Any]] | None = None, subject: str = "gina") -> FastAPI:
    """An app whose FGA checker answers `allow` and records what it was asked."""

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        if record is not None:
            record.append({"user": user, "relation": relation, "obj": obj})
        return allow

    handle = _Handle()

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)  # ForbiddenError -> problem+json 403, as in the real service
    app.dependency_overrides[get_checker] = lambda: checker
    # The VERIFIED subject. Overriding `current_subject` (not a header) is the point: with OIDC on
    # there is no header path to this value at all.
    app.dependency_overrides[current_subject] = lambda: subject
    app.dependency_overrides[get_state] = lambda: handle
    return app


@pytest.fixture(autouse=True)
def _resolve_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """`dataset_handle` opens Lance under a lock; the gate must be testable with no corpus present."""
    monkeypatch.setattr(assist_ep, "dataset_handle", lambda state, dataset=None: state)


BODY = {"producer": "sam", "prompt": "a line of text"}


def test_the_router_declares_a_door_at_all() -> None:
    """The structural claim, and the one the finding actually makes: no `dependencies=` on the
    router meant every route below it was reachable with no subject and no check."""
    assert router.dependencies, (
        "assist's router declares no dependencies — its three routes take no verified subject and no "
        "FGA checker, while every sibling router in this service has a door"
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/assist/doc-1/0/0"),
        ("GET", "/api/assist/producers"),
        ("GET", "/api/assist/generation-schema?task_id=t1"),
    ],
)
def test_a_caller_without_the_relation_is_refused(method: str, path: str) -> None:
    """403 for a caller the checker denies — on all three routes, including the two GETs whose
    disclosure is topology rather than data."""
    client = TestClient(_app(allow=False))
    response = client.request(method, path, json=BODY if method == "POST" else None)
    assert response.status_code == 403, (
        f"{method} {path} answered {response.status_code} for a denied caller — this route reads a corpus unit and spends model compute"
    )


def test_the_check_names_the_WRITE_rung_on_the_units_table() -> None:
    """A prediction is a proposed write, so the rung is `can_write_data`, and the object is the
    corpus table the annotations plane is governed by — not a second naming scheme."""
    asked: list[dict[str, Any]] = []
    client = TestClient(_app(allow=False, record=asked))
    client.post("/api/assist/doc-1/0/0", json=BODY)
    assert asked, "no FGA check was made at all"
    assert asked[0]["relation"] == "can_write_data", f"assist checked {asked[0]['relation']}"
    assert asked[0]["obj"].startswith("table:"), f"assist checked a non-table object: {asked[0]['obj']}"
    assert asked[0]["user"] == "gina"
