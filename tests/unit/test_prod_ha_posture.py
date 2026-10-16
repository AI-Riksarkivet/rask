"""The prod overlay's HA posture for the one service that must NOT be scaled: flows.

**`flows` is a documented v0 constraint, not a chart defect.** `.docker/flows.dockerfile` records that
its run store is a process-local dict, so a second worker would serve `GET /flows/runs/{id}` from a
process that never saw the run. It stays at one replica and there is a test saying so, because the
next sweep reading "four of five were scaled" would otherwise finish the job.

Rendered against `values-prod.yaml`, because none of this is observable in the default overlay: the
dev profile is single-replica everywhere by design.
"""

from __future__ import annotations

import pathlib
import sys


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _first_party_deployments  # noqa: E402

from tests.unit.chart_render import PROD_ARGS, render


#: The production overlay with the dummy inputs its fail-closed guards require, which is also how this render proves
#: those guards do not block a legitimate prod install.
_DOCS = list(render(*PROD_ARGS))
_OURS = _first_party_deployments(_DOCS)


def _component(doc: dict) -> str:
    return (doc["spec"]["template"]["metadata"].get("labels") or {}).get("app.kubernetes.io/component", doc["metadata"]["name"])


def _replicas(doc: dict) -> int:
    return int(doc["spec"].get("replicas", 1))


assert _OURS, "the prod overlay rendered no first-party Deployment — this file would pass vacuously"


def test_flows_stays_single_replica_until_its_run_store_is_durable() -> None:
    """NOT an oversight, and this test exists so it does not get "fixed".

    `.docker/flows.dockerfile`: "the run store is a process-local dict (v0), so a second worker would
    serve GET /flows/runs/{id} from a process that never saw the run". There is
    no durable engine to fall back to on a miss — a second replica would answer 404 for live runs.
    """
    doc = next((d for d in _OURS if _component(d) == "flows"), None)
    assert doc is not None, "flows does not render in the prod overlay"
    assert _replicas(doc) == 1, (
        "flows was scaled past one replica, but its run store is a process-local dict with no durable "
        "fallback — GET /flows/runs/{id} would 404 on the replica that did not accept the run"
    )
