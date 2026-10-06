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
import shutil
import subprocess
import sys

import pytest
import yaml


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chart_yaml import FAST_LOADER
from test_invariants import _first_party_deployments  # noqa: E402

from tests.unit.chart_render import ESO_ARGS


REPO = pathlib.Path(__file__).resolve().parents[2]

#: The dummy values `scripts/prod_render_check.sh` uses. They satisfy the chart's fail-closed prod
#: guards (appToken / age password / rustfs key / registry), which is also how this render proves those
#: guards do not block a legitimate prod install.
_PROD_ARGS = [
    "--set", "image.catalog.tag=v0",
    "--set", "frontend.image.tag=v0",
    "--set", "signing.provisioned=true",
    "--set", "nats.auth.provisioned=true",
    "--set", "backups.volumeSnapshot.snapshotClassName=csi-snapclass",
    "--set", "ingress.host=lance.example.com",
    *ESO_ARGS,
    "--set", "frontend.oidc.publicIssuer=https://auth.example.com/dex",
    "--set", "frontend.oidc.publicOrigin=https://lance.example.com",
    "--set", "image.repository=ghcr.io/example/rask",
]  # fmt: skip


def _prod_docs() -> list[dict]:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not pathlib.Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(REPO / "chart"), "-f", str(REPO / "chart/values-prod.yaml"), *_PROD_ARGS]
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603
    return [doc for doc in yaml.load_all(out, Loader=FAST_LOADER) if isinstance(doc, dict)]


_DOCS = _prod_docs()
_OURS = _first_party_deployments(_DOCS)


def _component(doc: dict) -> str:
    return (doc["spec"]["template"]["metadata"].get("labels") or {}).get("app.kubernetes.io/component", doc["metadata"]["name"])


def _replicas(doc: dict) -> int:
    return int(doc["spec"].get("replicas", 1))


assert _OURS, "the prod overlay rendered no first-party Deployment — this file would pass vacuously"


def test_flows_stays_single_replica_until_its_run_store_is_durable() -> None:
    """NOT an oversight, and this test exists so it does not get "fixed".

    `.docker/flows.dockerfile`: "the run store is a process-local dict (v0), so a second worker would
    serve GET /flows/runs/{id} from a process that never saw the run". Unlike ingest's store, there is
    no durable engine to fall back to on a miss — a second replica would answer 404 for live runs.
    """
    doc = next((d for d in _OURS if _component(d) == "flows"), None)
    assert doc is not None, "flows does not render in the prod overlay"
    assert _replicas(doc) == 1, (
        "flows was scaled past one replica, but its run store is a process-local dict with no durable "
        "fallback — GET /flows/runs/{id} would 404 on the replica that did not accept the run"
    )
