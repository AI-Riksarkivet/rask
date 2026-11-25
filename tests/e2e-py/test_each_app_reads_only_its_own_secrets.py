"""Each app reads only its own secrets, from the store and as itself, and the store refuses another app's.

[[XC-165]] (#659), [[XC-199]] (#787), read back on the deployed estate. The render holds the same claim offline
(`tests/chart/test_every_secret_in_the_dev_store_has_exactly_its_readers.py`); this asks the running layers:

- the app's own login: inside the pod of each Deployment an app runs (the catalog's, lineage's and the maintenance
  worker's), the process's own read (`lakehouse.governed.secrets`, with the store, the role and the projected token its
  pod was given) returns a secret the app owns and nothing of one another app owns;
- the store: a token the store issues to one app's ServiceAccount, through the Kubernetes auth role its Deployment
  names the app's process (`RASK_SECRET_STORE_ROLE`), reads the app's own secret and is refused another app's; the same
  account is refused another app's role; and `root`, the token a store in dev mode publishes, reads nothing.

The store is reached from its own seed container, which runs `bao`. A ServiceAccount token, a store token and every secret
value stay in this process or on a command's stdin, never in an argument list or an assertion message.
"""

import json
import subprocess
from typing import Final

import chaos
import pytest


pytestmark = pytest.mark.e2e

#: app -> (a secret its own login reads, a field of it, a secret it may not read). No app holds a store pair (ADR 0170): a
#: signer's own read is its signing key, and lineage, which signs nothing, reads the key it verifies the catalog by and
#: is refused the catalog's private one.
OWN_READS: Final[frozendict[str, tuple[str, str, str]]] = frozendict(
    {
        "catalog": ("signing-key-rask-sa-catalog", "seed", "signing-key-rask-sa-maintenance"),
        "lineage": ("signing-public-rask-sa-catalog", "keys", "signing-key-rask-sa-catalog"),
        "maintenance": ("signing-key-rask-sa-maintenance", "seed", "signing-key-rask-sa-catalog"),
    }
)

#: The apps that sign, each the only reader of its own signing key in the store.
SIGNERS: Final = ("catalog", "maintenance")

#: app -> the release's Deployment that runs its process. Maintenance's one process is its worker.
DEPLOYMENTS: Final = frozendict(
    {"catalog": f"{chaos.RELEASE}-catalog", "lineage": f"{chaos.RELEASE}-lineage", "maintenance": f"{chaos.RELEASE}-maintenance-worker"}
)

#: Reads each named secret as the pod's own process does, and prints, per secret, the FIELD NAMES the store gave it.
_READ_AS_THE_APP: Final = """\
import json, sys
from lakehouse.governed.secrets import read_secret
print(json.dumps({name: sorted(read_secret(name, retries=1)) for name in sys.argv[1:]}))
"""


def _run(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    """`kubectl args` in the release's namespace, with ``stdin``; its outcome, which a caller judges without echoing stdin."""
    command = ["kubectl", *(("--namespace", chaos.KUBE_NAMESPACE) if chaos.KUBE_NAMESPACE else ()), *args]
    return subprocess.run(command, input=stdin, capture_output=True, text=True, check=False, timeout=chaos.SETTLE_SECONDS)  # noqa: S603 — a fixed tool, argv built here


def _apps() -> dict[str, dict[str, str]]:
    """app -> the Deployment that runs its process and that Deployment's own container, for each app of `DEPLOYMENTS` the
    release runs. A Deployment that asks for a pod and has none running stays, and fails the ask."""
    running: dict[str, dict[str, str]] = {}
    names = {deployment: app for app, deployment in DEPLOYMENTS.items()}
    for deployment in json.loads(chaos.kubectl("get", "deployments", "-o", "json"))["items"]:
        app = names.get(deployment["metadata"]["name"])
        if app is not None and deployment["spec"].get("replicas", 1):
            running[app] = {deployment["metadata"]["name"]: deployment["spec"]["template"]["spec"]["containers"][0]["name"]}
    return running


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


def _asked_in_each_pod(script: str, apps: dict[str, dict[str, str]]) -> dict[str, tuple[tuple[str, str, str], dict[str, list]]]:
    """Deployment -> (its app's own secret, that secret's field, a foreign secret) and what ``script`` printed for the two,
    run in the app's own container of every Deployment ``apps`` names."""
    assert apps, "the release runs none of the apps asked"
    assert apps.keys() <= OWN_READS.keys(), f"the release runs {sorted(apps.keys() - OWN_READS.keys())}, which this test does not ask"
    answers = {}
    for app, deployment, container in sorted((app, name, container) for app, held in apps.items() for name, container in held.items()):
        own, _field, foreign = OWN_READS[app]
        done = _run("exec", f"deployment/{deployment}", "-c", container, "--", "python", "-c", script, own, foreign)
        assert done.returncode == 0, f"{deployment}'s pod could not ask: {done.stderr.strip()[-300:]}"
        answers[deployment] = (OWN_READS[app], json.loads(done.stdout.strip().splitlines()[-1]))
    return answers


def test_each_app_reads_its_own_secret_from_the_store_as_itself_and_none_of_another_s() -> None:
    found: dict[str, object] = {}
    for deployment, ((own, field, foreign), fields) in _asked_in_each_pod(_READ_AS_THE_APP, _apps()).items():
        if field not in fields[own]:
            found[f"{deployment} reading its app's own {own}"] = fields[own]
        if fields[foreign]:
            found[f"{deployment} reading {foreign}"] = fields[foreign]
    assert not found, f"an app's own login was refused a secret it owns, or read one it does not: {found}"


def _told(spec: dict) -> dict[str, str]:
    """What a pod's own container is told of the secret store, by its environment."""
    return {row["name"]: row["value"] for row in spec["containers"][0].get("env") or [] if row["name"].startswith("RASK_SECRET_STORE_") and "value" in row}


def test_the_store_refuses_each_app_s_token_another_app_s_secret_and_role() -> None:
    pod = _store_pod()
    specs = {app: json.loads(chaos.kubectl("get", f"deployment/{DEPLOYMENTS[app]}", "-o", "json"))["spec"]["template"]["spec"] for app in SIGNERS}
    keys = {app: f"signing-key-{spec['serviceAccountName']}" for app, spec in specs.items()}
    roles = {app: _told(spec)["RASK_SECRET_STORE_ROLE"] for app, spec in specs.items()}
    found: dict[str, object] = {}
    for app, foreign in (SIGNERS, SIGNERS[::-1]):
        account, mount = specs[app]["serviceAccountName"], _told(specs[app])["RASK_SECRET_STORE_AUTH_MOUNT"]
        [audience] = [
            source["serviceAccountToken"]["audience"]
            for volume in specs[app]["volumes"]
            if volume["name"] == "openbao-identity"
            for source in volume["projected"]["sources"]
        ]
        code, token = _login(pod, mount, roles[app], account, audience)
        if code != 0:
            found[f"{app} logging in as itself"] = code
            continue
        if (own := _read_as(pod, token, keys[app]))[0] != 0:
            found[f"{app} reading {keys[app]}"] = own
        if (other := _read_as(pod, token, keys[foreign]))[0] == 0 or "permission denied" not in other[1]:
            found[f"{app} reading {keys[foreign]}"] = other
        if _login(pod, mount, roles[foreign], account, audience)[0] == 0:
            found[f"{app}'s account logging in as {foreign}"] = "admitted"
    published = _read_as(pod, "root", keys["catalog"])
    if published[0] == 0 or "permission denied" not in published[1]:
        found["the once-published root token"] = published
    assert not found, f"the store admitted a token to a secret or a role it was not granted: {found}"
