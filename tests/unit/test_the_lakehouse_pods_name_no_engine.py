"""The engine is configuration of compute, and the lakehouse pods carry none of it (CP-056 step 3; CP-041).

Rendered, because the chart is the only path configuration reaches the estate.

With every engine off, no lakehouse pod's env names Ray, and nothing renders the planned-run lane; with the Ray
engine on (the local overlay, which renders the Ray head), the producer and every stage runner carry the lane's
whole wiring (the engines they invoke, the outcome door, the plan root, the sweep's binding, the control-lane
publisher), each door maps the Ray head's account to the trainer identity, compute's engine door maps every planner
and runs every task the planners register, and the Ray head carries the lakehouse allocator bounds. The one Ray
address is pinned by `test_invariants::test_every_ray_consumer_names_one_cluster_or_the_render_refuses`.

"Lakehouse pod" is every container of the lakehouse image (`lance-rest-catalog`) except the explorer's media trio,
whose Serve discovery names the Ray dashboard by design.
"""

from __future__ import annotations

import json
import re

from compute.config import ComputeSettings
from medallion.core.config import MedallionSettings
from tests.unit.chart_render import DEFAULT_ARGS, REPO, render


_ON = (*DEFAULT_ARGS, "-f", str(REPO / "chart/values-local.yaml"))
_OFF = (*DEFAULT_ARGS, "--set", "services.compute.engines.ray.enabled=false")
_MEDIA = {"viewer", "search", "annotator"}
_PLANNERS = ("rask-medallion-producer", "rask-bronze-to-silver", "rask-silver-to-gold", "rask-media-to-silver")


def _env(docs: tuple[dict, ...]) -> dict[str, dict[str, str]]:
    """`deployment/container` -> its literal env, for every first-party Deployment."""
    out: dict[str, dict[str, str]] = {}
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        for c in doc["spec"]["template"]["spec"]["containers"]:
            out[f"{doc['metadata']['name']}/{c['name']}"] = {e["name"]: str(e.get("value", "")) for e in c.get("env") or []}
    return out


def _lakehouse(docs: tuple[dict, ...]) -> list[str]:
    names = []
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        component = (doc["spec"]["template"]["metadata"].get("labels") or {}).get("app.kubernetes.io/component", "")
        for c in doc["spec"]["template"]["spec"]["containers"]:
            if "lance-rest-catalog" in str(c.get("image")) and component not in _MEDIA:
                names.append(f"{doc['metadata']['name']}/{c['name']}")
    return names


def _planner(env: dict[str, dict[str, str]], deployment: str) -> dict[str, str]:
    (key,) = [k for k in env if k.startswith(f"{deployment}/")]
    return env[key]


def test_a_lakehouse_pod_names_ray_only_through_an_enabled_engine_on_compute() -> None:
    off, on = render(*_OFF), render(*_ON)
    off_env, on_env = _env(off), _env(on)

    lakehouse = _lakehouse(off)
    assert {name.split("/")[0] for name in lakehouse} >= set(_PLANNERS), lakehouse
    named = {name: [f"{k}={v}" for k, v in off_env[name].items() if re.search("ray", f"{k}={v}", re.IGNORECASE)] for name in lakehouse}
    assert {name: hits for name, hits in named.items() if hits} == {}, "a lakehouse pod names Ray with no engine enabled"
    assert not any("MEDALLION_ENGINES" in env for env in off_env.values())
    assert "RASK_COMPUTE_ENGINE_TASKS" not in off_env["rask-compute/compute"]
    assert not [
        d for d in off if d.get("kind") == "Component" and d["spec"]["type"] == "bindings.cron" and d["metadata"]["name"] == "medallion-plan-sweep-cron"
    ]

    head_subject = {"system:serviceaccount:default:rask-sa-ray": "service-trainer"}
    registered: set[str] = set()
    reports: set[str] = set()
    for deployment in _PLANNERS:
        env = _planner(on_env, deployment)
        # Parsed by the setting that reads it, so a rendered key the model forbids fails here, not at a pod's boot.
        settings = MedallionSettings.model_validate(
            {
                "MEDALLION_ENGINES": json.loads(env["MEDALLION_ENGINES"]),
                "MEDALLION_COMPUTE_ENABLED": True,
                "MEDALLION_DEFAULT_ENGINE": env.get("MEDALLION_DEFAULT_ENGINE", "inprocess"),
            }
        )
        assert list(settings.engines) == ["ray"] and settings.engines["ray"].app_id == "compute", deployment
        assert "ray-cluster" in settings.engines["ray"].code_version, "a roll of the Ray image would re-attach to the old build's run"
        registered |= {task.task for task in settings.engines["ray"].tasks}
        reports.add(env.get("MEDALLION_OUTCOME_URL_BASE", ""))
        for name in ("MEDALLION_OUTCOME_URL_BASE", "MEDALLION_CONTROL_ROOT", "MEDALLION_PLAN_SWEEP_BINDING_NAME"):
            assert env.get(name), f"{deployment} lacks {name}: its planned runs cannot be reported, kept or swept"
        assert json.loads(env["RASK_SA_SUBJECTS"]).items() >= head_subject.items(), f"{deployment}'s door refuses the Ray head's report"
        if deployment != "rask-medallion-producer":
            assert settings.default_engine == "ray"
            assert env["MEDALLION_TRAINER_IDENTITY"] == "service-trainer" and env["MEDALLION_CONTROL_PUBSUB"].endswith(deployment.removeprefix("rask-"))
    door = on_env["rask-compute/compute"]
    compute = ComputeSettings.model_validate(
        {
            k: json.loads(v) if k in ("RASK_SA_SUBJECTS", "RASK_COMPUTE_ENGINE_TASKS", "RASK_COMPUTE_ENGINE_OUTCOME_BASES") else v
            for k, v in door.items()
            if k.startswith(("RASK_SA_", "RASK_COMPUTE_ENGINE"))
        }
    )
    assert registered and registered <= set(compute.engine_tasks["ray"]), "a planner registers a task compute's engine cannot run"
    assert reports <= set(compute.engine_outcome_bases), "compute refuses a planner's own outcome door"
    assert set(compute.sa_subjects.values()) >= {"service-medallion-producer", "service-bronze-to-silver", "service-silver-to-gold", "service-media-to-silver"}
    head = next(c for d in on if d.get("kind") == "RayCluster" for c in d["spec"]["headGroupSpec"]["template"]["spec"]["containers"])
    head_env = {e["name"]: e.get("value") for e in head.get("env") or []}
    assert {"MALLOC_ARENA_MAX", "ARROW_DEFAULT_MEMORY_POOL"} <= set(head_env), "the Ray head runs without the lakehouse allocator bounds"
