"""The External Secrets Operator ships inside the chart for a LOCAL estate, and never for production.

`values-local.yaml` turns `externalSecrets.enabled` on, so the release renders a SecretStore and three
ExternalSecrets — CRs whose operator nothing in the repo installed. Measured 2026-09-23 on a fresh host:
`make k3s-up` stopped at "no matches for kind ExternalSecret in version external-secrets.io/v1" and
recorded no release at all; the previous host had had the operator installed by hand, at another version.

Owner ruling 2026-09-23: ESO, OpenBao and OIDC may ship in the chart, but a real production estate uses
external ones. So the operator is a subchart behind its own toggle (`externalSecrets.operator.enabled`),
ON in values-local.yaml and OFF by default and in values-prod.yaml, where the cluster's own operator
serves the same CRs. Its CRDs are vendored in chart/crds-bootstrap/ and never rendered in the release:
in-release CRDs would push the release Secret past its 1 MiB ceiling, and helm maps every kind before it
applies any, so CRs of a kind created in the same release could not be served.
"""

from __future__ import annotations

import itertools
import re

import yaml

from tests.unit import chart_render
from tests.unit.test_every_job_that_logs_into_the_store_may_reach_it import PROD_ARGS


REPO = chart_render.REPO
LOCAL = (*chart_render.DEFAULT_ARGS, "-f", str(REPO / "chart" / "values-local.yaml"))
ESO_IMAGE = re.compile(r"(^|/)external-secrets/external-secrets:")


def _operator_deployments(docs: tuple[dict, ...]) -> list[dict]:
    return [
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and any(ESO_IMAGE.search(c.get("image", "")) for c in doc["spec"]["template"]["spec"]["containers"])
    ]


def _controller(docs: tuple[dict, ...]) -> dict:
    """The operator's CONTROLLER Deployment (the subchart also ships a webhook and a cert controller)."""
    controllers = [d for d in _operator_deployments(docs) if not any(tok in d["metadata"]["name"] for tok in ("webhook", "cert-controller"))]
    assert len(controllers) == 1, f"expected one ESO controller Deployment, found {[d['metadata']['name'] for d in controllers]}"
    return controllers[0]


def _seed_binding(docs: tuple[dict, ...]) -> tuple[str, str]:
    for doc in docs:
        text = str(doc)
        if doc.get("kind") == "Job" and "bound_service_account_names=" in text:
            names = re.search(r"bound_service_account_names=(\S+)", text)
            namespaces = re.search(r"bound_service_account_namespaces=(\S+)", text)
            assert names and namespaces
            return names.group(1), namespaces.group(1)
    raise AssertionError("no OpenBao seed Job binds an ESO role — the operator could never authenticate")


def test_the_local_estate_runs_the_operator_its_secrets_need() -> None:
    docs = chart_render.render(*LOCAL)

    assert any(d.get("kind") == "ExternalSecret" for d in docs), "the local estate renders no ExternalSecret — this gate is looking at the wrong render"
    assert _controller(docs), "values-local.yaml renders ExternalSecrets but no operator to reconcile them"


def test_the_seed_binds_the_role_to_the_operator_this_release_runs() -> None:
    """The OpenBao role is bound to one ServiceAccount in one namespace; bound to the defaults of a
    stock external install instead, the in-release operator would be refused and every ExternalSecret
    would sit in SecretSyncedError."""
    docs = chart_render.render(*LOCAL, "--namespace", "probe-ns")
    controller = _controller(docs)

    assert _seed_binding(docs) == (controller["spec"]["template"]["spec"]["serviceAccountName"], "probe-ns"), (
        "the OpenBao role must be bound to the in-release controller's ServiceAccount in the release namespace"
    )


def test_the_operators_CRDs_never_ride_in_the_release() -> None:
    docs = chart_render.render(*LOCAL)

    in_release = [d["metadata"]["name"] for d in docs if d.get("kind") == "CustomResourceDefinition" and d["spec"]["group"].endswith("external-secrets.io")]

    assert not in_release, f"ESO CRDs render in the release (installCRDs must stay false; they are vendored): {in_release}"


def test_every_ESO_kind_the_chart_renders_is_served_by_a_vendored_CRD() -> None:
    docs = chart_render.render(*LOCAL)
    used = {(d["apiVersion"], d["kind"]) for d in docs if str(d.get("apiVersion", "")).startswith("external-secrets.io/")}
    vendored = [d for d in yaml.safe_load_all((REPO / "chart/crds-bootstrap/external-secrets-crds.yaml").read_text(encoding="utf-8")) if d]
    served = {(f"{c['spec']['group']}/{v['name']}", c["spec"]["names"]["kind"]) for c in vendored for v in c["spec"]["versions"] if v.get("served")}

    assert used, "no external-secrets.io object rendered — this gate is looking at the wrong render"
    assert used <= served, f"rendered but not served by chart/crds-bootstrap/external-secrets-crds.yaml: {sorted(used - served)}"


def test_the_chart_default_ships_no_operator() -> None:
    assert not _operator_deployments(chart_render.render(*chart_render.DEFAULT_ARGS, "--set", "externalSecrets.enabled=true"))


def test_production_uses_its_own_operator() -> None:
    """External in production: values-prod.yaml renders the CRs for the cluster's operator and none of its own."""
    docs = chart_render.render(*PROD_ARGS, "--set", "externalSecrets.enabled=true")

    assert any(d.get("kind") == "ExternalSecret" for d in docs)
    assert not _operator_deployments(docs), "the production overlay ships the External Secrets Operator — production must use the cluster's own"


def _pre_delete(doc: dict) -> bool:
    return "pre-delete" in (doc.get("metadata", {}).get("annotations") or {}).get("helm.sh/hook", "")


def _weight(doc: dict) -> int:
    return int((doc["metadata"].get("annotations") or {}).get("helm.sh/hook-weight", "0"))


def _secret_cleanup_deletes(docs: tuple[dict, ...]) -> list[tuple[dict, list[str]]]:
    """(job, args) of every pre-delete container that deletes ExternalSecrets."""
    found = []
    for job in (d for d in docs if d.get("kind") == "Job" and _pre_delete(d)):
        spec = job["spec"]["template"]["spec"]
        for container in [*(spec.get("initContainers") or []), *spec["containers"]]:
            args = [str(a) for a in container.get("args") or []]
            if args[:1] == ["delete"] and any(a.split("/")[0].split(".")[0] == "externalsecrets" for a in args):
                found.append((job, args))
    return found


def _deletes_it(args: list[str], secret: dict, namespace: str) -> bool:
    pairs = list(itertools.pairwise(args))
    if not (("-n", namespace) in pairs or ("--namespace", namespace) in pairs):
        return False
    if any(a.split("/", 1)[0].split(".")[0] == "externalsecrets" and a.endswith("/" + secret["metadata"]["name"]) for a in args):
        return True
    labels = secret["metadata"].get("labels") or {}
    selectors = [v for k, v in pairs if k in ("-l", "--selector")] + [a.split("=", 1)[1] for a in args if a.startswith("--selector=")]
    return any(all(labels.get(k) == v for k, _, v in (term.partition("=") for term in s.split(","))) for s in selectors)


def test_uninstall_releases_every_ExternalSecret_while_the_operator_still_runs() -> None:
    """The operator puts `externalsecrets.external-secrets.io/externalsecret-cleanup` on every
    ExternalSecret (probed live 2026-09-23: present within a second of creation), and `helm uninstall`
    deletes them in the same pass as the operator that must remove it. Measured 2026-09-23, `make
    k3s-purge` then `make k3s-up`: the new release's manifest listed three ExternalSecrets and the
    cluster held none, nor the `rask-infra-credentials` Secret they own; 22 workloads could not configure
    their containers and the install was recorded failed with `context deadline exceeded` after 20
    minutes. An install creates every object in its manifest, so these were adopted while Terminating
    and finalized by the new operator, as the Kueue queues were. Pre-delete runs before Helm removes
    anything, while the operator is still there."""
    docs = chart_render.render(*LOCAL, "--namespace", "probe-ns")
    secrets = [d for d in docs if d.get("kind") == "ExternalSecret"]
    deletes = [args for _, args in _secret_cleanup_deletes(docs)]

    assert secrets, "the local estate renders no ExternalSecret — this gate is looking at the wrong render"
    missing = [s["metadata"]["name"] for s in secrets if not any(_deletes_it(args, s, "probe-ns") for args in deletes)]
    assert not missing, f"no pre-delete hook deletes {missing} while the in-release operator can still release its finalizer"
    assert not [args for args in deletes if "--wait=false" in args], f"a cleanup delete does not wait for the operator: {deletes}"


def test_the_secret_cleanup_may_delete_what_it_selects() -> None:
    docs = chart_render.render(*LOCAL, "--namespace", "probe-ns")
    jobs = {id(job): job for job, _ in _secret_cleanup_deletes(docs)}
    assert jobs, "no pre-delete hook deletes ExternalSecrets"

    for job in jobs.values():
        account = job["spec"]["template"]["spec"]["serviceAccountName"]

        def earlier(doc: dict, job: dict = job) -> bool:
            return _pre_delete(doc) and _weight(doc) < _weight(job)

        assert any(d.get("kind") == "ServiceAccount" and d["metadata"]["name"] == account and earlier(d) for d in docs), (
            f"the cleanup runs as {account!r}, which no earlier pre-delete hook creates"
        )
        roles = {
            (d["roleRef"]["kind"], d["roleRef"]["name"])
            for d in docs
            if d.get("kind") in ("RoleBinding", "ClusterRoleBinding")
            and earlier(d)
            and any(s.get("name") == account and s.get("namespace") == "probe-ns" for s in d.get("subjects") or [])
        }
        rules = [r for d in docs if (d.get("kind"), d["metadata"].get("name")) in roles and earlier(d) for r in d.get("rules") or []]
        verbs = {
            v
            for r in rules
            if "external-secrets.io" in (r.get("apiGroups") or []) and "externalsecrets" in (r.get("resources") or [])
            for v in r.get("verbs") or []
        }
        assert {"delete", "list", "get", "watch"} <= verbs, f"the cleanup may not select, delete and wait on externalsecrets; granted {sorted(verbs)}"


def test_under_default_deny_the_in_release_operator_may_still_reach_openbao() -> None:
    """With network policies on, OpenBao admits an explicit client list. The operator this release runs
    must be on it, or the SecretStore never validates and no infra Secret is ever written."""
    docs = chart_render.render(*LOCAL, "--set", "networkPolicy.enabled=true")
    labels = _controller(docs)["spec"]["template"]["metadata"]["labels"]
    [policy] = [d for d in docs if d.get("kind") == "NetworkPolicy" and d["metadata"]["name"].endswith("-openbao")]

    def admits(peer: dict) -> bool:
        selector = peer.get("podSelector") or {}
        wanted = selector.get("matchLabels") or {}
        return bool(wanted) and all(labels.get(k) == v for k, v in wanted.items()) and not selector.get("matchExpressions")

    assert any(admits(peer) for rule in policy["spec"]["ingress"] for peer in rule.get("from") or []), (
        f"the OpenBao ingress policy admits no peer matching the in-release ESO controller's labels {labels}"
    )
