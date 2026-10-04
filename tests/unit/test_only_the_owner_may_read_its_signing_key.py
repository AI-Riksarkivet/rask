"""Each app's sidecar may read only the private signing keys its own Deployments sign with, no verifier reads one, and no app reads a bus credential.

[[LH-064]] C6, as the owner ruled it on 2026-10-02 (the Dapr-scope claim): a signing identity's private key is its
own secret, `signing-key-<identity>`, in the store every sidecar reads, and every public key list,
`signing-public-<identity>`, is readable by all. Dapr scopes secrets by NAME, and each app's Configuration is
default-allow with a deny list, so a private key no deny list covers is readable by every sidecar'd app: the deny
lists are what this reads. Lineage verifies every signer's events and signs none, so it reads no private key at all.

[[XC-078]] puts the NATS credentials in the same store (values.yaml `nats.auth`): a `nats-user-<user>` per user of the
permission table, the dev mint's trust root `nats-root`, and the cluster's route credential `nats-route`. A sidecar loads its
own pub/sub Component's `nats-user-<app>` through the Component's secretKeyRef, which Dapr resolves without consulting the
Configuration's scopes, so every app's deny list hides every one of them from the app process. Ingest alone reads its own
user through the secret API, because its raw NATS client has no Component.

The residual the ruling names, and this gate cannot see: a pod that holds OpenBao's root token, or reaches :8200
directly, reads the store without asking a sidecar (XC-079).

Which identity a Deployment signs as is read from the `RASK_SIGNING_IDENTITY` the chart renders on it, never from a
helper's restatement, so an app that signs as one identity and may read another fails here. The Configuration a sidecar
loads is the one its pod's `dapr.io/config` names, never a name this gate builds: Configurations are named by the hash
of their spec, and a pod that names one the render does not contain has a daprd that never boots.
"""

from __future__ import annotations

from collections import defaultdict

from tests.unit import chart_render
from tests.unit.chart_render import DEFAULT_ARGS, env_of, render


STORE = "lance-secrets"
#: The one app whose client reads its own NATS user through the secret API rather than through a Component.
_READS_ITS_OWN_BUS_USER = "ingest"


def _readable(scope: dict, names: set[str]) -> set[str]:
    readable = names - set(scope.get("deniedSecrets") or [])
    if (scope.get("defaultAccess") or "allow").lower() == "deny":
        readable &= set(scope.get("allowedSecrets") or [])
    return readable


def test_each_sidecar_may_read_only_its_own_signing_key_and_no_bus_credential_but_ingest_its_own_user() -> None:
    docs = render(*DEFAULT_ARGS, "--set", "explorer.enabled=true")
    configurations = {doc["metadata"]["name"]: doc for doc in docs if doc.get("kind") == "Configuration"}
    signs_as: dict[str, set[str]] = defaultdict(set)
    loads: dict[str, set[str]] = defaultdict(set)
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        template = doc["spec"]["template"]
        annotations = (template.get("metadata") or {}).get("annotations") or {}
        app_id = annotations.get("dapr.io/app-id") or doc["metadata"]["name"]
        if name := annotations.get("dapr.io/config"):
            loads[app_id].add(name)
        for container in template["spec"]["containers"]:
            if identity := env_of(container).get("RASK_SIGNING_IDENTITY"):
                signs_as[app_id].add(identity)
    signers = set().union(*signs_as.values()) if signs_as else set()
    assert signers, "no Deployment renders RASK_SIGNING_IDENTITY: nothing in this render signs, so the gate measures nothing"
    private = {f"signing-key-{identity}" for identity in signers}
    public = {f"signing-public-{identity}" for identity in signers}
    users = chart_render.nats_users(docs)
    assert users, "the dev seed issues no NATS user, so the bus credentials this gate hides are not in the store to hide"
    bus = {f"nats-user-{user}" for user in users} | {"nats-root", "nats-route"}

    problems: dict[str, object] = {}
    checked: set[str] = set()
    for app_id, names in loads.items():
        for name in names:
            if name not in configurations:
                problems[app_id] = f"its sidecar loads Configuration {name}, which the render does not contain"
                continue
            for scope in ((configurations[name].get("spec") or {}).get("secrets") or {}).get("scopes") or []:
                if scope.get("storeName") != STORE:
                    continue
                checked.add(app_id)
                own = {f"signing-key-{identity}" for identity in signs_as.get(app_id, set())}
                if (readable := _readable(scope, private)) != own:
                    problems[app_id] = {"may read": sorted(readable), "signs with": sorted(own)}
                if hidden := public - _readable(scope, public):
                    problems[f"{app_id} (public lists)"] = sorted(hidden)
                own_bus = {f"nats-user-{app_id}"} if app_id == _READS_ITS_OWN_BUS_USER else set()
                if (readable := _readable(scope, bus)) != own_bus:
                    problems[f"{app_id} (bus credentials)"] = {"may read": sorted(readable), "reads through the secret API": sorted(own_bus)}

    assert {"lineage", _READS_ITS_OWN_BUS_USER} <= checked and set(signs_as) <= checked, (
        f"apps whose sidecar no Configuration scopes: {sorted(({'lineage', _READS_ITS_OWN_BUS_USER} | set(signs_as)) - checked)}"
    )
    assert not problems, f"a sidecar may read a private signing key it does not sign with, a bus credential, or not read a public list: {problems}"
