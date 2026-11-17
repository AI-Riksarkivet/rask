"""No pod the release runs can read a Secret, review a token, or reach inside a node or a pod through the Kubernetes API,
and every first-party service runs as its own ServiceAccount.

[[XC-202]]. `nodes/proxy` hands its holder the kubelet's API through the API server, exec into every pod on the node
included (a websocket upgrade is a GET), and a pod's own exec, attach or port-forward is the same reach into one pod. No
first-party pod needs either.

[[XC-076]]. A binding of `secrets: get` to the namespace's `default` ServiceAccount (a subchart or platform chart
that binds a secret reader to it is the known case) reaches any pod the chart gives no SA of its own, which runs as
`default` with its token mounted. Code execution in such a container then reads every Secret in the namespace, around
the secret store's policies.

[[LH-341]]. A grant that hands its holder another identity reaches a Secret as surely as reading one: a pod it creates
runs as any ServiceAccount of the namespace, `serviceaccounts/token` mints a token outright, `impersonate` acts as its
target, and `bind` and `escalate` on a role give whatever that role grants. An aggregated ClusterRole's rules are written
by the cluster, so a binding to one cannot be judged and fails the gate.

READ FROM THE RENDER, in the three overlays that ship: the default values, the deployed estate
(`make k3s-up` layers `values-local.yaml`), and production, and the default values rendered into another namespace,
since a subject is one of the release's pods' only in the namespace the release installs into ([[LH-344]]). A binding is
judged by what its role grants, so a new subchart that binds a secret-reading role to `default` fails here without anyone
having listed it.

ONE TEST PER CLOSES-WHEN CLAUSE a render can see: the grants, and one SA per service. That the dev store its
readers log in to is seeded before it serves is `test_the_dev_openbao_is_seeded_by_its_own_pod.py`.

MUTATION-CHECKED 2026-10-11 ([[XC-128]]; again with [[XC-130]]). With a template binding a `secrets: get` Role to SA `default` added to the
chart, the grants test fails in every overlay on `RoleBinding/<name> grants ['secrets'] to SA default` and the
identity test passes. With `openfga-model` on `rask-sa-jobs`, an ordinary SA no
earlier revision guarantees, only the identity test fails, on that pre-upgrade hook. With `rask-sa-hooks` rendered as
a hook again, only the identity test fails, on `identities rendered as hooks`. Again 2026-11-17: a Role granting `pods`
create bound to `default` in the release's namespace fails the grants test in every overlay, the other namespace's
included; `escalate` on ClusterRoles bound to the catalog's account fails it likewise; and a bound aggregated ClusterRole
fails both tests; and a Role creating `deployments` bound to `default` fails the grants test as the `pods` one does.
"""

from __future__ import annotations

import functools
import re

import pytest
import yaml
from pydantic import BaseModel, ConfigDict

from tests.chart.chart_render import DEFAULT_ARGS, PROD_ARGS, REPO, render_fixture, render_text
from tests.chart.chart_yaml import FAST_LOADER


#: A namespace other than the one `helm template` renders into unasked, so a subject is judged in the release's own.
_ANOTHER_NAMESPACE = "lake"

_OVERLAYS: dict[str, tuple[str, ...]] = {
    "default": DEFAULT_ARGS,
    "another-namespace": (*DEFAULT_ARGS, "--namespace", _ANOTHER_NAMESPACE),
    "deployed": ("--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml")),
    "prod": PROD_ARGS,
}

_FIRST_PARTY_SOURCE = "rask/templates/"

#: The subresources that reach inside a node or a pod: the kubelet's API through the API server's proxy, which answers
#: its exec, attach and port-forward endpoints too (a websocket upgrade is a GET), and a pod's own exec, attach and
#: port-forward.
_INTERIOR = frozenset({"nodes/proxy", "pods/exec", "pods/attach", "pods/portforward"})

#: Grants that hand their holder another identity: a pod it creates, itself or through a workload whose pod template it
#: writes, runs as any ServiceAccount of the namespace with that account's token mounted, `serviceaccounts/token` mints
#: one outright, `impersonate` acts as its target, and `bind` and
#: `escalate` on a role give the holder whatever that role grants.
_BORROWED_IDENTITY = frozenset({"pods", "serviceaccounts/token", "impersonate", "bind", "escalate"})

#: The kinds that carry a pod template, by API group.
_POD_TEMPLATE_KINDS: dict[str, frozenset[str]] = {
    "": frozenset({"replicationcontrollers"}),
    "apps": frozenset({"deployments", "statefulsets", "daemonsets", "replicasets"}),
    "batch": frozenset({"jobs", "cronjobs"}),
}

#: Built-in roles a binding may reference without the chart rendering them, and what each grants of
#: the capabilities this gate is about. An unlisted external role fails the gate: it cannot judge it.
_BUILTIN_GRANTS: dict[tuple[str, str], frozenset[str]] = {
    ("ClusterRole", "cluster-admin"): frozenset({"secrets", "tokenreviews"} | _INTERIOR | _BORROWED_IDENTITY),
    ("ClusterRole", "admin"): frozenset({"secrets", "pods/exec", "pods/attach", "pods/portforward", "pods", "serviceaccounts/token", "impersonate"}),
    ("ClusterRole", "edit"): frozenset({"secrets", "pods/exec", "pods/attach", "pods/portforward", "pods", "serviceaccounts/token", "impersonate"}),
    ("ClusterRole", "view"): frozenset(),
    ("ClusterRole", "system:auth-delegator"): frozenset({"tokenreviews"}),
    ("ClusterRole", "system:service-account-issuer-discovery"): frozenset(),
    ("Role", "extension-apiserver-authentication-reader"): frozenset(),
}

#: Group subjects that contain every ServiceAccount, so a grant to one is a grant to all of ours.
_EVERY_SERVICE_ACCOUNT = re.compile(r"^system:(serviceaccounts(:.*)?|authenticated)$")

#: A User subject RBAC matches against that SA's own token, so it binds the SA as surely as its own kind.
_SERVICE_ACCOUNT_USER = re.compile(r"^system:serviceaccount:(?P<namespace>[^:]+):(?P<name>[^:]+)$")

#: SAs more than one long-running workload may share, each because the sharers are ONE identity.
_SHARED_IDENTITIES: dict[str, str] = {
    "rask-sa-maintenance": "the sweep and its work-queue executor are one maintenance identity",
}

#: The D1 verifiers LH-220 adds the SA issuer to; each reads the issuer's discovery document and JWKS.
_ISSUER_VERIFIERS = ("Deployment/rask-catalog", "Deployment/rask-lineage")

#: The hook phases Helm runs before it applies the release manifest. A pod in one of them runs only as an SA that
#: already exists: an ordinary SA arrives with the manifest, AFTER them, on the install or upgrade that introduces it.
_BEFORE_THE_MANIFEST = frozenset({"pre-install", "pre-upgrade"})
#: Ordinary SAs every deployed revision has rendered since they were introduced, so a pre-upgrade hook finds them from
#: the previous revision ([[XC-126]]). A pre-install hook still cannot run as one. Adding a name here is the decision
#: that a NEW SA serves a pre-upgrade hook: the upgrade introducing it must find it created beforehand
#: (docs/chart/templates/security-sa.yaml.md), which no render can check.
_ESTABLISHED = frozenset({"rask-sa-hooks"})
#: The kinds that ARE an identity or its grants: one rendered as a hook is deleted by `hook-succeeded` while a failed
#: hook's pod still holds its token, and exists only during its own phases.
_IDENTITY_KINDS = frozenset({"ServiceAccount", "Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"})


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
    """Every pod template a workload document carries."""
    kind, spec = doc.get("kind"), doc.get("spec") or {}
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
        return [spec["template"]]
    if kind == "CronJob":
        return [spec["jobTemplate"]["spec"]["template"]]
    if kind == "Pod":
        return [{"metadata": doc.get("metadata") or {}, "spec": spec}]
    return []


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
                    long_running=doc["kind"] in {"Deployment", "StatefulSet", "DaemonSet"},
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
    if groups & {"", "*"} and verbs:
        found |= {sub for sub in _INTERIOR if resources & {sub, f"{sub.split('/')[0]}/*", "*"}}
    if groups & {"", "*"} and resources & {"pods", "*"} and verbs & {"create", "*"}:
        found.add("pods")
    # A workload's pod template names the account its pods run as, so writing one is creating a pod as that account.
    if any(groups & {group, "*"} and resources & (kinds | {"*"}) for group, kinds in _POD_TEMPLATE_KINDS.items()) and verbs & {
        "create",
        "update",
        "patch",
        "*",
    }:
        found.add("pods")
    if groups & {"", "*"} and resources & {"serviceaccounts/token", "serviceaccounts/*", "*"} and verbs & {"create", "*"}:
        found.add("serviceaccounts/token")
    if verbs & {"impersonate", "*"}:
        found.add("impersonate")
    if groups & {"rbac.authorization.k8s.io", "*"} and resources & {"roles", "clusterroles", "*"}:
        found |= {"bind", "escalate"} if "*" in verbs else verbs & {"bind", "escalate"}
    return found


def _namespace(args: tuple[str, ...]) -> str:
    """The namespace a render with ``args`` installs into, which a namespaced subject must name to be one of its pods'."""
    return args[args.index("--namespace") + 1] if "--namespace" in args else "default"


@functools.cache
def _bindings(*args: str) -> tuple[_Binding, ...]:
    docs = [doc for _, doc in _documents(*args)]
    namespace = _namespace(args)
    roles = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs if doc.get("kind") in {"Role", "ClusterRole"}}
    found: list[_Binding] = []
    for doc in docs:
        if doc.get("kind") not in {"RoleBinding", "ClusterRoleBinding"}:
            continue
        ref = (doc["roleRef"]["kind"], doc["roleRef"]["name"])
        if ref in roles:
            # An aggregated role's rules are written by the cluster from every role its selectors match, so no render holds them.
            assert "aggregationRule" not in roles[ref], f"{doc['kind']}/{doc['metadata']['name']} binds {ref}, whose rules the cluster aggregates"
            grants = frozenset().union(*(_rule_grants(rule) for rule in roles[ref].get("rules") or []))
        elif ref in _BUILTIN_GRANTS:
            grants = _BUILTIN_GRANTS[ref]
        else:
            pytest.fail(f"{doc['kind']}/{doc['metadata']['name']} binds {ref}, which neither the render nor _BUILTIN_GRANTS describes")
        subjects = doc.get("subjects") or []
        accounts = [s["name"] for s in subjects if s.get("kind") == "ServiceAccount" and s.get("namespace", namespace) == namespace]
        users = (_SERVICE_ACCOUNT_USER.match(s.get("name", "")) for s in subjects if s.get("kind") == "User")
        accounts += [user["name"] for user in users if user and user["namespace"] == namespace]
        found.append(
            _Binding(
                name=f"{doc['kind']}/{doc['metadata']['name']}",
                role=ref,
                grants=grants,
                service_accounts=tuple(accounts),
                groups=tuple(s["name"] for s in subjects if s.get("kind") == "Group"),
            )
        )
    return tuple(found)


def _absent_when_it_starts(pod: _Pod, accounts: dict[str, tuple[frozenset[str], int]]) -> list[str]:
    """Where the SA a pod names does not exist when the pod starts: a hook-only SA exists only during its own
    phases, from its weight on; an ordinary one arrives with the manifest, after the pre-* hooks, unless it is
    established, when only a fresh install's pre-install hooks run before it exists."""
    phases, weight = accounts[pod.service_account]
    if not phases:
        before = _BEFORE_THE_MANIFEST - {"pre-upgrade"} if pod.service_account in _ESTABLISHED else _BEFORE_THE_MANIFEST
        return sorted(pod.hook_phases & before)
    if not pod.hook_phases:
        return ["the release manifest"]
    return [phase for phase in sorted(pod.hook_phases) if not (phase in phases and weight < pod.hook_weight)]


def _pod(args: tuple[str, ...], workload: str) -> _Pod:
    matches = [pod for pod in _pods(*args) if pod.workload == workload]
    assert len(matches) == 1, f"expected one pod template for {workload}, rendered {len(matches)}"
    return matches[0]


def _offending(args: tuple[str, ...]) -> list[str]:
    """Every grant of `secrets`, `tokenreviews` or a node's or pod's interior that reaches an SA a first-party pod runs as."""
    ours = {pod.service_account for pod in _pods(*args) if pod.first_party} | {"default"}
    found: list[str] = []
    for binding in _bindings(*args):
        if not binding.grants:
            continue
        for group in binding.groups:
            if _EVERY_SERVICE_ACCOUNT.match(group):
                found.append(f"{binding.name} grants {sorted(binding.grants)} to group {group}")
        for account in binding.service_accounts:
            if account in ours:
                found.append(f"{binding.name} grants {sorted(binding.grants)} to SA {account}")
    return found


@pytest.mark.parametrize("overlay", sorted(_OVERLAYS))
def test_no_first_party_service_account_can_read_a_secret_review_a_token_or_reach_into_a_node(overlay: str) -> None:
    """No grant of `secrets`, `tokenreviews` or a subresource reaching inside a node or a pod (`nodes/proxy`, a pod's
    exec, attach or port-forward) reaches `default`, an every-SA group, or an SA a first-party pod runs as.

    The secret store is the platform's or the dev fixture's ([[XC-149]]), so no first-party pod reviews a token: ESO, a
    prerequisite ([[XC-004]]), logs in through the store's Kubernetes auth backend, and the dev store reviews that login
    with its OWN mounted token, so the fixture binds `system:auth-delegator` to the SA its pod runs as.
    """
    args = _OVERLAYS[overlay]
    pods, bindings = _pods(*args), _bindings(*args)
    delegated = [b.service_accounts for b in bindings if b.role == ("ClusterRole", "system:auth-delegator")]

    # The walk sees the chart's pods and the subcharts', and the classifier reads rendered roles; without these every
    # assertion below passes by parsing nothing.
    assert sum(pod.first_party for pod in pods) >= 7, f"{overlay}: only {sum(p.first_party for p in pods)} first-party pod templates"
    # OpenFGA is the chart's one subchart and production names the platform's ([[XC-132]]), so the overlays that run it
    # prove the split.
    if overlay != "prod":
        assert any(not pod.first_party for pod in pods), f"{overlay}: no subchart pods rendered; the source split is broken"
    # The render holds no Role or ClusterRole of its own (the operators that needed one are prerequisites, ADR 0153, and
    # OpenFGA's subchart renders none, [[XC-156]]), so the proof that the classifier reads a rendered role is the planted
    # binding of the module's mutation check. Production binds no role to any pod: every workload that called the
    # Kubernetes API there is the platform's.
    if overlay != "prod":
        assert bindings, f"{overlay}: the walk found no binding at all"

    assert _offending(args) == [], f"{overlay}: {_offending(args)}"
    # The dev store is the openbao fixture's ([[XC-131]]): rask binds no reviewer, and the fixture binds the SA its own pod
    # runs as, with that pod's token mounted.
    assert delegated == [], f"{overlay}: rask runs no OpenBao and still binds the auth-delegator to {delegated}"
    fixture = render_fixture("openbao")
    [pod] = [doc["spec"]["template"]["spec"] for doc in fixture if doc.get("kind") == "Deployment"]
    reviewers = [
        subject["name"]
        for doc in fixture
        if doc.get("kind") == "ClusterRoleBinding" and doc["roleRef"]["name"] == "system:auth-delegator"
        for subject in doc["subjects"]
    ]
    assert reviewers == [pod["serviceAccountName"]], f"the dev store's auth-delegator is bound to {reviewers}, its pod runs as {pod['serviceAccountName']}"
    assert pod.get("automountServiceAccountToken") is True, "the dev store mounts no token, so it cannot review ESO's login"


@pytest.mark.parametrize("overlay", sorted(_OVERLAYS))
def test_every_first_party_service_runs_as_its_own_service_account(overlay: str) -> None:
    """One SA per service is the identity D1 authenticates, and nothing else in the namespace shares it.

    No pod the release runs, a subchart's included, is on `default`, the identity every unnamed pod
    shares. Every SA a pod names exists when the pod is created: a pod naming an absent SA is refused at
    admission and its workload never starts, and a hook holds the whole upgrade until its timeout. A
    one-shot Job never borrows a service's identity, no identity (an SA, a role or a binding) is a hook, and a
    first-party pod mounts an API token only when a
    binding gives its SA something to do with it, since an idle token is only a credential to steal. A subchart's pod
    mounts none: OpenFGA, the one subchart, calls the Kubernetes API for nothing ([[XC-156]]). The D1 verifiers (LH-220) validate SA tokens offline against the issuer's JWKS, which the API
    server serves only to a grant.
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
        "pods that start before their SA exists": sorted(
            f"{pod.workload} ({where}) -> {pod.service_account}"
            for pod in pods
            if pod.service_account in accounts
            for where in _absent_when_it_starts(pod, accounts)
        ),
        "services sharing an SA": {sa: sorted(ws) for sa, ws in services.items() if len(ws) > 1 and sa not in _SHARED_IDENTITIES},
        "one-shot pods on a service's SA": sorted(
            f"{pod.workload} -> {pod.service_account}" for pod in first_party if not pod.long_running and pod.service_account in services
        ),
        "an API token no grant uses": sorted(
            f"{pod.workload} ({pod.service_account})" for pod in first_party if pod.token_mounted and pod.service_account not in granted
        ),
        "a subchart's pod holding an API token": sorted(f"{pod.workload} ({pod.service_account})" for pod in pods if pod.token_mounted and not pod.first_party),
        "D1 verifiers that cannot read the SA issuer": sorted(verifiers - issuer_readers),
        "issuer discovery granted to `default`": sorted(issuer_readers & {"default"}),
        "identities rendered as hooks": sorted(
            f"{doc['kind']}/{doc['metadata']['name']}"
            for _, doc in _documents(*args)
            if doc.get("kind") in _IDENTITY_KINDS and "helm.sh/hook" in ((doc.get("metadata") or {}).get("annotations") or {})
        ),
    }

    found = {name: offenders for name, offenders in problems.items() if offenders}
    assert not found, f"{overlay}: {found}"
