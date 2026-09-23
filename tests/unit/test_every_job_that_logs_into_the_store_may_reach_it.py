"""Under the production network posture, every Job that logs into the object store is admitted to it.

`values-prod.yaml` turns on default-deny; `rask-minio-ingress` then admits an explicit list of pod
components, and the general intra-namespace allow EXCLUDES the store. A Job absent from that list prints
`wait-minio` forever. `minio-scoped-users` was absent — so on a policy-enforcing CNI every scoped identity
the fleet boots with was never created, and since that Job runs in the install wave (and as the
pre-upgrade hook), an install or upgrade waited on it until `--wait` gave up.

Read as behaviour, not as a list: every Job whose script runs `mc alias set rfs <the store>` must carry
a component the store's ingress admits, so a new provisioning Job is covered the day it is added.
"""

from __future__ import annotations

from tests.unit import chart_render


REPO = chart_render.REPO

#: The same overrides `scripts/prod_render_check.sh` supplies: values-prod.yaml pins no registry, token
#: or credentials on purpose, so the render needs dummies for each.
PROD_ARGS = (
    "-f", str(REPO / "chart" / "values-prod.yaml"),
    "--set", "image.repository=ghcr.io/example/rask",
    "--set", "image.catalog.tag=v0", "--set", "frontend.image.tag=v0",
    "--set", "dapr.appToken=ci-dummy-token-0000000000",
    "--set", "age.password=ci-dummy-pw", "--set", "minio.secretKey=ci-dummy-key",
    "--set", "backups.volumeSnapshot.snapshotClassName=csi-snapclass",
    "--set", "ingress.host=lance.example.com",
)  # fmt: skip


def _admitted_components(docs: tuple[dict, ...]) -> set[str]:
    [policy] = [d for d in docs if d.get("kind") == "NetworkPolicy" and d["metadata"]["name"].endswith("-minio-ingress")]
    admitted: set[str] = set()
    for rule in policy["spec"].get("ingress") or []:
        for peer in rule.get("from") or []:
            for expression in (peer.get("podSelector") or {}).get("matchExpressions") or []:
                if expression.get("key") == "app.kubernetes.io/component" and expression.get("operator") == "In":
                    admitted |= set(expression.get("values") or [])
    return admitted


def test_every_job_that_logs_into_the_store_is_admitted_by_its_ingress_policy() -> None:
    docs = chart_render.render(*PROD_ARGS)
    admitted = _admitted_components(docs)
    store_jobs = {
        doc["metadata"]["name"]: doc["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/component")
        for doc in docs
        if doc.get("kind") == "Job" and "mc alias set rfs" in str(doc["spec"]["template"]["spec"]["containers"])
    }

    assert store_jobs, "the prod render has no Job logging into the store — this gate is looking at the wrong render"
    blocked = {job: component for job, component in store_jobs.items() if component not in admitted}
    assert not blocked, (
        f"these Jobs log into the store but its ingress policy does not admit their component, so they wait forever under default-deny: {blocked}"
    )
