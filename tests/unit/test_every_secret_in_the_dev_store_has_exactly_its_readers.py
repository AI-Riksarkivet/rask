"""Every secret in the dev store has exactly the readers it is for, no sidecar serves its app one, and no published token reads the store.

[[XC-165]] (#659), [[XC-199]]. A secret reaches a reader three ways in this chart, and the gate follows each through the dev
estate as `make k3s-up` renders it, with every plane that holds a credential switched on:

1. the store: an identity logs in to OpenBao and reads what its role's policy grants. An app logs in with the Kubernetes
   auth method as its pod's ServiceAccount, presenting a token projected for the role's audience into its own container,
   to the role its environment names (`lakehouse.governed.secrets`). Its sidecar logs in the same way, to the role its
   `lance-secrets` component names for its own app id (Dapr fills `{appID}` into a component's metadata,
   `pkg/runtime/meta/meta.go:96-97` in 1.18.1 and 1.19.0-rc.4), for what its components resolve by secretKeyRef (a pub/sub
   user). Both are one identity under one policy, so each read counts here. ESO logs in as its own account;
2. the Dapr scope: no app reads a secret through its sidecar's secrets API, since its Configuration denies the store by
   default and allows none. Dapr checks the scope there alone (`pkg/api/universal/secrets.go:35`); component resolution
   goes straight to the store (`pkg/runtime/processor/secret/secret.go:162-170`);
3. a Kubernetes Secret ESO writes from the store, as a pod mounts it, references it in env, or names it for its sidecar
   (`dapr.io/app-token-secret`, which the injector hands daprd as env).

The store's own pod holds every secret it serves and is not counted. What it holds the generated credentials and its own
root token in may reach no other pod, and no store token is published: none rides a component, an env value, an argument
or a probe header.

The store's root access key is named only where it is used or created: by the provisioning hook that writes each identity's
policy with it and prunes what git no longer declares, and by the dev store's seed, which writes it (ADR 0168).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import defaultdict
from collections.abc import Iterator
from typing import Final

import pytest

from tests.unit.chart_render import (
    DEFAULT_ARGS,
    PROD_ARGS,
    REPO,
    containers,
    render,
    render_chart_text,
    render_fixture,
    seed_scripts,
    store_identities,
    with_dev_store,
)


#: The dev estate as `make k3s-up` renders it, with the planes that hold a credential switched on.
DEV_ESTATE: Final = (
    "--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml"),
    "--set", "backups.pgDump.enabled=true", "--set", "backups.controlRoot.enabled=true",
)  # fmt: skip

#: The External Secrets Operator's own account, which values.yaml `externalSecrets` binds its role to.
ESO: Final = ("external-secrets", "external-secrets")
#: The store's own pod: it holds every secret it serves.
STORE_POD: Final = "Deployment/rask-openbao"
#: The KV v2 data path a policy grants a secret by.
_DATA_PATH: Final = re.compile(r"^secret/data/([^/*]+)$")

_DOORS: Final = frozenset({"catalog", "lineage", "maintenance"})
_SIDECARS: Final = frozenset(f"Deployment/rask-{app}" for app in ("catalog", "lineage", "maintenance", "maintenance-worker"))
_POSTGRES_CLIENTS: Final = frozenset({"StatefulSet/rask-age", "Deployment/rask-openfga", "Job/rask-openfga-migrate-r1", "CronJob/rask-pg-backup"})
_CATALOG_VERIFIERS: Final = frozenset({"catalog", "lineage", "maintenance"})
_MAINTENANCE_VERIFIERS: Final = frozenset({"catalog", "lineage", "maintenance"})

#: secret -> {"store": identities whose login reads it, "pods": workloads given it by ESO}. A "dapr" entry would be an app
#: whose sidecar API serves it the secret, and no secret has one.
EXPECTED: Final[dict[str, dict[str, frozenset[str]]]] = {
    "dapr-app-token": {"store": _DOORS | {"eso"}, "pods": _SIDECARS},
    "signing-key-rask-sa-catalog": {"store": frozenset({"catalog"})},
    # The key the catalog derives every announcement's run id under ([[LH-497]]): the catalog's alone.
    "commit-id-key": {"store": frozenset({"catalog"})},
    "signing-key-rask-sa-maintenance": {"store": frozenset({"maintenance"})},
    "signing-public-rask-sa-catalog": {"store": _CATALOG_VERIFIERS},
    "signing-public-rask-sa-maintenance": {"store": _MAINTENANCE_VERIFIERS},
    "nats-user-catalog": {"store": frozenset({"catalog"})},
    "nats-user-lineage": {"store": frozenset({"lineage"})},
    "nats-user-maintenance": {"store": frozenset({"maintenance"})},
    # The work queue's own bus user (ADR 0198 decision 5): the app reads it under its own login, as it reads its signing
    # key, where a component's user is read by the sidecar's.
    "nats-user-maintenance-worker": {"store": frozenset({"maintenance"})},
    "nats-user-admin": {"store": frozenset({"eso"}), "pods": frozenset({"Job/rask-nats-stream-r1"})},
    "nats-user-monitor": {"store": frozenset({"eso"}), "pods": frozenset({"Deployment/rask-nats-box"})},
    # The development store's own bus user: it publishes each write and delete of a rask bucket on store.events.
    "nats-user-object-store": {"store": frozenset({"eso"}), "pods": frozenset({"StatefulSet/rask-rustfs"})},
    "nats-server": {"store": frozenset({"eso"}), "pods": frozenset({"StatefulSet/rask-nats"})},
    "nats-route": {"store": frozenset({"eso"}), "pods": frozenset({"StatefulSet/rask-nats"})},
    "postgres": {"store": frozenset({"eso"}), "pods": _POSTGRES_CLIENTS},
    "object-store-root": {
        "store": frozenset({"eso"}),
        # The store itself, rask's provisioning hook, and the dev store's own operator stand-in that puts the event
        # configuration on every bucket (deploy/fixtures/rustfs, owner 2026-11-09).
        "pods": frozenset(
            {
                "StatefulSet/rask-rustfs",
                "CronJob/rask-rustfs-bucket-events",
                "Job/rask-object-store-provisioning-r1",
                "Job/rask-object-store-provisioning-prune-r1",
            }
        ),
    },
    "otel-collector-export": {"store": frozenset({"eso"}), "pods": frozenset({"Deployment/rask-otel-collector"})},
}

ROOT_ID_FIELD: Final = "object-store-access-key"


def _pods(docs: tuple[dict, ...]) -> Iterator[tuple[str, dict, dict]]:
    """`(kind/name, pod template metadata, pod spec)` for every workload shape."""
    for doc in docs:
        kind = doc.get("kind")
        if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
            template = doc["spec"]["template"]
        elif kind == "CronJob":
            template = doc["spec"]["jobTemplate"]["spec"]["template"]
        else:
            continue
        yield f"{kind}/{doc['metadata']['name']}", template.get("metadata") or {}, template["spec"]


def _documents(chart: tuple[dict, ...], prefix: str) -> dict[str, dict]:
    """The roles and policies rask renders for the store (`<release>-secret-store-identities`), by name: `policy-<name>.json`
    or `role-<name>.json`."""
    found = {}
    for key, text in store_identities(chart).items():
        if key.startswith(prefix) and key.endswith(".json"):
            found[key.removeprefix(prefix).removesuffix(".json")] = json.loads(text)
    return found


def _granted(policy: dict) -> set[str]:
    """The secrets a policy lets its holder read: KV v2 data paths with `read`."""
    granted = set()
    for path, rule in (policy.get("path") or {}).items():
        match = _DATA_PATH.match(path)
        assert match or not path.startswith("secret/"), f"the gate reads only exact KV v2 data paths, not {path!r}"
        if match and "read" in rule.get("capabilities", []):
            granted.add(match.group(1))
    return granted


def _audiences(pod: dict) -> dict[str, str]:
    """Each projected ServiceAccount token the pod asks the kubelet for: volume name -> audience."""
    found = {}
    for volume in pod.get("volumes") or []:
        for source in (volume.get("projected") or {}).get("sources") or []:
            if token := source.get("serviceAccountToken"):
                found[volume["name"]] = token.get("audience", "")
    return found


def _store_readers(estate: tuple[dict, ...], chart: tuple[dict, ...], namespace: str) -> tuple[dict[str, set[str]], list[str]]:
    """secret -> identities whose login reads it, and every way a login fails to be the app's own."""
    policies = {name: _granted(doc) for name, doc in _documents(chart, "policy-").items()}
    roles = _documents(chart, "role-")
    readers: dict[str, set[str]] = defaultdict(set)
    problems: list[str] = []
    for name, role in roles.items():
        bound = {(ns, sa) for ns in role.get("bound_service_account_namespaces") or [] for sa in role.get("bound_service_account_names") or []}
        audience = role.get("audience", "")
        holders: set[str] = set()
        if ESO in bound:
            holders.add("eso")
        for workload, metadata, pod in _pods(estate):
            if (namespace, pod.get("serviceAccountName")) in bound and audience in _audiences(pod).values():
                holders.add((metadata.get("annotations") or {}).get("dapr.io/app-id") or workload)
        if not audience and holders - {"eso"}:
            problems.append(f"role {name} takes a token of any audience, so any token its accounts hold logs in")
        for policy in role.get("token_policies") or []:
            assert policy in policies, f"role {name} grants policy {policy}, which rask does not render"
            for secret in policies[policy]:
                readers[secret] |= holders
    return readers, problems


def _logins(chart: tuple[dict, ...], estate: tuple[dict, ...]) -> list[str]:
    """Every app the store is scoped to logs in as its own role, twice: its own container, which its environment tells the
    store, the role and the mounts and which is given the projected token; and its daprd, through the one component. No
    other container holds the token."""
    [store] = [doc for doc in chart if doc.get("kind") == "Component" and doc["spec"]["type"] == "secretstores.hashicorp.vault"]
    meta = {row["name"]: row.get("value") for row in store["spec"].get("metadata") or []}
    problems = [f"lance-secrets carries {key}" for key in ("vaultToken", "vaultTokenMountPath") if key in meta]
    if meta.get("vaultAuthMethod") != "kubernetes" or "{appID}" not in str(meta.get("vaultKubernetesRole")):
        return [*problems, f"lance-secrets logs in as {meta.get('vaultAuthMethod')}/{meta.get('vaultKubernetesRole')}, not each sidecar as its own app"]
    roles = _documents(chart, "role-")
    token_file = str(meta["vaultServiceAccountTokenPath"])
    token_dir = token_file.rsplit("/", 1)[0]
    for workload, metadata, pod in _pods(estate):
        annotations = metadata.get("annotations") or {}
        app = annotations.get("dapr.io/app-id")
        if not app or app not in (store.get("scopes") or []):
            continue
        role_name = str(meta["vaultKubernetesRole"]).replace("{appID}", app)
        role = roles.get(role_name)
        if role is None or pod.get("serviceAccountName") not in (role.get("bound_service_account_names") or []):
            problems.append(f"{workload} logs in as a role rask does not bind to its account {pod.get('serviceAccountName')}")
            continue
        volumes = {name for name, audience in _audiences(pod).items() if audience == role.get("audience")}
        sidecar = {entry.split(":", 1)[0] for entry in annotations.get("dapr.io/volume-mounts", "").split(",") if entry.endswith(f":{token_dir}")}
        if not volumes & sidecar:
            problems.append(f"{workload}'s daprd is given no token for {role.get('audience')!r} at {token_dir}")
        [own] = pod["containers"]
        held = {
            container["name"]: mount["mountPath"]
            for container in [*(pod.get("initContainers") or []), *pod["containers"]]
            for mount in container.get("volumeMounts") or []
            if mount["name"] in volumes
        }
        if held != {own["name"]: token_dir}:
            problems.append(f"{workload} mounts its store token into {held}, not its own container alone at {token_dir}")
        told = {
            "RASK_SECRET_STORE_ADDR": meta["vaultAddr"],
            "RASK_SECRET_STORE_ROLE": role_name,
            "RASK_SECRET_STORE_AUTH_MOUNT": meta["vaultKubernetesMountPath"],
            "RASK_SECRET_STORE_KV_MOUNT": meta["enginePath"],
            "RASK_SECRET_STORE_TOKEN_FILE": token_file,
        }
        env = {row["name"]: row.get("value") for row in own.get("env") or []}
        if (given := {name: env.get(name) for name in told}) != told:
            problems.append(f"{workload}'s own container is told {given}, where its sidecar logs in with {told}")
    return problems


def _dapr_readers(chart: tuple[dict, ...], universe: set[str]) -> dict[str, set[str]]:
    """secret -> apps whose sidecar's secrets API serves it, as Dapr reads the pod's Configuration (`IsSecretAllowed`): none."""
    [store] = [doc for doc in chart if doc.get("kind") == "Component" and doc["spec"]["type"] == "secretstores.hashicorp.vault"]
    configurations = {doc["metadata"]["name"]: doc for doc in chart if doc.get("kind") == "Configuration"}
    readers: dict[str, set[str]] = defaultdict(set)
    for _, metadata, _ in _pods(chart):
        annotations = metadata.get("annotations") or {}
        app = annotations.get("dapr.io/app-id")
        if not app or app not in (store.get("scopes") or []):
            continue
        spec = configurations[annotations["dapr.io/config"]].get("spec") or {}
        rule = next((scope for scope in (spec.get("secrets") or {}).get("scopes") or [] if scope["storeName"] == store["metadata"]["name"]), {})
        allowed, denied = set(rule.get("allowedSecrets") or []), set(rule.get("deniedSecrets") or [])
        default = (rule.get("defaultAccess") or "allow").lower() == "allow"
        for secret in universe:
            if (secret in allowed) if allowed else (secret not in denied and default):
                readers[secret].add(app)
    return readers


def _carriers(estate: tuple[dict, ...]) -> tuple[dict[str, dict[str, set[str]]], set[str]]:
    """Each Kubernetes Secret ESO writes from the store -> its keys -> the store secrets they carry; and the Secrets it fills from a generator."""
    carriers: dict[str, dict[str, set[str]]] = {}
    generated: set[str] = set()
    for doc in estate:
        if doc.get("kind") != "ExternalSecret":
            continue
        spec = doc["spec"]
        target = (spec.get("target") or {}).get("name") or doc["metadata"]["name"]
        fields: dict[str, set[str]] = {}
        for source in spec.get("dataFrom") or []:
            assert not {"find", "rewrite"} & source.keys(), f"{target}: this gate does not model `dataFrom` {sorted(source)}"
            if (source.get("sourceRef") or {}).get("generatorRef"):
                generated.add(target)
            elif key := (source.get("extract") or {}).get("key"):
                fields["*"] = {key}
        for entry in spec.get("data") or []:
            fields[entry["secretKey"]] = {entry["remoteRef"]["key"]}
        template = (spec.get("target") or {}).get("template") or {}
        if template.get("data"):
            rendered = {
                key: {s for field, sources in fields.items() if f".{field} " in value or f".{field}}}" in value for s in sources}
                for key, value in template["data"].items()
            }
            fields = rendered | (fields if template.get("mergePolicy") == "Merge" else {})
        if fields:
            carriers[target] = fields
    return carriers, generated


def _container_reads(pod: dict, metadata: dict) -> dict[str, set[tuple[str, str]]]:
    """Each container -> the `(Secret, key)` pairs it reads, key `*` for every key; `daprd` for what the injector hands it."""
    volumes: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for volume in pod.get("volumes") or []:
        sources = [volume.get("secret"), *(source.get("secret") for source in (volume.get("projected") or {}).get("sources") or [])]
        for source in filter(None, sources):
            name = source.get("secretName") or source.get("name") or ""
            volumes[volume["name"]] |= {(name, item["key"]) for item in source["items"]} if source.get("items") else {(name, "*")}
    reads: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for container in [*(pod.get("initContainers") or []), *pod["containers"]]:
        own = reads[container["name"]]
        for mount in container.get("volumeMounts") or []:
            own |= volumes.get(mount["name"], set())
        for entry in container.get("env") or []:
            if ref := (entry.get("valueFrom") or {}).get("secretKeyRef"):
                own.add((ref["name"], ref["key"]))
        own |= {(source["secretRef"]["name"], "*") for source in container.get("envFrom") or [] if source.get("secretRef")}
    if token := (metadata.get("annotations") or {}).get("dapr.io/app-token-secret"):
        reads["daprd"].add((token, "token"))
    return reads


def _pod_readers(estate: tuple[dict, ...]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """secret -> workloads reading it through a Secret ESO writes; and each generated Secret -> the containers that read it."""
    carriers, generated = _carriers(estate)
    readers: dict[str, set[str]] = defaultdict(set)
    generated_readers: dict[str, set[str]] = defaultdict(set)
    for workload, metadata, pod in _pods(estate):
        for container, reads in _container_reads(pod, metadata).items():
            for name, key in reads:
                if name in generated:
                    generated_readers[name].add(f"{workload}/{container}")
                for field, secrets in carriers.get(name, {}).items():
                    if key in {"*", field} or field == "*":
                        for secret in secrets:
                            readers[secret].add(workload)
    return readers, generated_readers


def _published_tokens(estate: tuple[dict, ...]) -> list[str]:
    """Every place a store token is a literal of the render: an env value, an argument, a probe header."""
    found = []
    for workload, name, container in containers(estate):
        for entry in container.get("env") or []:
            if entry["name"] in {"BAO_TOKEN", "VAULT_TOKEN", "BAO_DEV_ROOT_TOKEN_ID", "VAULT_DEV_ROOT_TOKEN_ID"} and "value" in entry:
                found.append(f"{workload}/{name} env {entry['name']}")
        argv = [str(part) for part in [*(container.get("command") or []), *(container.get("args") or [])]]
        found += [f"{workload}/{name} runs {part}" for part in argv if part.startswith("-dev-root-token-id=") and part.split("=", 1)[1]]
        for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
            headers = ((container.get(probe) or {}).get("httpGet") or {}).get("httpHeaders") or []
            found += [f"{workload}/{name} {probe} sends {h['name']}" for h in headers if h["name"].lower() in {"x-vault-token", "authorization"}]
    return found


def _root_id(chart: tuple[dict, ...]) -> str:
    """The store's root access key, as the dev store's seed writes it into `object-store-root`."""
    [found] = set(re.findall(rf"\b{ROOT_ID_FIELD}=([^\s\"'\\]+)", seed_scripts(chart)["open-and-seed.sh"]))
    return found


def _names_the_root_id(container: dict, root_id: str) -> bool:
    """A literal root access key in the command line or env, or an access key taken from the fleet's `-app` Secret,
    which carries the root's (`chart/templates/secrets.yaml`)."""
    literals = [*(container.get("command") or []), *(container.get("args") or []), *(entry.get("value") for entry in container.get("env") or [])]
    from_app = (((entry.get("valueFrom") or {}).get("secretKeyRef") or {}) for entry in container.get("env") or [])
    return any(root_id in str(literal) for literal in literals) or any(
        ref.get("name", "").endswith("-app") and ref.get("key") == "AWS_ACCESS_KEY_ID" for ref in from_app
    )


def test_every_secret_in_the_dev_store_has_exactly_its_readers_and_no_published_token_reads_it() -> None:
    chart = render(*DEV_ESTATE)
    estate = (*with_dev_store(chart), *render_fixture("rustfs"), *render_fixture("nats", "--set", "nats.auth.server=true"))
    namespace = "default"

    store, unbound = _store_readers(estate, chart, namespace)
    pods, generated = _pod_readers(estate)
    universe = set(store) | set(pods) | set(EXPECTED)
    dapr = _dapr_readers(chart, universe)
    columns = {"store": store, "dapr": dapr, "pods": pods}
    actual = {
        secret: found
        for secret in universe
        if (found := {column: frozenset(readers[secret] - {STORE_POD}) for column, readers in columns.items() if readers.get(secret, set()) - {STORE_POD}})
    }
    presenters = {f"{workload}/{name}" for workload, name, container in containers(estate) if _names_the_root_id(container, _root_id(chart))}

    assert not unbound, unbound
    assert actual == EXPECTED, {
        secret: {"actual": {k: sorted(v) for k, v in actual.get(secret, {}).items()}, "expected": {k: sorted(v) for k, v in EXPECTED.get(secret, {}).items()}}
        for secret in sorted(set(actual) | set(EXPECTED))
        if actual.get(secret) != EXPECTED.get(secret)
    }
    assert not (logins := _logins(chart, estate)), logins
    assert {name: sorted(held) for name, held in generated.items() if held != {f"{STORE_POD}/seed"}} == {}, (
        "a generated credential reaches a container beside the seed"
    )
    assert not (published := _published_tokens(estate)), f"a store token is a literal of the render: {published}"
    assert presenters == {
        f"{STORE_POD}/seed",
        "Job/rask-object-store-provisioning-r1/provision",
        "Job/rask-object-store-provisioning-prune-r1/provision",
    }, sorted(presenters)


def _own_logins(chart: tuple[dict, ...]) -> dict[str, tuple[str | None, list[str], list[str]]]:
    """workload -> (the role its own container is told, the secrets that role's policy grants, where its store token is
    mounted), for every workload told a secret store."""
    policies = {name: _granted(doc) for name, doc in _documents(chart, "policy-").items()}
    roles = _documents(chart, "role-")
    found = {}
    for workload, _metadata, pod in _pods(chart):
        [own] = pod["containers"][:1]
        env = {row["name"]: row.get("value") for row in own.get("env") or []}
        if "RASK_SECRET_STORE_ADDR" not in env:
            continue
        role = env.get("RASK_SECRET_STORE_ROLE")
        bound = (roles.get(str(role)) or {}).get("bound_service_account_names") or []
        granted = sorted(policies.get(str(role), set())) if pod.get("serviceAccountName") in bound else []
        mounts = sorted(mount["mountPath"] for mount in own.get("volumeMounts") or [] if mount["name"] in _audiences(pod))
        found[workload] = (role, granted, [path for path in mounts if env.get("RASK_SECRET_STORE_TOKEN_FILE", "").startswith(f"{path}/")])
    return found


def test_each_service_keeps_its_store_role_and_its_own_login_where_no_sidecar_runs() -> None:
    """The store's readers are the services, whatever carries the bus ([[XC-199]]): with no sidecar each still has its role,
    bound to its own account under a policy naming its own reads, the store in its environment and its projected token.
    Each role's token may also ask the store about itself, which is how its service learns that the store dropped it: the
    roles carry no `default` policy, so the rendered one is what grants the question."""
    chart = render(*DEFAULT_ARGS, "--set", "dapr.enabled=false", "--set", "catalog.controlEmit=false")

    token = ["/var/run/secrets/rask/openbao"]
    assert _own_logins(chart) == {
        "Deployment/rask-catalog": ("rask-catalog", ["commit-id-key", "signing-key-rask-sa-catalog", "signing-public-rask-sa-catalog"], token),
        "Deployment/rask-lineage": ("rask-lineage", ["signing-public-rask-sa-catalog", "signing-public-rask-sa-maintenance"], token),
        "Deployment/rask-maintenance": (
            "rask-maintenance",
            ["signing-key-rask-sa-maintenance", "signing-public-rask-sa-catalog", "signing-public-rask-sa-maintenance"],
            token,
        ),
    }
    services = ("rask-catalog", "rask-lineage", "rask-maintenance")
    policies, roles = _documents(chart, "policy-"), _documents(chart, "role-")
    assert {name: policies[name]["path"].get("auth/token/lookup-self") for name in services} == {name: {"capabilities": ["read"]} for name in services}
    assert {name: roles[name].get("token_no_default_policy") for name in services} == dict.fromkeys(services, True)


def test_a_private_ca_named_for_the_platform_store_reaches_every_reader_of_it() -> None:
    """A platform store under a private CA is verified by each of its three readers against the ConfigMap the deployment
    names: each service (Python trusts its own bundle, never the node's), each sidecar, and ESO."""
    chart = render(*PROD_ARGS, "--set-string", "openbao.ca.configMap=platform-ca", "--set-string", "openbao.ca.key=bundle.pem")

    [component] = [doc for doc in chart if doc.get("kind") == "Component" and doc["spec"]["type"] == "secretstores.hashicorp.vault"]
    [secret_store] = [doc for doc in chart if doc.get("kind") == "SecretStore"]
    readers = {}
    for workload, metadata, pod in _pods(chart):
        [own] = pod["containers"][:1]
        env = {row["name"]: row.get("value") for row in own.get("env") or []}
        if "RASK_SECRET_STORE_ADDR" in env:
            readers[workload] = (
                env.get("RASK_SECRET_STORE_CA_FILE"),
                [mount["mountPath"] for mount in own.get("volumeMounts") or [] if mount["name"] == "secret-store-ca"],
                [volume.get("configMap") for volume in pod.get("volumes") or [] if volume["name"] == "secret-store-ca"],
                "secret-store-ca:/etc/rask/secret-store-ca" in (metadata.get("annotations") or {}).get("dapr.io/volume-mounts", "").split(","),
            )
    mounted = (
        "/etc/rask/secret-store-ca/ca.crt",
        ["/etc/rask/secret-store-ca"],
        [{"name": "platform-ca", "items": [{"key": "bundle.pem", "path": "ca.crt"}]}],
        True,
    )

    assert readers == {f"Deployment/rask-{name}": mounted for name in ("catalog", "lineage", "maintenance", "maintenance-worker")}
    assert {row["name"]: row.get("value") for row in component["spec"]["metadata"]}.get("caCert") == "/etc/rask/secret-store-ca/ca.crt"
    assert secret_store["spec"]["provider"]["vault"].get("caProvider") == {"type": "ConfigMap", "name": "platform-ca", "key": "bundle.pem"}


def test_a_ca_named_beside_the_development_store_is_refused() -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_chart_text(REPO / "chart", *DEV_ESTATE, "--set-string", "openbao.ca.configMap=platform-ca")
    assert "openbao.ca names the CA of the platform's secret store" in refused.value.stderr


@pytest.mark.parametrize(
    ("value", "refusal"),
    [
        pytest.param(
            "openbao.identity.tokenTtl=61m", 'openbao.identity.tokenTtl is "61m": a store token lives at most an hour', id="a-token-ttl-past-the-hour"
        ),
        pytest.param(
            "openbao.identity.tokenMaxTtl=2h", 'openbao.identity.tokenMaxTtl is "2h": a store token lives at most an hour', id="a-max-ttl-past-the-hour"
        ),
        pytest.param("openbao.identity.tokenTtl=1h30m", 'openbao.identity.tokenTtl is "1h30m": write it as', id="a-form-the-chart-cannot-read"),
        # Helm reads a number with a leading zero as octal (2560), and the store reads it as written (5000 s).
        pytest.param(
            "openbao.identity.tokenTtl=05000s", 'openbao.identity.tokenTtl is "05000s": write it as', id="a-number-helm-and-the-store-read-differently"
        ),
        # A product past 64 bits wraps: this many hours renders as 64 s unless the number's length is held.
        pytest.param(
            "openbao.identity.tokenTtl=1132425122302725252h",
            'openbao.identity.tokenTtl is "1132425122302725252h": write it as',
            id="a-number-whose-seconds-wrap-around",
        ),
        pytest.param("openbao.identity.tokenTtl=0", 'openbao.identity.tokenTtl is "0": a role with no lifetime of its own', id="no-lifetime-of-its-own"),
        pytest.param(
            "openbao.identity.tokenTtl=59s", 'openbao.identity.tokenTtl is "59s": a store token lives at least a minute', id="a-token-ttl-under-the-minute"
        ),
        pytest.param(
            "openbao.identity.tokenMaxTtl=10m",
            'openbao.identity.tokenMaxTtl is "10m", shorter than tokenTtl "15m"',
            id="a-max-ttl-under-the-ttl",
        ),
    ],
)
def test_a_store_token_lifetime_a_service_would_refuse_fails_the_render(value: str, refusal: str) -> None:
    """A service takes a store token whose lease is a minute to an hour, so the render refuses the role that would issue
    any other: a lifetime past the hour, one under the minute its loop needs to keep the login, one written in a form the
    chart cannot read, and none at all, which leaves the store's default of weeks. It refuses a `tokenMaxTtl` under the
    `tokenTtl` as well, since the store then issues the shorter one and the role is not what git declares. The hour
    itself, the default `tokenMaxTtl`, renders in every other test here."""
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_chart_text(REPO / "chart", *DEV_ESTATE, "--set-string", value)
    assert refusal in refused.value.stderr


def test_a_forwarder_key_name_that_is_no_secret_name_is_refused() -> None:
    """The one secret name an operator supplies is a path of lineage's store policy and a segment of the path lineage
    reads: a wildcard there would hand lineage every secret in the store, so the render refuses what is no secret name."""
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_chart_text(REPO / "chart", *DEV_ESTATE, "--set-string", "services.lineage.forward.authSecret.name=*")
    assert 'services.lineage.forward.authSecret.name: "*" is no secret name' in refused.value.stderr


#: A client's store reader as a client's own values supply it (`openbao.externalReaders`): its bus user, a name of its
#: own and a signer's public key, which every verifier may read: the three kinds a reader may hold.
_CLIENT_READER: Final = '{"client":{"serviceAccount":"rask-sa-client","secrets":["nats-user-client","client-token","signing-public-rask-sa-catalog"]}}'


@pytest.mark.parametrize(
    ("app", "service_account", "secrets", "refusal"),
    [
        pytest.param("Client_1", "rask-sa-client", '["client-x"]', '"Client_1" is no Dapr app id', id="no-dapr-app-id"),
        pytest.param("client", "rask-sa-client", '["*"]', '"*" is no secret name', id="a-wildcard"),
        pytest.param("client", "rask-sa-client", '["a/b"]', '"a/b" is no secret name', id="a-path"),
        pytest.param("client", "rask-sa-client", '["nats-root"]', '"nats-root" is not the reader\'s own', id="the-bus-trust-root"),
        pytest.param(
            "client", "rask-sa-client", '["signing-key-rask-sa-catalog"]', '"signing-key-rask-sa-catalog" is not the reader\'s own', id="a-signer-key"
        ),
        pytest.param(
            "openbao",
            "rask-sa-client",
            '["openbao-seeded"]',
            '"openbao" is a reserved app id: its own names would cover openbao-seeded',
            id="an-app-id-covering-a-seeded-name",
        ),
        pytest.param(
            "nats-user-foo",
            "rask-sa-client",
            '["nats-user-foo-bar"]',
            '"nats-user-foo" is a reserved app id: its own names (nats-user-foo-<name>) would cover the store\'s nats-user-<name> secrets',
            id="an-app-id-covering-another-bus-user",
        ),
        pytest.param("catalog", "rask-sa-client", '["catalog-x"]', '"catalog" is the lakehouse\'s own app', id="a-lakehouse-app-id"),
        pytest.param("client", "Rask_SA", '["client-x"]', "client.serviceAccount names no ServiceAccount", id="a-bad-service-account"),
        pytest.param("client", "default", '["client-x"]', 'client.serviceAccount: "default" is the lakehouse\'s own', id="the-shared-default-account"),
        pytest.param("client", "rask-sa-catalog", '["client-x"]', 'client.serviceAccount: "rask-sa-catalog" is the lakehouse\'s own', id="a-lakehouse-account"),
        pytest.param("client", "rask-sa-client", "[]", "client.secrets names no secret", id="no-secret"),
    ],
)
def test_a_client_store_reader_is_refused_any_read_that_is_not_its_own(app: str, service_account: str, secrets: str, refusal: str) -> None:
    reader = f'openbao.externalReaders={{"{app}":{{"serviceAccount":"{service_account}","secrets":{secrets}}}}}'
    #: A client's bus user, as a client's own values add it to the issued table.
    bus_user = 'nats.auth.users.foo-bar={"publish":["dlq.foo-bar"],"subscribe":["_INBOX.>"]}'
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_chart_text(REPO / "chart", *DEV_ESTATE, "--set-json", bus_user, "--set-json", reader)
    assert refusal in refused.value.stderr


@pytest.mark.parametrize(
    ("readers", "values", "refusal"),
    [
        pytest.param(
            {"client": ["client-x"], "client-x": ["client-x-token"]},
            (),
            '"client" is a reserved app id: its own names would cover client-x\'s names',
            id="another-readers-id-as-a-prefix",
        ),
        pytest.param(
            {"client": ["client-x"]},
            ("--set", "externalSecrets.role=rask-client"),
            '"client" would log in as rask-client, the External Secrets Operator\'s own role',
            id="the-operators-role",
        ),
    ],
)
def test_a_client_store_reader_is_refused_an_identity_that_is_not_its_own(readers: dict[str, list[str]], values: tuple[str, ...], refusal: str) -> None:
    supplied = {app: {"serviceAccount": f"sa-{app}", "secrets": secrets} for app, secrets in readers.items()}
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_chart_text(REPO / "chart", *DEV_ESTATE, *values, "--set-json", f"openbao.externalReaders={json.dumps(supplied)}")
    assert refusal in refused.value.stderr


def test_a_client_store_reader_is_granted_exactly_what_its_values_name() -> None:
    chart = render(*DEV_ESTATE, "--set-json", f"openbao.externalReaders={_CLIENT_READER}")

    policies = _documents(chart, "policy-")
    roles = _documents(chart, "role-")

    assert _granted(policies["rask-client"]) == {"nats-user-client", "client-token", "signing-public-rask-sa-catalog"}
    assert roles["rask-client"]["bound_service_account_names"] == ["rask-sa-client"]
    # A client's sidecar runs its own release's Configuration: the product chart renders none for it.
    assert _configurations(chart) == _configurations(render(*DEV_ESTATE))


def _configurations(chart: tuple[dict, ...]) -> list[str]:
    return sorted(doc["metadata"]["name"] for doc in chart if doc.get("kind") == "Configuration")
