"""A scoped storage identity the chart PROVISIONS must be the one its service actually presents.

`object-store-scoped-users.yaml` creates a least-privilege RustFS user and policy for the maintenance
and lineage planes on every install and upgrade. Both were OFF by default —
`objectStore.<plane>AccessKey: ""` falls back to the tenant ROOT credential — so the chart builds the
identity, provisions it, and then hands the service the root key id anyway.

MEASURED ON THE RUNNING ESTATE 2026-09-08, reading each pod's real environment rather than the
templates: catalog, lineage, ingest and viewer all present the root key id. The operator had armed
maintenance and medallion by hand in a values file; lineage — whose policy is the TIGHTEST of the
three, needing no PutObject at all — was still root because nobody knew to name it.

This is the shape `rask.isRealDeployment` was written to end, in its own words: "BOTH SIGNALS ARE
FACTS THE DEPLOY ALREADY DEPENDS ON, never a flag someone must remember to arm. A guard reachable
only by remembering an opt-in protects the installs that did not need protecting." A provisioned
identity nobody selected is that guard.

The ordering is the reason it shipped off, and it was a real one: the provisioning hook ran
`post-upgrade`, so a chart that defaulted the key ON would roll the pods onto a credential that did
not exist yet. That is fixed by the hook running before the roll, not by leaving every install on the
root key.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from chart_yaml import FAST_LOADER

from tests.unit.chart_render import PREREQ_ARGS, RAY_ARGS, containers, env_of, render, render_fixture


REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"

#: `{env var naming the store field the identity's key id is read from: the workload that presents it}` — one
#: row per plane the chart provisions a scoped RustFS user for in `object-store-scoped-users.yaml`. The pair is the
#: store's (ADR 0166), so the identity a plane presents is the one behind the field it is told to read.
PROVISIONED_PLANES = {
    "MAINTENANCE_DAPR_SECRET_S3_ID_FIELD": "maintenance",
    "LINEAGE_DAPR_SECRET_S3_ID_FIELD": "lineage",
}

#: The store field holding the tenant ROOT account's key id, seeded from `objectStore.accessKey`.
ROOT_ID_FIELD = "object-store-access-key"


def _render(*extra: str) -> str:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [
        helm,
        "template",
        "rask",
        str(CHART),
        "--set",
        "image.localImages=true",
        *PREREQ_ARGS,
        *RAY_ARGS,
        "--set-string",
        "auth.oidc.issuer=http://localhost:8080/dex",
        *extra,
    ]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout


def _rendered_identities(rendered: str) -> dict[str, set[str]]:
    """`{env var: every value any workload renders for it}`."""
    found: dict[str, set[str]] = {k: set() for k in PROVISIONED_PLANES}
    for doc in yaml.load_all(rendered, Loader=FAST_LOADER):
        if not doc or doc.get("kind") not in {"Deployment", "StatefulSet"}:
            continue
        for container in doc["spec"]["template"]["spec"].get("containers", []):
            for env in container.get("env", []):
                if env["name"] in found and "value" in env:
                    found[env["name"]].add(env["value"])
    return found


def test_no_provisioned_plane_falls_back_to_the_storage_root() -> None:
    """The default install must not hand a service the tenant root key it has an identity for."""
    identities = _rendered_identities(_render())
    for var, plane in PROVISIONED_PLANES.items():
        rendered = identities[var]
        assert rendered, f"{var} renders nowhere — this pin is asserting nothing about {plane}"
        assert ROOT_ID_FIELD not in rendered, (
            f"{plane} reads the tenant ROOT credential's key id ({ROOT_ID_FIELD!r}) on a default install, while "
            f"`object-store-scoped-users.yaml` provisions a least-privilege user for it in the same release. "
            "Root by default is not a deployment choice anyone made."
        )


def test_the_catalog_presents_the_store_identity_the_chart_provisions_for_its_account() -> None:
    """[[XC-083]] The catalog holds no store key: the store grants its token the policy the hook names after its account.

    The catalog's pod projects a token for the audience the dev store trusts as its identity provider's client, and the
    store grants a token exactly the canned policy its `sub` names, with no role policy (ADR 0170; measured on RustFS
    1.0.3, `docs/audits/2026-10-18/rustfs_web_identity_measure.py`). Each part alone is a distinct failure: a policy named
    for another account leaves the catalog's token mapping to nothing, so every exchange is refused; an audience the
    store does not trust is refused at verification; a role policy grants the catalog's data plane to every
    ServiceAccount token in the cluster; and a discovery URL that is not rask's issuer mirror leaves the store unable to
    fetch the keys it verifies with.
    """
    chart = render("--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml"))
    [server] = [env_of(container) for _, name, container in containers(render_fixture("rustfs")) if name == "rustfs"]
    [(pod, catalog)] = [
        (doc["spec"]["template"]["spec"], container)
        for doc in chart
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "rask-catalog"
        for container in doc["spec"]["template"]["spec"]["containers"]
        if container["name"] == "catalog"
    ]
    env = env_of(catalog)
    token_dir = str(Path(env["LANCE_S3_WEB_IDENTITY_TOKEN_FILE"]).parent)
    [mounted] = [mount["name"] for mount in catalog["volumeMounts"] if mount["mountPath"] == token_dir]
    [audience] = [
        source["serviceAccountToken"]["audience"]
        for volume in pod["volumes"]
        if volume["name"] == mounted
        for source in volume["projected"]["sources"]
        if "serviceAccountToken" in source
    ]
    [hook] = [container for workload, name, container in containers(chart) if workload.startswith("Job/rask-object-store-scoped-users") and name == "provision"]
    [mirror] = [doc for doc in chart if doc.get("kind") == "Service" and doc["metadata"]["name"] == "rask-issuer-mirror"]
    origin = f"http://{mirror['metadata']['name']}:{mirror['spec']['ports'][0]['port']}"

    assert not {"LANCE_S3_ACCESS_KEY_ID", "LANCE_S3_SECRET_ACCESS_KEY", "LANCE_DAPR_SECRET_KEY"} & set(env), sorted(env)
    assert audience == server["RUSTFS_IDENTITY_OPENID_CLIENT_ID"]
    assert (server["RUSTFS_IDENTITY_OPENID_CLAIM_NAME"], "RUSTFS_IDENTITY_OPENID_ROLE_POLICY" in server) == ("sub", False)
    command = " ".join(hook["command"])
    policy = f"system:serviceaccount:default:{pod['serviceAccountName']}"
    assert f'--policy "{policy}=' in command
    assert [user for user, bound in re.findall(r'--user "([^"=]+)=([^"]+)"', command) if bound == policy] == [], "a store user holds the catalog's policy"
    assert (server["RUSTFS_IDENTITY_OPENID_CONFIG_URL"], server["RUSTFS_OUTBOUND_ALLOW_ORIGINS"]) == (f"{origin}/.well-known/openid-configuration", origin)
    assert server["RUSTFS_IDENTITY_OPENID_ISSUER"] == env["RASK_SA_ISSUER"]


#: The lakehouse services whose doors verify a person's token. Maintenance has no human door: its routes take only the
#: Dapr sidecar's app token.
_LAKEHOUSE_DOORS = ("rask-catalog", "rask-lineage", "rask-notifications")


def test_the_lakehouse_doors_check_the_audience_the_idp_issues_to_the_clients_it_trusts() -> None:
    """[[LH-515]] The lakehouse checks an audience of its own, which the IdP issues only to the clients it trusts.

    Measured on Dex v2.42.0: a client that asks for `audience:server:client_id:<audience>` gets an id token and an access
    token whose `aud` is `[<audience>, <client>]`, and only when the audience client lists it in `trustedPeers`; any
    other client is refused 400 for the scope. A public audience client would let anyone sign in as it (200, `aud` the
    audience alone) and skip the trust. So the doors' audience is the dev Dex's audience client, distinct from its
    sign-in client, confidential and trusting exactly the clients the fixture declares: the sign-in client and each machine
    client, which signs in for itself with the client-credentials grant (ADR 0181).
    """
    chart = render("--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml"))
    [config] = [yaml.safe_load(doc["data"]["config.yaml"]) for doc in render_fixture("dex") if doc.get("kind") == "ConfigMap"]
    clients = {client["id"]: client for client in config["staticClients"]}
    doors = {
        doc["metadata"]["name"]: env_of(container).get("RASK_OIDC_AUDIENCE")
        for doc in chart
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] in _LAKEHOUSE_DOORS
        for container in doc["spec"]["template"]["spec"]["containers"][:1]
    }

    [audience] = set(doors.values())
    others = sorted(set(clients) - {audience})
    assert sorted(doors) == sorted(_LAKEHOUSE_DOORS)
    assert (clients[audience].get("public", False), sorted(clients[audience].get("trustedPeers", []))) == (False, others)
