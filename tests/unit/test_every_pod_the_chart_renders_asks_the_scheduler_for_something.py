"""Every container the chart renders must carry requests/limits — Jobs and init containers included.

[[XC-038]]. The row said the four infra subcharts (GreptimeDB, NATS, Perses, the Dapr control plane)
get no resources from `values-prod.yaml`. MEASURED 2026-09-16 against the rendered prod overlay: all
four DO carry them — `chart/values.yaml` supplies every one (greptimedb-standalone:3013, nats:2416,
perses:3028, dapr:2478) and Helm merges the overlay ON TOP of the base rather than instead of it, so
reading `values-prod.yaml` alone answers a different question than "is this container bounded".

WHAT THE SAME RENDER DOES SHOW is 9 of 54 containers unbounded, and not one of them is a subchart:
they are the chart's own bootstrap Jobs, its two backup CronJobs and the two `wait-age` busybox inits.
That matters more than the tier-sizing the row asked for, because a ResourceQuota on the namespace —
the ordinary prod control, and the reason anyone audits this — REJECTS a pod whose containers declare
no requests. The fleet would come up and the release would never converge, because the bootstrap Jobs
that create the buckets, the streams and the OpenFGA schema would never be admitted.

FOUR JOB TEMPLATES ALREADY DO THIS (bootstrap-admin, dapr-inject-sweep, greptimedb-ttl-job,
kueue-queues), each with an inline block sized to its own work. This gate is what makes the fifth
inherit the rule instead of relearning it.

BOTH OVERLAYS ARE RENDERED because neither alone sees every Job: the prod overlay renders the two
backup CronJobs and the default overlay renders the OpenBao seed. A container is covered if either
render produces it.
"""

from __future__ import annotations

import functools
import pathlib
import shutil
import subprocess

import pytest
import yaml
from chart_yaml import FAST_LOADER

from tests.unit.chart_render import ESO_ARGS


REPO = pathlib.Path(__file__).resolve().parents[2]

_OIDC_ARGS = [
    *ESO_ARGS,
    "--set-string", "frontend.oidc.publicIssuer=https://auth.example.com/dex",
    "--set-string", "frontend.oidc.publicOrigin=https://lance.example.com",
]  # fmt: skip

#: The fail-closed prod guards' dummy inputs, same set `scripts/prod_render_check.sh` uses.
_PROD_ARGS = [
    "--set", "image.catalog.tag=v0",
    "--set", "frontend.image.tag=v0",
    "--set", "signing.provisioned=true",
    "--set", "nats.auth.provisioned=true",
    "--set", "backups.volumeSnapshot.snapshotClassName=csi-snapclass",
    "--set", "ingress.host=lance.example.com",
    "--set", "image.repository=ghcr.io/example/rask",
]  # fmt: skip

_DEFAULT_ARGS = ["--set", "image.localImages=true", "--set", "minio.enabled=true"]


@functools.cache
def _render(*extra: str) -> list[dict]:
    """Cached on the verbatim flag tuple: five assertions here want three renders, not six.

    [[XC-037]] counts this suite's hand-rolled `helm template` subprocesses; a new one that spawned a
    process per assertion would be a row growing while it is open.
    """
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not pathlib.Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(REPO / "chart"), *_OIDC_ARGS, *extra]
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603
    return [doc for doc in yaml.load_all(out, Loader=FAST_LOADER) if isinstance(doc, dict)]


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
    "prod": ["-f", str(REPO / "chart/values-prod.yaml"), *_PROD_ARGS],
}


@pytest.mark.parametrize("overlay", sorted(_OVERLAYS))
def test_no_container_asks_the_scheduler_for_nothing(overlay: str) -> None:
    bare = _unbounded(_render(*_OVERLAYS[overlay]))

    assert not bare, (
        f"the {overlay} overlay renders unbounded containers {bare} — under a namespace ResourceQuota "
        "every one of those pods is REJECTED at admission, and the ones here are the bootstrap Jobs the "
        "release needs to converge"
    )
