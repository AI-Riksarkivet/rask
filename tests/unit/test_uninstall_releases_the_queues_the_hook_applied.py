"""Uninstalling the release deletes the Kueue queues its setup hook applied, while Kueue is still running.

The post-install hook creates the queue topology with `kubectl apply`, so Helm never records those
objects and `helm uninstall` never deletes them. It does delete the Kueue controller and the Kueue CRDs
(templates of the kueue subchart), in one pass. The ResourceFlavor and the ClusterQueue each carry
`kueue.x-k8s.io/resource-in-use`, a finalizer only that controller removes, so the objects outlive it and
hold their CRDs in Terminating with nothing left to release them.

Measured 2026-09-23, moving the estate from `default` to `rask`: `helm uninstall` completed, the next
`make k3s-up` adopted the two Terminating CRDs (`--take-ownership`), the new Kueue controller released
the orphaned finalizers, the CRDs finished deleting under the install, and the release was recorded
failed with `customresourcedefinitions.apiextensions.k8s.io "clusterqueues.kueue.x-k8s.io" not found`.
The other nine Kueue CRDs, whose kinds the chart never instantiates, were recreated in the install's
first seconds; those two appeared only on the next upgrade. `make k3s-down` then `make k3s-up` walks the
same path.

So a pre-delete hook deletes every object the setup hook applies, and WAITS for it: pre-delete runs
before Helm removes anything, which is the only moment the controller that owns the finalizers is still
there to release them.
"""

from __future__ import annotations

import itertools

import yaml

from tests.unit.chart_render import DEFAULT_ARGS, render


NAMESPACE = "probe-ns"


def _docs() -> tuple[dict, ...]:
    return render(*DEFAULT_ARGS, "--namespace", NAMESPACE)


def _hook(doc: dict) -> str:
    return (doc.get("metadata", {}).get("annotations") or {}).get("helm.sh/hook", "")


def _applied() -> list[tuple[str, str, str | None]]:
    """(kind, name, namespace) of every object the setup hook `kubectl apply`s."""
    [config] = [d for d in _docs() if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-kueue-queues")]
    objects = [o for o in yaml.safe_load_all(config["data"]["queues.yaml"]) if o]
    assert objects, "the setup hook applies nothing — this gate would check nothing"
    return [(o["kind"], o["metadata"]["name"], o["metadata"].get("namespace")) for o in objects]


def _cleanup_jobs() -> list[dict]:
    return [d for d in _docs() if d.get("kind") == "Job" and "pre-delete" in _hook(d)]


def _deletes(job: dict) -> list[list[str]]:
    spec = job["spec"]["template"]["spec"]
    containers = [*(spec.get("initContainers") or []), *spec["containers"]]
    return [[str(a) for a in c.get("args") or []] for c in containers if (c.get("args") or [None])[0] == "delete"]


def _names_it(args: list[str], kind: str, name: str, namespace: str | None) -> bool:
    for arg in args:
        resource, _, ref = arg.partition("/")
        if not ref or ref != name:
            continue
        if resource.split(".")[0].removesuffix("s") != kind.lower():
            continue
        if namespace is None:
            return True
        flags = list(itertools.pairwise(args))
        return ("-n", namespace) in flags or ("--namespace", namespace) in flags or f"--namespace={namespace}" in args
    return False


def test_every_queue_object_the_hook_applies_is_deleted_before_uninstall() -> None:
    deletes = [args for job in _cleanup_jobs() for args in _deletes(job)]

    missing = [obj for obj in _applied() if not any(_names_it(args, *obj) for args in deletes)]

    assert not missing, (
        f"no pre-delete hook deletes {missing}: `helm uninstall` removes the Kueue controller and leaves these "
        "behind with finalizers nothing will release, holding their CRDs in Terminating for the next install to trip on"
    )


def test_the_cleanup_waits_for_the_finalizers() -> None:
    """A delete that returns before the finalizers clear hands the objects to an uninstall that is about
    to remove the only controller able to clear them."""
    deletes = [args for job in _cleanup_jobs() for args in _deletes(job)]

    assert deletes, "no pre-delete hook deletes anything"
    assert not [args for args in deletes if "--wait=false" in args], f"a cleanup delete does not wait: {deletes}"


def test_the_cleanup_may_delete_what_it_names() -> None:
    """The setup hook's RBAC is deleted when that hook succeeds, so the cleanup needs its own, rendered
    as a pre-delete hook ahead of the Job."""
    docs = _docs()
    [job] = _cleanup_jobs()
    account = job["spec"]["template"]["spec"]["serviceAccountName"]
    job_weight = int(job["metadata"]["annotations"].get("helm.sh/hook-weight", "0"))

    def earlier_hook(doc: dict) -> bool:
        return "pre-delete" in _hook(doc) and int(doc["metadata"]["annotations"].get("helm.sh/hook-weight", "0")) < job_weight

    assert any(d.get("kind") == "ServiceAccount" and d["metadata"]["name"] == account and earlier_hook(d) for d in docs), (
        f"the cleanup runs as {account!r}, which no earlier pre-delete hook creates"
    )
    bound = {
        d["roleRef"]["name"]
        for d in docs
        if d.get("kind") == "ClusterRoleBinding"
        and earlier_hook(d)
        and any(s.get("name") == account and s.get("namespace") == NAMESPACE for s in d.get("subjects") or [])
    }
    rules = [r for d in docs if d.get("kind") == "ClusterRole" and d["metadata"]["name"] in bound and earlier_hook(d) for r in d.get("rules") or []]
    for kind, _, _ in _applied():
        resource = f"{kind.lower()}s"
        verbs = {v for r in rules if "kueue.x-k8s.io" in (r.get("apiGroups") or []) and resource in (r.get("resources") or []) for v in r.get("verbs") or []}
        assert {"delete", "get", "watch"} <= verbs, f"the cleanup may not delete-and-wait on {resource}; granted {sorted(verbs)}"
