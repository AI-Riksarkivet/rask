"""No pod the release runs can read a Secret, or review a token, through the Kubernetes API, and every
first-party service runs as its own ServiceAccount.

[[XC-076]]. The Dapr subchart's `dapr_rbac.secretReader` binds `secrets: get` to the namespace's
`default` ServiceAccount, and any pod the chart gives no SA of its own runs as `default` with its token
mounted. Code execution in such a container then reads every Secret in the namespace, around the Dapr
store's scopes. No sidecar needs the grant: every injected pod disables daprd's built-in Kubernetes
secret store (`lance.daprSidecarResources`).

READ FROM THE RENDER, in the three overlays that ship: the default values, the deployed estate
(`make k3s-up` layers `values-local.yaml`), and production. A binding is judged by what its role
grants, so a new subchart that binds a secret-reading role to `default` fails here without anyone
having listed it.

ONE TEST PER CLOSES-WHEN CLAUSE a render can see: the grants, and one SA per service. The third,
every sidecar still loading lance-secrets, is read back on the deployed estate.

MUTATION-CHECKED 2026-10-02. With `dapr.dapr_rbac.secretReader.enabled: true` (the subchart's own
default) in chart/values.yaml, the grants test fails in all three overlays on
`RoleBinding/dapr-secret-reader grants ['secrets'] to SA default` and the identity test passes. With
`openfga-model` on `rask-sa-jobs`, only the identity test fails, on that pre-upgrade hook.
"""

from __future__ import annotations

import functools
import re

import pytest
import yaml
from pydantic import BaseModel, ConfigDict

from tests.unit.chart_render import DEFAULT_ARGS, REPO, render_text
from tests.unit.chart_yaml import FAST_LOADER


#: The fail-closed prod guards' dummy inputs, the set `scripts/prod_render_check.sh` supplies.
_PROD_ARGS: tuple[str, ...] = (
    "-f", str(REPO / "chart/values-prod.yaml"),
    "--set", "image.catalog.tag=v0",
    "--set", "frontend.image.tag=v0",
    "--set", "dapr.appToken=ci-dummy-token-0000000000",
    "--set", "age.password=ci-dummy-pw",
    "--set", "minio.secretKey=ci-dummy-key",
    "--set", "backups.volumeSnapshot.snapshotClassName=csi-snapclass",
    "--set", "ingress.host=lance.example.com",
    "--set", "image.repository=ghcr.io/example/rask",
)  # fmt: skip

_OVERLAYS: dict[str, tuple[str, ...]] = {
    "default": DEFAULT_ARGS,
    "deployed": ("--set", "image.localImages=true", "-f", str(REPO / "chart/values-local.yaml")),
    "prod": _PROD_ARGS,
}

_FIRST_PARTY_SOURCE = "rask/templates/"

#: Built-in roles a binding may reference without the chart rendering them, and what each grants of
#: the two capabilities this gate is about. An unlisted external role fails the gate: it cannot judge it.
_BUILTIN_GRANTS: dict[tuple[str, str], frozenset[str]] = {
    ("ClusterRole", "cluster-admin"): frozenset({"secrets", "tokenreviews"}),
    ("ClusterRole", "admin"): frozenset({"secrets"}),
    ("ClusterRole", "edit"): frozenset({"secrets"}),
    ("ClusterRole", "view"): frozenset(),
    ("ClusterRole", "system:auth-delegator"): frozenset({"tokenreviews"}),
    ("ClusterRole", "system:service-account-issuer-discovery"): frozenset(),
    ("Role", "extension-apiserver-authentication-reader"): frozenset(),
}

#: Group subjects that contain every ServiceAccount, so a grant to one is a grant to all of ours.
_EVERY_SERVICE_ACCOUNT = re.compile(r"^system:(serviceaccounts(:.*)?|authenticated)$")

#: SAs more than one long-running workload may share, each because the sharers are ONE identity.
_SHARED_IDENTITIES: dict[str, str] = {
    "rask-sa-web": "every zone calls the backends as the one frontend.serviceIdentity",
    "rask-sa-maintenance": "the sweep and its work-queue executor are one maintenance identity",
}

#: The D1 verifiers LH-220 adds the SA issuer to; each reads the issuer's discovery document and JWKS.
_ISSUER_VERIFIERS = ("Deployment/rask-catalog", "Deployment/rask-lineage")

#: The hook phases Helm runs before it applies the release manifest. A pod in one of them can run only as
#: an SA that already exists, which on a first install or on the upgrade that introduces it is an SA that
#: is itself a hook of that phase, created at a lower weight.
_BEFORE_THE_MANIFEST = frozenset({"pre-install", "pre-upgrade"})


class _Pod(BaseModel):
    """One pod template the release runs, reduced to the identity it runs as."""

    model_config = ConfigDict(frozen=True)

    workload: str
    service_account: str
    token_mounted: bool
    first_party: bool
    long_running: bool
    hook_phases: frozenset[str]
    hook_weight: int


class _Binding(BaseModel):
    """One RoleBinding/ClusterRoleBinding with what its role grants."""

    model_config = ConfigDict(frozen=True)

    name: str
    role: tuple[str, str]
    grants: frozenset[str]
    service_accounts: tuple[str, ...]
    groups: tuple[str, ...]


@functools.cache
def _documents(*args: str) -> tuple[tuple[str, dict], ...]:
    """`(source, document)` for every mapping the chart renders, the source read from Helm's header."""
    found: list[tuple[str, dict]] = []
    for chunk in render_text(*args).split("\n---\n"):
        source = re.search(r"^# Source: (\S+)", chunk, re.MULTILINE)
        if source:
            found.extend((source.group(1), doc) for doc in yaml.load_all(chunk, Loader=FAST_LOADER) if isinstance(doc, dict))
    return tuple(found)


def _templates(doc: dict) -> list[dict]:
    """Every pod template a workload document carries, Ray's CR-owned pods included."""
    kind, spec = doc.get("kind"), doc.get("spec") or {}
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
        return [spec["template"]]
    if kind == "CronJob":
        return [spec["jobTemplate"]["spec"]["template"]]
    if kind == "Pod":
        return [{"metadata": doc.get("metadata") or {}, "spec": spec}]
    ray = spec.get("rayClusterConfig", spec) if kind == "RayService" else spec if kind == "RayCluster" else None
    if ray is None:
        return []
    return [ray["headGroupSpec"]["template"], *(group["template"] for group in ray.get("workerGroupSpecs") or [])]


def _hook(doc: dict) -> tuple[frozenset[str], int]:
    """The Helm hook phases a document runs in and its weight; no phases for an ordinary release resource."""
    annotations = (doc.get("metadata") or {}).get("annotations") or {}
    phases = frozenset(phase.strip() for phase in annotations.get("helm.sh/hook", "").split(",") if phase.strip())
    return phases, int(annotations.get("helm.sh/hook-weight", "0"))


@functools.cache
def _pods(*args: str) -> tuple[_Pod, ...]:
    docs = _documents(*args)
    service_accounts = {doc["metadata"]["name"]: doc for _, doc in docs if doc.get("kind") == "ServiceAccount"}
    pods: list[_Pod] = []
    for source, doc in docs:
        phases, weight = _hook(doc)
        if "test" in phases:
            continue  # a `helm test` pod runs only when someone runs `helm test`; it is not part of the estate
        for template in _templates(doc):
            spec = template.get("spec") or {}
            account = spec.get("serviceAccountName") or "default"
            automount = spec.get("automountServiceAccountToken", (service_accounts.get(account) or {}).get("automountServiceAccountToken", True))
            pods.append(
                _Pod(
                    workload=f"{doc['kind']}/{doc['metadata']['name']}",
                    service_account=account,
                    token_mounted=automount is not False,
                    first_party=source.startswith(_FIRST_PARTY_SOURCE),
                    long_running=doc["kind"] in {"Deployment", "StatefulSet", "DaemonSet", "RayCluster", "RayService"},
                    hook_phases=phases,
                    hook_weight=weight,
                )
            )
    return tuple(pods)


def _rule_grants(rule: dict) -> set[str]:
    groups, resources, verbs = (set(rule.get(key) or []) for key in ("apiGroups", "resources", "verbs"))
    found: set[str] = set()
    if groups & {"", "*"} and resources & {"secrets", "*"} and verbs:
        found.add("secrets")
    if groups & {"authentication.k8s.io", "*"} and resources & {"tokenreviews", "*"} and verbs & {"create", "*"}:
        found.add("tokenreviews")
    return found


@functools.cache
def _bindings(*args: str) -> tuple[_Binding, ...]:
    docs = [doc for _, doc in _documents(*args)]
    roles = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs if doc.get("kind") in {"Role", "ClusterRole"}}
    found: list[_Binding] = []
    for doc in docs:
        if doc.get("kind") not in {"RoleBinding", "ClusterRoleBinding"}:
            continue
        ref = (doc["roleRef"]["kind"], doc["roleRef"]["name"])
        if ref in roles:
            grants = frozenset().union(*(_rule_grants(rule) for rule in roles[ref].get("rules") or []))
        elif ref in _BUILTIN_GRANTS:
            grants = _BUILTIN_GRANTS[ref]
        else:
            pytest.fail(f"{doc['kind']}/{doc['metadata']['name']} binds {ref}, which neither the render nor _BUILTIN_GRANTS describes")
        subjects = doc.get("subjects") or []
        found.append(
            _Binding(
                name=f"{doc['kind']}/{doc['metadata']['name']}",
                role=ref,
                grants=grants,
                service_accounts=tuple(s["name"] for s in subjects if s.get("kind") == "ServiceAccount" and s.get("namespace", "default") == "default"),
                groups=tuple(s["name"] for s in subjects if s.get("kind") == "Group"),
            )
        )
    return tuple(found)


def _pod(args: tuple[str, ...], workload: str) -> _Pod:
    matches = [pod for pod in _pods(*args) if pod.workload == workload]
    assert len(matches) == 1, f"expected one pod template for {workload}, rendered {len(matches)}"
    return matches[0]


def _offending(args: tuple[str, ...]) -> list[str]:
    """Every grant of `secrets` or `tokenreviews` that reaches an SA a first-party pod runs as.

    The one sanctioned grant: `system:auth-delegator` on the SA OpenBao alone runs as, which its
    Kubernetes auth backend reviews ESO's login token with.
    """
    pods = _pods(*args)
    ours = {pod.service_account for pod in pods if pod.first_party} | {"default"}
    openbao = [pod.service_account for pod in pods if pod.workload == "Deployment/rask-openbao"]
    reviewer = openbao[0] if openbao and sum(pod.service_account == openbao[0] for pod in pods) == 1 else None
    found: list[str] = []
    for binding in _bindings(*args):
        if not binding.grants:
            continue
        for group in binding.groups:
            if _EVERY_SERVICE_ACCOUNT.match(group):
                found.append(f"{binding.name} grants {sorted(binding.grants)} to group {group}")
        for account in binding.service_accounts:
            sanctioned = binding.role == ("ClusterRole", "system:auth-delegator") and account == reviewer
            if account in ours and not sanctioned:
                found.append(f"{binding.name} grants {sorted(binding.grants)} to SA {account}")
    return found


@pytest.mark.parametrize(
    ("overlay", "eso"),
    [pytest.param("default", False, id="default"), pytest.param("deployed", True, id="deployed"), pytest.param("prod", False, id="prod")],
)
def test_no_first_party_service_account_can_read_a_secret_or_review_a_token(overlay: str, eso: bool) -> None:
    """No grant of `secrets` or `tokenreviews` reaches `default`, an every-SA group, or an SA a first-party pod runs as.

    The one sanctioned grant is `system:auth-delegator` on the SA OpenBao alone runs as, and only under
    ESO, the one path that logs in through OpenBao's Kubernetes auth backend. OpenBao reviews ESO's
    login with its OWN mounted token, so the grant and the mount must name one SA; without ESO the token
    would be idle and the pod holds none.
    """
    args = _OVERLAYS[overlay]
    pods, bindings = _pods(*args), _bindings(*args)
    openbao = _pod(args, "Deployment/rask-openbao")
    delegated = [b.service_accounts for b in bindings if b.role == ("ClusterRole", "system:auth-delegator")]

    # The walk sees the chart and the classifier sees the subcharts' own grants; without these every
    # assertion below passes by parsing nothing.
    assert sum(pod.first_party for pod in pods) >= 25, f"{overlay}: only {sum(p.first_party for p in pods)} first-party pod templates"
    assert any(not pod.first_party for pod in pods), f"{overlay}: no subchart pods rendered; the source split is broken"
    assert any("secrets" in b.grants for b in bindings), f"{overlay}: no binding classified as granting secrets; the Dapr operator's does"
    assert any("tokenreviews" in b.grants for b in bindings), f"{overlay}: no binding classified as reviewing tokens; Sentry's does"

    assert _offending(args) == [], f"{overlay}: {_offending(args)}"
    assert delegated == ([(openbao.service_account,)] if eso else []), (
        f"{overlay}: auth-delegator bound to {delegated}, OpenBao runs as {openbao.service_account}"
    )
    assert openbao.token_mounted is eso, f"{overlay}: OpenBao mounts a token: {openbao.token_mounted}, ESO on: {eso}"


@pytest.mark.parametrize("overlay", sorted(_OVERLAYS))
def test_every_first_party_service_runs_as_its_own_service_account(overlay: str) -> None:
    """One SA per service is the identity D1 authenticates, and nothing else in the namespace shares it.

    No pod the release runs, a subchart's included, is on `default`, the identity every unnamed pod
    shares. Every SA a pod names exists when the pod is created: a pod naming an absent SA is refused at
    admission and its workload never starts, and a hook that runs before the manifest holds the whole
    upgrade until its timeout. A one-shot Job never borrows a service's identity, and a first-party pod
    mounts an API token only when a binding gives its SA something to do with it, since an idle token
    is only a credential to steal. The D1 verifiers (LH-220) validate SA tokens offline against the
    issuer's JWKS, which the API server serves only to a grant.
    """
    args = _OVERLAYS[overlay]
    pods, bindings = _pods(*args), _bindings(*args)
    accounts = {doc["metadata"]["name"]: _hook(doc) for _, doc in _documents(*args) if doc.get("kind") == "ServiceAccount"}
    rendered = set(accounts) | {"default"}
    first_party = [pod for pod in pods if pod.first_party]
    services: dict[str, set[str]] = {}
    for pod in first_party:
        if pod.long_running:
            services.setdefault(pod.service_account, set()).add(pod.workload)
    granted = {account for binding in bindings for account in binding.service_accounts}
    issuer_readers = {a for b in bindings if b.role == ("ClusterRole", "system:service-account-issuer-discovery") for a in b.service_accounts}
    verifiers = {_pod(args, workload).service_account for workload in _ISSUER_VERIFIERS}

    problems = {
        "on `default`": sorted(pod.workload for pod in pods if pod.service_account == "default"),
        "naming an SA the release does not render": sorted({f"{pod.workload} -> {pod.service_account}" for pod in pods if pod.service_account not in rendered}),
        "hooks that start before their SA exists": sorted(
            f"{pod.workload} ({phase}) -> {pod.service_account}"
            for pod in pods
            for phase in sorted(pod.hook_phases & _BEFORE_THE_MANIFEST)
            if pod.service_account in accounts and not (phase in accounts[pod.service_account][0] and accounts[pod.service_account][1] < pod.hook_weight)
        ),
        "services sharing an SA": {sa: sorted(ws) for sa, ws in services.items() if len(ws) > 1 and sa not in _SHARED_IDENTITIES},
        "one-shot pods on a service's SA": sorted(
            f"{pod.workload} -> {pod.service_account}" for pod in first_party if not pod.long_running and pod.service_account in services
        ),
        "an API token no grant uses": sorted(
            f"{pod.workload} ({pod.service_account})" for pod in first_party if pod.token_mounted and pod.service_account not in granted
        ),
        "D1 verifiers that cannot read the SA issuer": sorted(verifiers - issuer_readers),
        "issuer discovery granted to `default`": sorted(issuer_readers & {"default"}),
    }

    found = {name: offenders for name, offenders in problems.items() if offenders}
    assert not found, f"{overlay}: {found}"
