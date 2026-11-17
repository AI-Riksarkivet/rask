"""Every container the chart renders must carry requests/limits — Jobs and init containers included.

[[XC-038]]. The row said the infra subcharts (GreptimeDB, NATS, Perses) get no resources from
`values-prod.yaml`. MEASURED 2026-09-16 against the rendered prod overlay: they DO carry them —
`chart/values.yaml` supplied every one (greptimedb-standalone:3013, nats:2416, perses:3028) and Helm merges the overlay ON TOP of the base rather than instead of it, so
reading `values-prod.yaml` alone answers a different question than "is this container bounded".

WHAT THE SAME RENDER DOES SHOW is 9 of 54 containers unbounded, and not one of them is a subchart:
they are the chart's own bootstrap Jobs, its two backup CronJobs and the two `wait-age` busybox inits.
That matters more than the tier-sizing the row asked for, because a ResourceQuota on the namespace —
the ordinary prod control, and the reason anyone audits this — REJECTS a pod whose containers declare
no requests. The fleet would come up and the release would never converge, because the bootstrap Jobs
that create the buckets, the streams and the OpenFGA schema would never be admitted.

THE JOB TEMPLATES ALREADY DO THIS, each with an
inline block sized to its own work. This gate is what makes the next one inherit the rule instead of
relearning it.

BOTH OVERLAYS ARE RENDERED because neither alone sees every Job: the prod overlay renders the two
backup CronJobs and the default overlay renders the OpenBao seed. A container is covered if either
render produces it.
"""

from __future__ import annotations

import pathlib

import pytest

from tests.chart.chart_render import PROD_ARGS, render


REPO = pathlib.Path(__file__).resolve().parents[2]

_DEFAULT_ARGS = ["--set", "image.localImages=true", "--set", "objectStore.enabled=true"]


def _render(*extra: str) -> list[dict]:
    """The chart under ``extra``, through the shared render, which caches on the flag tuple."""
    return list(render(*extra))


def _pod_specs(docs: list[dict]) -> list[tuple[str, dict]]:
    """``("<Kind>/<name>", podSpec)`` for every workload shape the chart renders."""
    found: list[tuple[str, dict]] = []
    for doc in docs:
        kind = doc.get("kind")
        name = doc.get("metadata", {}).get("name", "?")
        if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
            found.append((f"{kind}/{name}", doc["spec"]["template"]["spec"]))
        elif kind == "CronJob":
            found.append((f"{kind}/{name}", doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]))
    return found


def _unbounded(docs: list[dict]) -> list[str]:
    """Every container that declares neither a request nor a limit, init containers included.

    Init containers count: the scheduler admits a pod against ``max(init) ⊕ sum(containers)``, and a
    ResourceQuota that requires requests rejects the POD, not the container.
    """
    bare: list[str] = []
    for where, spec in _pod_specs(docs):
        for key in ("initContainers", "containers"):
            for container in spec.get(key) or []:
                resources = container.get("resources") or {}
                if not (resources.get("requests") or resources.get("limits")):
                    suffix = " [init]" if key == "initContainers" else ""
                    bare.append(f"{where}/{container['name']}{suffix}")
    return bare


_OVERLAYS = {
    "default": _DEFAULT_ARGS,
    "prod": [*PROD_ARGS],
}


@pytest.mark.parametrize("overlay", sorted(_OVERLAYS))
def test_no_container_asks_the_scheduler_for_nothing(overlay: str) -> None:
    bare = _unbounded(_render(*_OVERLAYS[overlay]))

    assert not bare, (
        f"the {overlay} overlay renders unbounded containers {bare} — under a namespace ResourceQuota "
        "every one of those pods is REJECTED at admission, and the ones here are the bootstrap Jobs the "
        "release needs to converge"
    )
