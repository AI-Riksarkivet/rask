"""Every readiness probe the product chart renders asks `/readyz`, never a static badge.

A readiness probe answers whether a pod can serve now, so it has to report draining: `lakehouse.probes`' `/readyz`
answers 503 `shutting_down` while a pod drains, where a liveness badge such as `/api/health` answers 200 and keeps the
pod in the Service's rotation. Liveness asks "am I alive?", readiness "can I serve?", and one path answering both asks
neither.
"""

from __future__ import annotations

import pathlib
import sys


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


#: The two first-party workloads that are not built on the lance app factory and serve no `/readyz`: the console's static server
#: and the issuer mirror, each probed at the one thing it serves. Neither reports draining.
_NO_READYZ = {"rask-console/console": "/", "rask-issuer-mirror/mirror": "/.well-known/openid-configuration"}


def test_the_chart_probes_READINESS_at_readyz_not_the_static_badge() -> None:
    """A readiness probe pointed at a constant cannot report draining, which is its whole job."""
    from test_invariants import _rendered_docs  # noqa: PLC0415

    probed: dict[str, str] = {}
    for doc in _rendered_docs():
        if doc.get("kind") != "Deployment":
            continue
        name = doc["metadata"]["name"]
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            path = ((container.get("readinessProbe") or {}).get("httpGet") or {}).get("path")
            if path:
                probed[f"{name}/{container['name']}"] = path

    assert {"rask-catalog/catalog", "rask-lineage/lineage"} <= set(probed), f"the walk found no probe on the lakehouse's services: {sorted(probed)}"
    wrong = {workload: path for workload, path in probed.items() if path != "/readyz"}
    assert wrong == _NO_READYZ, (
        f"these readiness probes point somewhere other than /readyz: {wrong}; `/api/health` is a "
        f"static badge, so it answers 200 while the pod drains and the kubelet keeps sending traffic"
    )
