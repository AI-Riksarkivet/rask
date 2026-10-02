"""A verifier door admits exactly the service accounts of the pods that call it ([[LH-220]], D1).

A service authenticates as the ServiceAccount its pod runs as: it projects a token for each door audience
it calls, and the door maps the token's full username to the subject its grants name (`RASK_SA_SUBJECTS`).
The map and the projections are rendered from one set of values, and this holds them together. A caller
missing from the map is refused 401 on every call, which the lineage emitter swallows by design, so the
data lands and the graph never hears of it (measured twice before D1: the trainer in 2026-07 and ingest on
2026-08-06). An account in the map that no pod runs as is a door left open to whoever can mint its token.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.unit.chart_render import REPO, env_of, render, render_text


#: The overlay `make k3s-up` deploys, which also renders the Ray head the default leaves out.
DEPLOYED = ("--set", "image.localImages=true", "-f", str(REPO / "chart/values-local.yaml"))

#: The producer's door also admits ingest's account, which no pod presents: clause 5's live proof mints a
#: 10-minute `rask-medallion` token for it to show a service caller is held to its own tenant's runs.
OPERATOR_MINTED = {"Deployment/rask-medallion-producer": {"rask-sa-ingest"}}

#: The identity a caller declares for each door it calls, where it declares one in its own env.
DECLARED = {"rask-catalog": "RASK_CATALOG_SERVICE_IDENTITY", "rask-lineage": "RASK_LINEAGE_SERVICE_IDENTITY"}


def _pod_specs() -> dict[str, dict]:
    pods: dict[str, dict] = {}
    for doc in render(*DEPLOYED):
        kind, name = doc.get("kind"), doc.get("metadata", {}).get("name")
        if kind in {"Deployment", "StatefulSet"}:
            pods[f"{kind}/{name}"] = doc["spec"]["template"]["spec"]
        elif kind == "RayCluster":
            pods[f"{kind}/{name}"] = doc["spec"]["headGroupSpec"]["template"]["spec"]
    return pods


def _projected_audiences(spec: dict) -> set[str]:
    return {
        source["serviceAccountToken"]["audience"]
        for volume in spec.get("volumes") or []
        for source in (volume.get("projected") or {}).get("sources") or []
        if (source.get("serviceAccountToken") or {}).get("audience")
    }


def test_each_door_maps_exactly_the_service_accounts_that_project_its_audience() -> None:
    pods = _pod_specs()
    namespace = "default"
    doors = {workload: env_of(container) for workload, spec in pods.items() for container in spec["containers"] if "RASK_SA_SUBJECTS" in env_of(container)}
    assert {"Deployment/rask-catalog", "Deployment/rask-lineage", "Deployment/rask-medallion-producer"} <= set(doors), (
        f"a D1 door renders no subject map: {sorted(doors)}"
    )

    problems: dict[str, object] = {}
    for door, env in doors.items():
        audience = env["RASK_SA_AUDIENCE"]
        callers = {spec["serviceAccountName"]: spec for workload, spec in pods.items() if workload != door and audience in _projected_audiences(spec)}
        expected = set(callers) | OPERATOR_MINTED.get(door, set())
        subjects: dict[str, str] = json.loads(env["RASK_SA_SUBJECTS"])
        admitted = {key.removeprefix(f"system:serviceaccount:{namespace}:") for key in subjects}
        if admitted != expected:
            problems[door] = {"admitted, no caller": sorted(admitted - expected), "caller, not admitted": sorted(expected - admitted)}
        for account, spec in callers.items():
            declared = {name for c in spec["containers"] if (name := env_of(c).get(DECLARED.get(audience, "")))}
            subject = subjects.get(f"system:serviceaccount:{namespace}:{account}")
            if declared and subject not in declared:
                problems[f"{door} <- {account}"] = f"admitted as {subject!r}, declares {sorted(declared)}"

    assert not problems, f"a door's subject map disagrees with the pods that call it: {problems}"


@pytest.mark.parametrize(
    "identity",
    [
        "medallion.producer.serviceIdentity",
        "medallion.stageRunners[bronze-to-silver].serviceIdentity",
        "maintenance.catalogServiceIdentity",
        "medallion.train.trainerIdentity",
        "frontend.serviceIdentity",
    ],
)
def test_an_identity_that_names_no_subject_fails_the_render(identity: str, tmp_path: Path) -> None:
    """Mapped to a blank subject, an account authenticates as the empty principal, and so does every other one mapped there."""
    if identity.startswith("medallion.stageRunners"):
        runners = yaml.safe_load((REPO / "chart/values.yaml").read_text())["medallion"]["stageRunners"]
        blanked = [{**runner, "serviceIdentity": ""} if runner["name"] == "bronze-to-silver" else runner for runner in runners]
        overlay = tmp_path / "blank.yaml"
        overlay.write_text(yaml.safe_dump({"medallion": {"stageRunners": blanked}}))
        extra: tuple[str, ...] = ("-f", str(overlay))
    else:
        extra = ("--set", f"{identity}=")

    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_text(*DEPLOYED, *extra)

    assert f"{identity} is blank" in refused.value.stderr, refused.value.stderr
