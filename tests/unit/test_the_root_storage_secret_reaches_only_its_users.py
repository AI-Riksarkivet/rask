"""The store's root secret reaches the store, the dev secret store that seeds it, and the scoped-users hook, nothing else.

On RustFS the root secret also signs every STS session token the store issues (GHSA-m77q-r63m-pj89, intentionally
unfixed: `rustfs@1.0.3:crates/iam/src/root_credentials.rs:25-36`), so a workload that can read it can forge any credential
the catalog vends (ADR 0168). A secret reaches a pod three ways in this chart, and the gate follows all three through the
dev estate as `make k3s-up` renders it, with every plane that holds a store credential switched on:

1. a Secret the pod mounts or references (a volume, `env.valueFrom.secretKeyRef`, `envFrom`);
2. a Secret ESO fills (`data`, `dataFrom.extract`) from where the dev store holds the root, read off the seeds rask
   renders for it (`bao kv put secret/<name> ...`) by the value written, the generated file or the shell variable the
   seed holds it in, whatever the property is called; or from the Password generator that mints it;
3. the Dapr secret store: an app scoped to it reads every secret its Configuration does not deny
   (`defaultAccess: allow` with `deniedSecrets`, `lance.daprAppSpec`), with every field of that secret.

The same holds for the root's identity: a pod that presents the root access key with any secret is either root or
broken, so only the hook names it, and the dev secret store's seed, which writes it.

The gate follows what the chart hands a pod, not what the dev store's own token reaches: the dev OpenBao runs
`server -dev` with a published root token, so a pod that calls it directly reads every secret it holds. Per-app
secret-store identities, which close that, are XC-165 (#659; ADR 0168).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Final

from tests.unit.chart_render import REPO, containers, render, render_fixture, seed_scripts, with_dev_store


ROOT_SECRET: Final = "object-store-secret-key"
ROOT_ID: Final = "object-store-access-key"

#: The dev estate as `make k3s-up` renders it, with the planes that hold a store credential switched on.
DEV_ESTATE: Final = (
    "--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml"),
    "--set", "explorer.enabled=true", "--set", "search.enabled=true",
    "--set", "backups.pgDump.enabled=true", "--set", "backups.controlRoot.enabled=true",
)  # fmt: skip

#: `bao kv put secret/<name> <property>=<value> ...`, one command per logical line of the rendered seed.
_KV_PUT: Final = re.compile(r"bao kv put secret/(\S+)((?: +[\w-]+=\S+)+)")
#: A seeded value that IS the root: the generated file (`$G` mounts the Password generator's Secret) or `$root`, which
#: the seed derives the scoped secrets from.
_ROOT_VALUE: Final = re.compile(r"\$G/object-store-secret-key\b|\$\{?root\b")


def _seeded(script: str) -> dict[str, dict[str, str]]:
    """Each secret the dev store's seed writes, with its properties and their literal values."""
    seeded: dict[str, dict[str, str]] = {}
    for match in _KV_PUT.finditer(script.replace("\\\n", " ")):
        pairs = (token.split("=", 1) for token in match.group(2).split())
        seeded.setdefault(match.group(1), {}).update(dict(pairs))
    return seeded


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


def _carriers(docs: tuple[dict, ...], root_fields: frozenset[tuple[str, str]]) -> dict[str, frozenset[str]]:
    """Each Kubernetes Secret that carries the root secret, with the keys in it that do."""
    root_paths = {path for path, _ in root_fields}
    minted = {doc["metadata"]["name"] for doc in docs if doc.get("kind") == "Password" and ROOT_SECRET in (doc["spec"].get("secretKeys") or [])}
    carriers: dict[str, frozenset[str]] = {}
    for doc in docs:
        if doc.get("kind") != "ExternalSecret":
            continue
        spec = doc["spec"]
        target = (spec.get("target") or {}).get("name") or doc["metadata"]["name"]
        bound: set[str] = set()
        for source in spec.get("dataFrom") or []:
            assert not {"find", "rewrite"} & source.keys(), f"{target}: this gate does not model `dataFrom` {sorted(source)}"
            if ((source.get("sourceRef") or {}).get("generatorRef") or {}).get("name") in minted:
                bound.add(ROOT_SECRET)
            path = (source.get("extract") or {}).get("key")
            bound |= {prop for field_path, prop in root_fields if field_path == path}
        for entry in spec.get("data") or []:
            ref = entry["remoteRef"]
            if (ref["key"], ref.get("property")) in root_fields or (ref["key"] in root_paths and not ref.get("property")):
                bound.add(entry["secretKey"])
        if not bound:
            continue
        template = (spec.get("target") or {}).get("template") or {}
        rendered = {key for key, value in (template.get("data") or {}).items() if any(var in value for var in bound)}
        kept = bound if not template.get("data") or template.get("mergePolicy") == "Merge" else set()
        carriers[target] = frozenset(rendered | kept)
    return carriers


def _reads_a_carrier(pod: dict, carriers: dict[str, frozenset[str]]) -> bool:
    for volume in pod.get("volumes") or []:
        sources = [volume.get("secret"), *(source.get("secret") for source in (volume.get("projected") or {}).get("sources") or [])]
        for source in filter(None, sources):
            keys = carriers.get(source.get("secretName") or source.get("name") or "")
            if keys is not None and (not source.get("items") or any(item["key"] in keys for item in source["items"])):
                return True
    for container in [*(pod.get("initContainers") or []), *pod["containers"]]:
        for entry in container.get("env") or []:
            ref = (entry.get("valueFrom") or {}).get("secretKeyRef") or {}
            if ref.get("key") in carriers.get(ref.get("name", ""), frozenset()):
                return True
        if any((source.get("secretRef") or {}).get("name") in carriers for source in container.get("envFrom") or []):
            return True
    return False


def _dapr_reads(docs: tuple[dict, ...], metadata: dict, root_paths: frozenset[str]) -> bool:
    """Whether the pod's sidecar may fetch a secret of the Vault store that holds the root."""
    annotations = metadata.get("annotations") or {}
    if annotations.get("dapr.io/enabled") != "true":
        return False
    app, config = annotations.get("dapr.io/app-id"), annotations.get("dapr.io/config")
    for store in (doc for doc in docs if doc.get("kind") == "Component" and doc["spec"].get("type") == "secretstores.hashicorp.vault"):
        if store.get("scopes") and app not in store["scopes"]:
            continue
        secrets = next((doc["spec"].get("secrets") or {} for doc in docs if doc.get("kind") == "Configuration" and doc["metadata"]["name"] == config), {})
        rule = next((scope for scope in secrets.get("scopes") or [] if scope["storeName"] == store["metadata"]["name"]), {})
        allowed, denied = set(rule.get("allowedSecrets") or []), set(rule.get("deniedSecrets") or [])
        if any(path in allowed or (rule.get("defaultAccess", "allow") == "allow" and path not in denied) for path in root_paths):
            return True
    return False


def _names_the_root_id(container: dict, root_id: str) -> bool:
    """A literal root access key in the command line or env, or an access key taken from the fleet's `-app` Secret,
    which carries the root's (`chart/templates/secrets.yaml`)."""
    literals = [*(container.get("command") or []), *(container.get("args") or []), *(entry.get("value") for entry in container.get("env") or [])]
    from_app = (((entry.get("valueFrom") or {}).get("secretKeyRef") or {}) for entry in container.get("env") or [])
    return any(root_id in str(literal) for literal in literals) or any(
        ref.get("name", "").endswith("-app") and ref.get("key") == "AWS_ACCESS_KEY_ID" for ref in from_app
    )


def test_the_root_secret_and_its_identity_reach_only_the_store_its_seeder_and_the_provisioning_hook() -> None:
    chart = render(*DEV_ESTATE)
    estate = (*with_dev_store(chart), *render_fixture("rustfs"))
    seeded = _seeded("\n".join(seed_scripts(chart).values()))
    root_fields = frozenset((path, prop) for path, properties in seeded.items() for prop, value in properties.items() if _ROOT_VALUE.search(value))
    root_paths = frozenset(path for path, _ in root_fields)
    root_id = next(properties[ROOT_ID] for properties in seeded.values() if ROOT_ID in properties)
    carriers = _carriers(chart, root_fields)

    readers = {workload for workload, metadata, pod in _pods(estate) if _reads_a_carrier(pod, carriers) or _dapr_reads(chart, metadata, root_paths)}
    presenters = {f"{workload}/{name}" for workload, name, container in containers(estate) if _names_the_root_id(container, root_id)}

    assert root_fields == {("object-store-root", ROOT_SECRET)}, f"the dev store holds the root secret at {sorted(root_fields)}"
    assert readers == {"Deployment/rask-openbao", "StatefulSet/rask-rustfs", "Job/rask-object-store-scoped-users-r1"}, sorted(readers)
    assert presenters == {"Deployment/rask-openbao/seed", "Job/rask-object-store-scoped-users-r1/provision"}, sorted(presenters)
