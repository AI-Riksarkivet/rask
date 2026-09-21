"""The kueue hook re-triggers the controller's CA patch before it applies queues.

[[XC-065]] MEASURED LIVE 2026-09-21, and it took an upgrade down. `make k3s-up` reached its
post-upgrade hooks and `rask-kueue-setup` crash-looped nine times over 21 minutes on::

    conversion webhook for kueue.x-k8s.io/v1beta2, Kind=ClusterQueue failed: ...
    x509: certificate signed by unknown authority ... "kueue-ca"

The whole release was then recorded `failed` although every resource had already applied.

WHY RETRYING CANNOT FIX IT, which is the point this file exists to encode. The Job's own comment
states its retry design: "If the Kueue admission webhook (failurePolicy=Fail) isn't ready yet the apply
exits non-zero and the Job's restartPolicy + backoffLimit retry it — that's the retry loop, no shell
needed." That is correct for a webhook still coming up, and useless here. kueue's controller writes its
self-signed CA into the CRDs' `spec.conversion.webhook.clientConfig.caBundle` when it BOOTS; `helm
upgrade` re-applies those CRDs from the subchart and overwrites the patch. Nothing re-runs, so the
mismatch is PERMANENT and every retry meets the same wall. Measured: the controller had been up 24
hours with zero restarts, so it had long since patched and had no reason to patch again.

THE REMEDY IS THE ONE THAT WAS OBSERVED TO WORK: `rollout restart` of the controller re-ran its patch
and `kubectl get clusterqueues` answered again, 68 seconds later. Doing it as an init container is what
makes it ORDERED — the queues apply only once the CA the API server holds matches the certificate the
webhook serves, rather than racing it.

WHY UNCONDITIONALLY. The caBundle is clobbered whenever the CRDs are re-applied, which is every
upgrade, so there is no cheap condition to test — and the image is distroless with no shell to test one
in. One controller restart per upgrade is the price; a failed release is the alternative.
"""

from __future__ import annotations

from tests.unit.chart_render import DEFAULT_ARGS, render


def _kueue_setup_job() -> dict:
    jobs = [d for d in render(*DEFAULT_ARGS) if d.get("kind") == "Job" and d.get("metadata", {}).get("name", "").endswith("-kueue-setup")]
    assert jobs, "no kueue-setup Job rendered; this gate would check nothing"
    return jobs[0]


def test_the_hook_restarts_the_controller_before_applying() -> None:
    """RED before the fix: the Job went straight to `apply` and met a caBundle nothing had re-patched."""
    spec = _kueue_setup_job()["spec"]["template"]["spec"]
    inits = spec.get("initContainers") or []

    assert inits, "the hook has no init container, so nothing re-triggers kueue's CA patch before the apply"
    joined = " ".join(str(a) for c in inits for a in (c.get("args") or []))
    assert "rollout" in joined and "restart" in joined, f"no init container restarts the kueue controller; args were: {joined!r}"


def test_it_waits_for_the_controller_before_the_queues_apply() -> None:
    """A restart that is not waited on is a race: the apply would meet the OLD webhook."""
    spec = _kueue_setup_job()["spec"]["template"]["spec"]
    joined = " ".join(str(a) for c in (spec.get("initContainers") or []) for a in (c.get("args") or []))

    assert "status" in joined, "nothing waits for the restarted controller, so the apply races the CA patch"


def test_the_hook_may_actually_restart_a_deployment() -> None:
    """RBAC is where this silently fails: a restart it cannot perform is a Job that fails differently.

    The ClusterRole is rendered by the same template, so the permission and the action stay together —
    the failure mode this estate has already paid for is a control whose permission lives elsewhere and
    was never granted.
    """
    roles = [d for d in render(*DEFAULT_ARGS) if d.get("kind") == "ClusterRole" and d.get("metadata", {}).get("name", "").endswith("-kueue-setup")]
    assert roles, "no kueue-setup ClusterRole rendered"

    rules = roles[0].get("rules") or []
    deployment_rules = [r for r in rules if "apps" in (r.get("apiGroups") or []) and "deployments" in (r.get("resources") or [])]
    assert deployment_rules, "the hook restarts a Deployment but holds no `apps/deployments` rule"
    verbs = {v for r in deployment_rules for v in (r.get("verbs") or [])}
    assert "patch" in verbs, f"`rollout restart` is a PATCH of the pod template; verbs granted were {sorted(verbs)}"
    assert "get" in verbs, f"`rollout status` reads the Deployment; verbs granted were {sorted(verbs)}"
