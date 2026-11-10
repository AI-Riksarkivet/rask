"""Each Dapr app reads only its own secrets: its own sidecar refuses another app's, and so does the store.

[[XC-165]] (#659), read back on the deployed estate. The render holds the same claim offline
(`tests/unit/test_every_secret_in_the_dev_store_has_exactly_its_readers.py`); this asks the running layers:

- the Dapr scope: from inside each app's pod, its own sidecar serves the app a secret it owns and answers 403 for a secret
  another app owns, or one only its components may resolve;
- the store: a token the store issues to one app's ServiceAccount, through the Kubernetes auth role the `lance-secrets`
  component logs that app in as, reads the app's own secret and is refused another app's; the same account is refused
  another app's role; and `root`, the token a store in dev mode publishes, reads nothing.

The store is reached from its own seed container, which runs `bao`. A ServiceAccount token, a store token and every secret
value stay in this process or on a command's stdin, never in an argument list or an assertion message.
"""

from __future__ import annotations

import json
import subprocess
from typing import Final

import chaos
import pytest


pytestmark = pytest.mark.e2e

#: app -> (a secret its own sidecar serves its code, a field of it, a secret it may not read through its sidecar). No app
#: holds a store pair (ADR 0170): a signer's own read is its signing key, and lineage, which signs nothing, reads the key
#: it verifies the catalog by and is refused the catalog's private one.
SIDECAR_READS: Final[dict[str, tuple[str, str, str]]] = {
    "catalog": ("signing-key-rask-sa-catalog", "seed", "signing-key-rask-sa-maintenance"),
    "lineage": ("signing-public-rask-sa-catalog", "keys", "signing-key-rask-sa-catalog"),
    "maintenance": ("signing-key-rask-sa-maintenance", "seed", "signing-key-rask-sa-catalog"),
}

#: The apps that sign, each the only reader of its own signing key in the store.
SIGNERS: Final = ("catalog", "maintenance")

#: Asks the pod's own sidecar for each named secret and prints, per secret, the status and the FIELD NAMES it answered.
_ASK_THE_SIDECAR: Final = """\
import json, os, sys, urllib.error, urllib.request
port = os.environ.get("DAPR_HTTP_PORT", "3500")
answers = {}
for name in sys.argv[1:]:
    try:
        body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/v1.0/secrets/lance-secrets/{name}", timeout=10).read())
        answers[name] = [200, sorted(body)]
    except urllib.error.HTTPError as refused:
        answers[name] = [refused.code, []]
print(json.dumps(answers))
"""


def _run(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    """`kubectl args` in the release's namespace, with ``stdin``; its outcome, which a caller judges without echoing stdin."""
    command = ["kubectl", *(("--namespace", chaos.KUBE_NAMESPACE) if chaos.KUBE_NAMESPACE else ()), *args]
    return subprocess.run(command, input=stdin, capture_output=True, text=True, check=False, timeout=chaos.SETTLE_SECONDS)  # noqa: S603 — a fixed tool, argv built here


def _sidecars() -> dict[str, dict[str, str]]:
    """app -> each Deployment that runs its sidecar and that Deployment's own container, for each app the `lance-secrets`
    component is scoped to that the release runs a pod of. One app may run in several Deployments (maintenance's web pods
    and its worker pods share one app id), and every one of them is asked. A Deployment that asks for a pod and has none
    running stays, and fails the ask."""
    scopes = set(json.loads(chaos.kubectl("get", "components.dapr.io", "lance-secrets", "-o", "json")).get("scopes") or [])
    running: dict[str, dict[str, str]] = {}
    for deployment in json.loads(chaos.kubectl("get", "deployments", "-o", "json"))["items"]:
        template = deployment["spec"]["template"]
        app = ((template.get("metadata") or {}).get("annotations") or {}).get("dapr.io/app-id")
        if isinstance(app, str) and app in scopes and deployment["spec"].get("replicas", 1):
            running.setdefault(app, {})[deployment["metadata"]["name"]] = template["spec"]["containers"][0]["name"]
    return running


def _store_component() -> dict[str, str]:
    """The `lance-secrets` component's metadata, as the cluster serves it."""
    component = json.loads(chaos.kubectl("get", "components.dapr.io", "lance-secrets", "-o", "json"))
    return {row["name"]: str(row.get("value", "")) for row in component["spec"]["metadata"]}


def _store_pod() -> str:
    pods = chaos.Workload(kind="deployment", name=chaos.SECRET_STORE).pods()
    assert len(pods) == 1, f"the dev store runs {len(pods)} pods"
    return pods[0].removeprefix("pod/")


def _login(pod: str, mount: str, role: str, account: str, audience: str) -> tuple[int, str]:
    """Log `account` in to `role` with a token the API server issues it for `audience`: the exit code and the store token."""
    jwt = chaos.kubectl("create", "token", account, "--audience", audience, "--duration", "10m")
    done = _run("exec", "-i", pod, "-c", "seed", "--", "bao", "write", "-format=json", f"auth/{mount}/login", f"role={role}", "jwt=-", stdin=jwt)
    token = json.loads(done.stdout)["auth"]["client_token"] if done.returncode == 0 else ""
    return done.returncode, token


def _read_as(pod: str, token: str, secret: str) -> tuple[int, str]:
    """Read `secret` from the store as `token`: the exit code and bao's error, which names no value."""
    done = _run(
        "exec", "-i", pod, "-c", "seed", "--", "sh", "-c", 'IFS= read -r t; BAO_TOKEN="$t" exec bao kv get -format=json "secret/$1"', "sh", secret, stdin=token
    )
    return done.returncode, done.stderr.strip()[-200:]


def test_each_sidecar_serves_its_app_only_its_own_secrets() -> None:
    sidecars = _sidecars()
    assert sidecars, "the release runs no app the store is scoped to"
    assert sidecars.keys() <= SIDECAR_READS.keys(), f"the store is scoped to {sorted(sidecars.keys() - SIDECAR_READS.keys())}, which this test does not ask"
    found: dict[str, object] = {}
    for app, deployment, container in sorted((app, name, container) for app, held in sidecars.items() for name, container in held.items()):
        own, field, foreign = SIDECAR_READS[app]
        done = _run("exec", f"deployment/{deployment}", "-c", container, "--", "python", "-c", _ASK_THE_SIDECAR, own, foreign)
        assert done.returncode == 0, f"{deployment}'s pod could not ask its sidecar: {done.stderr.strip()[-300:]}"
        answers = json.loads(done.stdout)
        status, fields = answers[own]
        if status != 200 or field not in fields:
            found[f"{deployment} reading its app's own {own}"] = answers[own]
        if answers[foreign][0] != 403:
            found[f"{deployment} reading {foreign}"] = answers[foreign][0]
    assert not found, f"a sidecar served a secret its app does not own, or refused one it does: {found}"


def test_the_store_refuses_each_app_s_token_another_app_s_secret_and_role() -> None:
    meta = _store_component()
    assert meta.get("vaultAuthMethod") == "kubernetes", f"lance-secrets logs in by {meta.get('vaultAuthMethod')!r}, not as each app's own account"
    mount, role = meta["vaultKubernetesMountPath"], meta["vaultKubernetesRole"]
    pod = _store_pod()
    specs = {app: json.loads(chaos.kubectl("get", f"deployment/{chaos.RELEASE}-{app}", "-o", "json"))["spec"]["template"]["spec"] for app in SIGNERS}
    keys = {app: f"signing-key-{spec['serviceAccountName']}" for app, spec in specs.items()}
    found: dict[str, object] = {}
    for app, foreign in (SIGNERS, SIGNERS[::-1]):
        account = specs[app]["serviceAccountName"]
        [audience] = [
            source["serviceAccountToken"]["audience"]
            for volume in specs[app]["volumes"]
            if volume["name"] == "openbao-identity"
            for source in volume["projected"]["sources"]
        ]
        code, token = _login(pod, mount, role.replace("{appID}", app), account, audience)
        if code != 0:
            found[f"{app} logging in as itself"] = code
            continue
        if (own := _read_as(pod, token, keys[app]))[0] != 0:
            found[f"{app} reading {keys[app]}"] = own
        if (other := _read_as(pod, token, keys[foreign]))[0] == 0 or "permission denied" not in other[1]:
            found[f"{app} reading {keys[foreign]}"] = other
        if _login(pod, mount, role.replace("{appID}", foreign), account, audience)[0] == 0:
            found[f"{app}'s account logging in as {foreign}"] = "admitted"
    published = _read_as(pod, "root", keys["catalog"])
    if published[0] == 0 or "permission denied" not in published[1]:
        found["the once-published root token"] = published
    assert not found, f"the store admitted a token to a secret or a role it was not granted: {found}"
