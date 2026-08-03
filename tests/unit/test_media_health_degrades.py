"""``/api/health`` must answer 200 with no corpus dataset — it is a PROBE, not a dataset read.

Regression pin for live-proof 2026-07-28 defect 4. The endpoint used to resolve the default dataset
before it could answer anything, so a deployment with an empty corpus volume — now the chart's DEFAULT
(``explorer.corpus.mode=emptyDir``, because the old unconditional hostPath wedged every fresh cluster)
answered::

    404 {"detail": "dataset 'transcripts_v2' not found under /media-corpus"}

to the one endpoint the media AND annotator zones poll on every single page load (both zones' `api/**`
BFF catch-all proxies to the viewer). Measured live through both: a console 404 per page load, and the
zone reporting "backend unreachable" for a backend that was Running and healthy and merely had nothing
loaded. A probe that cannot tell "service down" from "no corpus" is worse than no probe.

Driven over HTTP at the boundary with the SAME problem+json handlers the service registers, so a
regression that reinstates the 404 fails here rather than in a browser console.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from viewer.api.v1.endpoints.system import router as system_router

from service_kit.exceptions import register_handlers
from service_kit.media.deps import get_state
from service_kit.media.state import AppState


class _NoDatasetRegistry:
    """A registry whose default dataset does not exist — an un-loaded corpus volume."""

    def default(self) -> Any:
        from service_kit.lancekit.registry import UnknownDatasetError

        raise UnknownDatasetError("dataset 'transcripts_v2' not found under /media-corpus")

    def get(self, _dataset_id: str) -> Any:
        return self.default()


class _DeadEncoders:
    """The default encoder posture: nothing deployed, so every ping refuses."""

    def get(self, url: str, timeout: float = 0.0) -> Any:
        raise ConnectionError(f"All connection attempts failed to {url}")


@pytest.fixture
def client() -> Iterator[TestClient]:
    state = AppState(http=_DeadEncoders(), registry=_NoDatasetRegistry())
    app = FastAPI()
    register_handlers(app)
    app.include_router(system_router)
    app.dependency_overrides[get_state] = lambda: state
    with TestClient(app) as c:
        yield c


def test_health_is_200_with_no_dataset_loaded(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200, (
        f"/api/health returned {r.status_code} on a service with no corpus dataset: {r.text}. "
        "It is the media plane's liveness probe — the media + annotator zones poll it on every page "
        "load, so a non-200 here is a console error per load and a UI that blames the network."
    )


def test_health_reports_the_dataset_gap_without_failing(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["db"] is None, "no dataset resolved, so there are no db facts to report"
    assert body["db_error"], "db=None without db_error leaves the UI unable to say WHY (it would guess 'unreachable')"
    assert "transcripts_v2" in body["db_error"], f"db_error must carry the backend's own reason, got {body['db_error']!r}"


def test_a_dataset_bound_endpoint_on_the_same_app_still_404s(client: TestClient) -> None:
    """The proof that the fix is scoped to the PROBE, not a blanket softening.

    Same app, same registry double: ``/api/columns`` reads a dataset, so its 404 is correct and stays.
    It is also the mechanical proof that this fixture reproduces the pre-fix failure — the identical
    resolution path that now degrades under ``/api/health`` still renders 404 problem+json here, which is
    exactly what every media/annotator page load used to get from the probe.
    """
    r = client.get("/api/columns")
    assert r.status_code == 404, f"a dataset READ with no dataset must still 404, got {r.status_code}"
    assert "transcripts_v2" in r.text


def test_encoder_pings_are_reported_even_with_no_dataset(client: TestClient) -> None:
    """The capability half of the probe cannot depend on the dataset half.

    The media zone gates its Meaning/Hybrid search modes on `embed.ok`. When health 404'd, `current`
    stayed null and the modes fell back to "assume available" — offering a mode that 503s.
    """
    body = client.get("/api/health").json()
    for name in ("embed", "rerank"):
        assert body[name]["ok"] is False, f"{name} ping missing/true against a dead encoder"
        assert body[name]["error"], f"{name} ping must carry the failure reason"
