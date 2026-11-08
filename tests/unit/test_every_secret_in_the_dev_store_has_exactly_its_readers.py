"""Every secret in the dev store has exactly the readers it is for, at the store and at the Dapr scope, and no published token reads the store.

[[XC-165]] (#659). A secret reaches a reader three ways in this chart, and the gate follows each through the dev estate as
`make k3s-up` renders it, with every plane that holds a credential switched on:

1. the store: an identity logs in to OpenBao and reads what its role's policy grants. An app's sidecar logs in with the
   Kubernetes auth method as its pod's ServiceAccount, presenting a token projected for the role's audience into daprd
   alone, to the role its `lance-secrets` component names for its own app id (Dapr fills `{appID}` into a component's
   metadata, `pkg/runtime/meta/meta.go:96-97` in 1.18.1 and 1.19.0-rc.4). A component that resolves its own secretKeyRef (a
   pub/sub user, the state store's DSN) reads with that same login, so its reads count here. ESO logs in as its own account;
2. the Dapr scope: an app reads, through its sidecar's secrets API, only what its Configuration allows. Dapr checks the
   scope there alone (`pkg/api/universal/secrets.go:35`); component resolution goes straight to the store
   (`pkg/runtime/processor/secret/secret.go:162-170`);
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

from tests.unit.chart_render import REPO, containers, render, render_chart_text, render_fixture, seed_scripts, store_identities, with_dev_store


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
_SIDECARS: Final = frozenset(f"Deployment/rask-{app}" for app in ("catalog", "lineage", "maintenance"))
_POSTGRES_CLIENTS: Final = frozenset({"StatefulSet/rask-age", "Deployment/rask-openfga", "Job/rask-openfga-migrate-r1", "CronJob/rask-pg-backup"})
_CATALOG_VERIFIERS: Final = frozenset({"catalog", "lineage", "maintenance"})
_MAINTENANCE_VERIFIERS: Final = frozenset({"catalog", "lineage", "maintenance"})

#: secret -> {"store": identities whose login reads it, "dapr": apps whose sidecar API serves it, "pods": workloads given it by ESO}.
EXPECTED: Final[dict[str, dict[str, frozenset[str]]]] = {
    "dapr-app-token": {"store": _DOORS | {"eso"}, "dapr": _DOORS, "pods": _SIDECARS},
    "signing-key-rask-sa-catalog": {"store": frozenset({"catalog"}), "dapr": frozenset({"catalog"})},
    # The key the catalog derives every announcement's run id under ([[LH-497]]): the catalog's alone.
    "commit-id-key": {"store": frozenset({"catalog"}), "dapr": frozenset({"catalog"})},
    "signing-key-rask-sa-maintenance": {"store": frozenset({"maintenance"}), "dapr": frozenset({"maintenance"})},
    "signing-public-rask-sa-catalog": {"store": _CATALOG_VERIFIERS, "dapr": _CATALOG_VERIFIERS},
    "signing-public-rask-sa-maintenance": {"store": _MAINTENANCE_VERIFIERS, "dapr": _MAINTENANCE_VERIFIERS},
    "nats-user-catalog": {"store": frozenset({"catalog"})},
    "nats-user-lineage": {"store": frozenset({"lineage"})},
    "nats-user-maintenance": {"store": frozenset({"maintenance"})},
    "nats-user-admin": {"store": frozenset({"eso"}), "pods": frozenset({"Job/rask-nats-stream-r1"})},
    "nats-user-monitor": {"store": frozenset({"eso"}), "pods": frozenset({"Deployment/rask-nats-box"})},
    "nats-server": {"store": frozenset({"eso"}), "pods": frozenset({"StatefulSet/rask-nats"})},
    "nats-route": {"store": frozenset({"eso"}), "pods": frozenset({"StatefulSet/rask-nats"})},
    "postgres": {"store": frozenset({"eso"}), "pods": _POSTGRES_CLIENTS},
    "object-store-root": {
        "store": frozenset({"eso"}),
        "pods": frozenset({"StatefulSet/rask-rustfs", "Job/rask-object-store-provisioning-r1", "Job/rask-object-store-provisioning-prune-r1"}),
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
    """Every Dapr app the store is scoped to logs in as its own role, with a token delivered to its daprd alone."""
    [store] = [doc for doc in chart if doc.get("kind") == "Component" and doc["spec"]["type"] == "secretstores.hashicorp.vault"]
    meta = {row["name"]: row.get("value") for row in store["spec"].get("metadata") or []}
    problems = [f"lance-secrets carries {key}" for key in ("vaultToken", "vaultTokenMountPath") if key in meta]
    if meta.get("vaultAuthMethod") != "kubernetes" or "{appID}" not in str(meta.get("vaultKubernetesRole")):
        return [*problems, f"lance-secrets logs in as {meta.get('vaultAuthMethod')}/{meta.get('vaultKubernetesRole')}, not each sidecar as its own app"]
    roles = _documents(chart, "role-")
    token_dir = str(meta["vaultServiceAccountTokenPath"]).rsplit("/", 1)[0]
    for workload, metadata, pod in _pods(estate):
        annotations = metadata.get("annotations") or {}
        app = annotations.get("dapr.io/app-id")
        if not app or app not in (store.get("scopes") or []):
            continue
        role = roles.get(str(meta["vaultKubernetesRole"]).replace("{appID}", app))
        if role is None or pod.get("serviceAccountName") not in (role.get("bound_service_account_names") or []):
            problems.append(f"{workload} logs in as a role rask does not bind to its account {pod.get('serviceAccountName')}")
            continue
        volumes = {name for name, audience in _audiences(pod).items() if audience == role.get("audience")}
        sidecar = {entry.split(":", 1)[0] for entry in annotations.get("dapr.io/volume-mounts", "").split(",") if entry.endswith(f":{token_dir}")}
        if not volumes & sidecar:
            problems.append(f"{workload}'s daprd is given no token for {role.get('audience')!r} at {token_dir}")
        held = {
            container["name"]
            for container in [*(pod.get("initContainers") or []), *pod["containers"]]
            for mount in container.get("volumeMounts") or []
            if mount["name"] in volumes
        }
        if held:
            problems.append(f"{workload} mounts its store token into {sorted(held)}, not daprd alone")
    return problems


def _dapr_readers(chart: tuple[dict, ...], universe: set[str]) -> dict[str, set[str]]:
    """secret -> apps whose sidecar's secrets API serves it, as Dapr reads the pod's Configuration (`IsSecretAllowed`)."""
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
