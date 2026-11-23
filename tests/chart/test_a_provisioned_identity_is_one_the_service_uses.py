"""Every lakehouse workload that reaches the object store presents the store identity the chart provisions for its account.

[[XC-083]], [[XC-084]], ADR 0170. No workload holds a store key: each pod projects a ServiceAccount token for the audience
the store trusts, the store grants a verified token exactly the canned policy its `sub` names, and the provisioning hook
writes each account's policy from `<release>-object-store-identities`. Each part alone is a distinct failure: a policy
written for another account leaves the workload's token mapping to nothing, so every exchange is refused; an audience the
store does not trust is refused at verification; a key pair in the environment is a second identity no rotation reaches;
and a role policy grants its policy to every ServiceAccount token in the cluster.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.chart.chart_render import containers, env_of, render, render_fixture


REPO = Path(__file__).resolve().parents[2]

#: `{workload: (the container that signs with the store, its settings' variable prefix)}`: every lakehouse workload that
#: reaches the object store, with both backups on as the development estate runs them.
STORE_CLIENTS = {
    "Deployment/rask-catalog": ("catalog", "LANCE"),
    "Deployment/rask-lineage": ("lineage", "LINEAGE"),
    "Deployment/rask-maintenance-worker": ("worker", "MAINTENANCE"),
    "CronJob/rask-pg-backup": ("upload", "RASK"),
    "CronJob/rask-control-root-backup": ("backup", "RASK"),
}


def _estate() -> tuple[dict, ...]:
    return render("--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml"))


def _pod(docs: tuple[dict, ...], workload: str) -> dict:
    kind, name = workload.split("/", 1)
    [doc] = [doc for doc in docs if doc.get("kind") == kind and doc["metadata"]["name"] == name]
    return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"] if kind == "CronJob" else doc["spec"]["template"]["spec"]


def _store_server() -> dict[str, str]:
    [server] = [env_of(container) for _, name, container in containers(render_fixture("rustfs")) if name == "rustfs"]
    return server


def test_the_store_grants_a_token_only_the_policy_its_subject_names() -> None:
    """The dev store's provider grants a verified token the canned policy its `sub` names and no role policy, verified with
    the keys rask's issuer mirror serves for the cluster's issuer (measured 2026-10-18 on RustFS 1.0.3,
    `scripts/rustfs_web_identity_measure.py`)."""
    chart = _estate()
    server = _store_server()
    [mirror] = [doc for doc in chart if doc.get("kind") == "Service" and doc["metadata"]["name"] == "rask-issuer-mirror"]
    origin = f"http://{mirror['metadata']['name']}:{mirror['spec']['ports'][0]['port']}"
    [catalog] = [env_of(container) for workload, name, container in containers(chart) if workload == "Deployment/rask-catalog" and name == "catalog"]

    assert (server["RUSTFS_IDENTITY_OPENID_CLAIM_NAME"], "RUSTFS_IDENTITY_OPENID_ROLE_POLICY" in server) == ("sub", False)
    assert (server["RUSTFS_IDENTITY_OPENID_CONFIG_URL"], server["RUSTFS_OUTBOUND_ALLOW_ORIGINS"]) == (f"{origin}/.well-known/openid-configuration", origin)
    assert server["RUSTFS_IDENTITY_OPENID_ISSUER"] == catalog["RASK_SA_ISSUER"]


@pytest.mark.parametrize("workload", sorted(STORE_CLIENTS))
def test_each_workload_presents_the_store_identity_the_chart_provisions_for_its_account(workload: str) -> None:
    chart = _estate()
    container_name, prefix = STORE_CLIENTS[workload]
    pod = _pod(chart, workload)
    [container] = [container for container in pod["containers"] if container["name"] == container_name]
    env = env_of(container)
    token_dir = str(Path(env[f"{prefix}_S3_WEB_IDENTITY_TOKEN_FILE"]).parent)
    [mounted] = [mount["name"] for mount in container["volumeMounts"] if mount["mountPath"] == token_dir]
    [audience] = [
        source["serviceAccountToken"]["audience"]
        for volume in pod["volumes"]
        if volume["name"] == mounted
        for source in volume["projected"]["sources"]
        if "serviceAccountToken" in source
    ]
    account = pod["serviceAccountName"]
    [hook] = [
        container for workload_, name, container in containers(chart) if workload_.startswith("Job/rask-object-store-provisioning-r") and name == "provision"
    ]
    [identities] = [doc["data"] for doc in chart if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "rask-object-store-identities"]
    held = sorted(name for name in env if name.endswith(("_ACCESS_KEY_ID", "_SECRET_ACCESS_KEY")))

    written = {arg.split("=", 1)[0]: json.loads(arg.split("=", 1)[1]) for flag, arg in zip(hook["args"], hook["args"][1:], strict=False) if flag == "--policy"}
    declared = json.loads(identities[f"{account}.json"])

    assert (audience, held) == (_store_server()["RUSTFS_IDENTITY_OPENID_CLIENT_ID"], [])
    assert (written.get(f"system:serviceaccount:default:{account}"), declared["Version"]) == (declared, "2012-10-17")


#: The lakehouse services whose doors verify a person's token. Maintenance has no human door: its worker serves only its
#: probes.
_LAKEHOUSE_DOORS = ("rask-catalog", "rask-lineage")


def test_the_lakehouse_doors_check_the_audience_the_idp_issues_to_the_clients_it_trusts(tmp_path: Path) -> None:
    """[[LH-515]] The lakehouse checks an audience of its own, which the IdP issues only to the clients it trusts.

    Measured on Dex v2.42.0: a client that asks for `audience:server:client_id:<audience>` gets an id token and an access
    token whose `aud` is `[<audience>, <client>]`, and only when the audience client lists it in `trustedPeers`; any
    other client is refused 400 for the scope. A public audience client would let anyone sign in as it (200, `aud` the
    audience alone) and skip the trust. So the doors' audience is the dev Dex's audience client, distinct from its
    sign-in client, confidential and trusting exactly the clients the fixture declares: the sign-in client, each machine
    client, which signs in for itself with the client-credentials grant (ADR 0181), and each client release's own sign-in
    client, which a pinned lakehouse registers for that client's e2e ([[XC-189]]). A browser app's client is public and
    holds no secret, so its code is bound to an S256 challenge Dex enforces for every client; the fixture's own sign-in
    client, which the live lanes sign in as with the password grant, is confidential and registers no redirect, so no
    browser signs in through it (owner decision D2, #769).
    """
    chart = render("--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml"))
    client = {"id": "a-client", "secret": "a-client-secret", "redirectURIs": ["http://localhost:9999/auth/callback"]}
    browser = {"id": "a-browser-app", "public": True, "redirectURIs": ["http://localhost:9999/auth/callback"]}
    # Each client comes from a values file of its own, as two repositories would supply them: a client is an entry keyed
    # by its id, so the two files merge, and each client's own origins join the ones Dex answers CORS for ([[XC-221]]).
    one, other = tmp_path / "one.yaml", tmp_path / "other.yaml"
    one.write_text(yaml.safe_dump({"clients": {"a-client": {"secret": client["secret"], "redirectURIs": client["redirectURIs"]}}}))
    other.write_text(
        yaml.safe_dump(
            {
                "clients": {
                    "a-browser-app": {
                        "public": True,
                        "redirectURIs": browser["redirectURIs"],
                        "allowedOrigins": ["http://localhost:9999"],
                        "postLogoutRedirectURIs": ["http://localhost:9999/"],
                    }
                }
            }
        )
    )
    fixture = render_fixture("dex", "-f", str(one), "-f", str(other))
    [config] = [yaml.safe_load(doc["data"]["config.yaml"]) for doc in fixture if doc.get("kind") == "ConfigMap"]
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
    assert clients["a-client"] == {"name": "a-client", **client}
    # A client that names where its sign-out may land has it registered; one that names none registers none.
    assert clients["a-browser-app"] == {"name": "a-browser-app", **browser, "postLogoutRedirectURIs": ["http://localhost:9999/"]}
    assert config["web"]["allowedOrigins"] == ["http://localhost:5173", "http://localhost:9999"]
    assert (sorted(clients["rask-web"]), config["oauth2"]["pkce"]) == (["id", "name", "secret"], {"enforce": True, "codeChallengeMethodsSupported": ["S256"]})


@pytest.mark.parametrize(
    ("browser", "refusal"),
    [
        pytest.param(
            {"a-browser-app": {"public": True, "secret": "leaked", "redirectURIs": ["http://localhost:9999/auth/callback"]}},
            'clients "a-browser-app" is public and names a secret: a public client holds none',
            id="a-secret",
        ),
        pytest.param(
            {"a-browser-app": {"public": True}},
            'clients "a-browser-app" is public and names no redirectURIs: Dex then accepts any loopback, out-of-band or device redirect',
            id="no-redirect",
        ),
        pytest.param(
            [{"id": "a-browser-app", "public": True, "redirectURIs": ["http://localhost:9999/auth/callback"]}],
            "clients is a map of entries keyed by client id",
            id="a-list-which-would-replace-every-other-files-clients",
        ),
    ],
)
def test_a_dex_client_declared_wrongly_is_refused_at_the_render(browser: object, refusal: str) -> None:
    """A public client holds no secret, and names each redirect it may receive a code at: one that names a secret is a
    confidential client mis-declared, and one with no redirect is a client Dex hands a code to at any loopback,
    out-of-band or device callback (v2.46.1 `server/authflow/request.go:96-113`), so either is refused at the render."""
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_fixture("dex", "--set-json", f"clients={json.dumps(browser)}")
    assert refusal in refused.value.stderr
