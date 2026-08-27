"""The prod overlay's HA posture: what is scaled, what is bounded against drains, what is spread.

open_fastapi-audit — "No PodDisruptionBudget for any front-door fleet service except the gateway, and
values-prod leaves all of them single-replica".

THE FINDING ARGUES ITSELF DOWN TWICE and this file keeps only what survives.

**A PDB over a single replica is the WRONG fix, not the missing one.** `ha.yaml`'s own header says a
1-replica `minAvailable: 1` PDB "would block every voluntary drain", which is why the zones get none
below `replicas > 1`. So the order matters: scale first, bound second. `test_no_single_replica_
deployment_carries_a_disruption_budget` is here to stop the inverse being "fixed" in later.

**The service the finding calls its "genuine residue" cannot be scaled, and this is where that gets
recorded.** It says notifications is the sharpest case — every zone polls its bell — and cites
values.yaml arguing that replicas are safe there: Dapr placement spreads actor ids, and the bus
subscription's `queueGroupName` makes replicas a competing-consumer group. Both true, and both about
the ACTOR and BUS ingresses. The finding never reaches the third one. The cron reconciler's overlap
guard is an `asyncio.Lock` — per process — so at two replicas both pods tick, both read the same
un-advanced cursor and both walk the same rows: double the FGA and actor load, silently, exactly when
lineage or the sidecar is already the slow thing. `test_invariants.py` already rules on this and
states what would unblock it (a cross-pod guard that SKIPS; not an actor, which queues). Owner call
2026-08-27: leave it at one replica and record the refutation rather than delete a reasoned
constraint. The availability gap the finding names is real and stays open, with its exit condition
written down.

**`flows` is a documented v0 constraint, not a chart defect.** `.docker/flows.dockerfile` records that
its run store is a process-local dict, so a second worker would serve `GET /flows/runs/{id}` from a
process that never saw the run. It stays at one replica and there is a test saying so, because the
next sweep reading "four of five were scaled" would otherwise finish the job.

What survives is the availability posture of the services that CAN scale, plus one gap the finding
spots in passing and is the sharpest thing in it: `fleet.yaml` is the one template that never calls
`lance.spreadConstraints`, so the prod `replicas: 2` on the GATEWAY — the ingress every request in the
estate passes through — could put both pods on one node, and its PDB then buys nothing. That is the
exact failure the helper's own docstring records.

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

from test_invariants import _first_party_deployments  # noqa: E402


REPO = pathlib.Path(__file__).resolve().parents[2]

#: The dummy values `scripts/prod_render_check.sh` uses. They satisfy the chart's fail-closed prod
#: guards (appToken / age password / rustfs key / registry), which is also how this render proves those
#: guards do not block a legitimate prod install.
_PROD_ARGS = [
    "--set", "image.catalog.tag=v0",
    "--set", "frontend.image.tag=v0",
    "--set", "dapr.appToken=ci-dummy-token-0000000000",
    "--set", "age.password=ci-dummy-pw",
    "--set", "rustfs.secretKey=ci-dummy-key",
    "--set", "backups.volumeSnapshot.snapshotClassName=csi-snapclass",
    "--set", "ingress.host=lance.example.com",
    "--set", "frontend.oidc.sessionSecret=ci-dummy-session-secret-at-least-32-chars",
    "--set", "frontend.oidc.publicIssuer=https://auth.example.com/dex",
    "--set", "frontend.oidc.publicOrigin=https://lance.example.com",
    "--set", "image.repository=ghcr.io/example/rask",
]  # fmt: skip

#: Documented as safe to scale at their own definition site, and each for a stated reason — ingest
#: (its run store is an explicitly NON-authoritative read-side index that falls back to the durable
#: workflow engine on a miss), controlplane (a read-only k8s client for Project CRs) and compute (a
#: stateless proxy over the Ray dashboard).
#:
#: `notifications` IS NOT HERE, and its absence is the finding's own headline being refuted — see the
#: module docstring. It is enforced by `test_invariants.py::test_notifications_stays_single_replica_
#: while_its_single_flight_lock_is_process_local`, which is not restated here: one claim, one gate.
SCALABLE_IN_PROD = ("ingest", "controlplane", "compute")


def _prod_docs() -> list[dict]:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not pathlib.Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(REPO / "chart"), "-f", str(REPO / "chart/values-prod.yaml"), *_PROD_ARGS]
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603
    return [doc for doc in yaml.safe_load_all(out) if isinstance(doc, dict)]


_DOCS = _prod_docs()
_OURS = _first_party_deployments(_DOCS)
_PDB_COMPONENTS = {
    (d.get("spec", {}).get("selector", {}).get("matchLabels", {}) or {}).get("app.kubernetes.io/component")
    for d in _DOCS
    if d.get("kind") == "PodDisruptionBudget"
} - {None}


def _component(doc: dict) -> str:
    return (doc["spec"]["template"]["metadata"].get("labels") or {}).get("app.kubernetes.io/component", doc["metadata"]["name"])


def _replicas(doc: dict) -> int:
    return int(doc["spec"].get("replicas", 1))


assert _OURS, "the prod overlay rendered no first-party Deployment — this file would pass vacuously"


@pytest.mark.parametrize("service", SCALABLE_IN_PROD)
def test_a_service_documented_as_scalable_is_actually_scaled_in_prod(service: str) -> None:
    """The residue the finding keeps: values.yaml argues these scale safely, values-prod leaves them at 1.

    notifications is the sharpest of them — every zone polls its bell, and one replica makes it a
    single point of failure for the duration of a node drain on a cluster upgrade.
    """
    doc = next((d for d in _OURS if _component(d) == service), None)
    assert doc is not None, f"{service} does not render in the prod overlay"
    assert _replicas(doc) >= 2, (
        f"{service} runs a single replica in prod, so a node drain is a full outage window for it — while its own values.yaml entry argues it scales safely"
    )


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


def test_every_multi_replica_deployment_is_bounded_against_a_drain() -> None:
    """A replica bump with no PDB lets a node drain take every replica at once."""
    unbounded = sorted(_component(d) for d in _OURS if _replicas(d) > 1 and _component(d) not in _PDB_COMPONENTS)
    assert not unbounded, f"these run multiple replicas in prod with no PodDisruptionBudget: {unbounded}"


def test_no_single_replica_deployment_carries_a_disruption_budget() -> None:
    """The inverse hazard, which ha.yaml's header records: `minAvailable: 1` over one replica blocks
    EVERY voluntary drain — a node cordon then hangs instead of proceeding."""
    blocking = sorted(_component(d) for d in _OURS if _replicas(d) == 1 and _component(d) in _PDB_COMPONENTS)
    assert not blocking, f"these have a PDB at one replica, so a node drain can never complete: {blocking}"


def test_every_multi_replica_deployment_is_SPREAD_across_nodes() -> None:
    """A PDB bounds how many pods may be evicted; only a spread constraint stops them sharing a node.

    `fleet.yaml` is the one template that never calls `lance.spreadConstraints`, and the gateway — the
    ingress every request in the estate passes through — is what runs two replicas there. Both on one
    node, and the node's failure takes the whole estate down with a PDB that permitted nothing.
    """
    flat = sorted(_component(d) for d in _OURS if _replicas(d) > 1 and not d["spec"]["template"]["spec"].get("topologySpreadConstraints"))
    assert not flat, f"these run multiple replicas with no topologySpreadConstraints, so both pods may land on one node: {flat}"
