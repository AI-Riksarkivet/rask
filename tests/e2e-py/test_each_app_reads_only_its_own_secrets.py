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

#: app -> (a secret its own sidecar serves its code, a field of it, a secret it may not read through its sidecar). flows reads
#: nothing itself: the state store's DSN is resolved by its component, so its own read of `dapr-state` is refused.
SIDECAR_READS: Final[dict[str, tuple[str | None, str | None, str]]] = {
    "catalog": ("catalog-s3", "access_key", "lineage-s3"),
    "lineage": ("lineage-s3", "access_key", "catalog-s3"),
    "maintenance": ("maintenance-s3", "access_key", "catalog-s3"),
    "notifications": ("dapr-app-token", "token", "catalog-s3"),
    "annotator": ("publisher-oidc", "password", "catalog-s3"),
    "viewer": ("viewer-s3", "access_key", "media-s3"),
    "search": ("media-s3", "access_key", "viewer-s3"),
    "flows": (None, None, "dapr-state"),
}

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


def _sidecars() -> dict[str, str]:
    """app -> the Deployment that runs its sidecar, for each app the `lance-secrets` component is scoped to that the release
    runs: a release that renders no explorer runs no annotator, search or viewer."""
    scopes = set(json.loads(chaos.kubectl("get", "components.dapr.io", "lance-secrets", "-o", "json")).get("scopes") or [])
    running: dict[str, str] = {}
    for deployment in json.loads(chaos.kubectl("get", "deployments", "-o", "json"))["items"]:
        app = ((deployment["spec"]["template"].get("metadata") or {}).get("annotations") or {}).get("dapr.io/app-id")
        if isinstance(app, str) and app in scopes:
            running[app] = deployment["metadata"]["name"]
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
    for app, deployment in sorted(sidecars.items()):
        own, field, foreign = SIDECAR_READS[app]
        names = [name for name in (own, foreign) if name]
        done = _run("exec", f"deployment/{deployment}", "-c", app, "--", "python", "-c", _ASK_THE_SIDECAR, *names)
        assert done.returncode == 0, f"{app}'s pod could not ask its sidecar: {done.stderr.strip()[-300:]}"
        answers = json.loads(done.stdout)
        if own is not None:
            status, fields = answers[own]
            if status != 200 or field not in fields:
                found[f"{app} reading its own {own}"] = answers[own]
        if answers[foreign][0] != 403:
            found[f"{app} reading {foreign}"] = answers[foreign][0]
    assert not found, f"a sidecar served a secret its app does not own, or refused one it does: {found}"


def test_the_store_refuses_each_app_s_token_another_app_s_secret_and_role() -> None:
    meta = _store_component()
    assert meta.get("vaultAuthMethod") == "kubernetes", f"lance-secrets logs in by {meta.get('vaultAuthMethod')!r}, not as each app's own account"
    mount, role = meta["vaultKubernetesMountPath"], meta["vaultKubernetesRole"]
    pod = _store_pod()
    found: dict[str, object] = {}
    for app, foreign in (("catalog", "lineage"), ("lineage", "catalog")):
        deployment = json.loads(chaos.kubectl("get", f"deployment/{chaos.RELEASE}-{app}", "-o", "json"))["spec"]["template"]["spec"]
        account = deployment["serviceAccountName"]
        [audience] = [
            source["serviceAccountToken"]["audience"]
            for volume in deployment["volumes"]
            if volume["name"] == "openbao-identity"
            for source in volume["projected"]["sources"]
        ]
        code, token = _login(pod, mount, role.replace("{appID}", app), account, audience)
        if code != 0:
            found[f"{app} logging in as itself"] = code
            continue
        if (own := _read_as(pod, token, f"{app}-s3"))[0] != 0:
            found[f"{app} reading {app}-s3"] = own
        if (other := _read_as(pod, token, f"{foreign}-s3"))[0] == 0 or "permission denied" not in other[1]:
            found[f"{app} reading {foreign}-s3"] = other
        if _login(pod, mount, role.replace("{appID}", foreign), account, audience)[0] == 0:
            found[f"{app}'s account logging in as {foreign}"] = "admitted"
    published = _read_as(pod, "root", "catalog-s3")
    if published[0] == 0 or "permission denied" not in published[1]:
        found["the once-published root token"] = published
    assert not found, f"the store admitted a token to a secret or a role it was not granted: {found}"
