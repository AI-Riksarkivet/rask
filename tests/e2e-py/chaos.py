"""Taking one dependency of the release down and bringing it back, for criterion 5's drills (XC-095).

Every action is a `kubectl` call on the cluster the run's KUBECONFIG names, against the release `LANCE_E2E_RELEASE` names
in the namespace `LANCE_E2E_RELEASE_NAMESPACE` names (the current context's when unset). A drill runs only when its run
asks for it (`--chaos`, `tests/e2e-py/conftest.py`), because it takes down what the release it runs against depends on.

AN OUTAGE BEGINS WHEN THE LAST POD IS GONE, not when the scale is accepted: a terminating pod still answers, so a probe
sent before it is gone measures the old pod, not the outage (`docs/agents/lessons.md`). And an outage ENDS in a
`finally`, whatever the drill found: a drill that fails mid-way still hands the release back with what it scaled down
running again.

EACH DEPENDENCY IS TAKEN DOWN THE WAY ITS OUTAGE LOOKS, without destroying what it holds. A store with a volume (the
object store, the bus) and a stateless server (OpenFGA, lineage) are scaled to zero. The dev issuer and the dev secret
store keep their state in memory, so a new pod would be a new signing key or an empty store: the issuer's Service is cut
from its pod instead, and the secret store is sealed in place, the outage a Vault-shaped store really has. The catalog
is killed with SIGKILL from inside its container, which reaches uvicorn because the image's tini, not uvicorn, is the
container's PID 1 (LH-313).

The names are the dev fixtures' (`deploy/fixtures`): in production these dependencies are the platform's, and no drill
runs there.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from typing import Literal

import bus
import requests
from pydantic import BaseModel

from catalog.core.config import CatalogReadBounds


RELEASE = os.environ.get("LANCE_E2E_RELEASE", "rask")
#: The Kubernetes namespace the release runs in; empty means the current context's.
KUBE_NAMESPACE = os.environ.get("LANCE_E2E_RELEASE_NAMESPACE", "")
#: How long a pod may take to go, or a workload to roll back out, before the drill gives up.
SETTLE_SECONDS = float(os.environ.get("LANCE_E2E_CHAOS_SETTLE_SECONDS", "300"))

#: The release's own workloads.
CATALOG = f"{RELEASE}-catalog"
LINEAGE = f"{RELEASE}-lineage"
OPENFGA = f"{RELEASE}-openfga"
NOTIFICATIONS = f"{RELEASE}-notifications"
#: The release's workloads whose sidecar holds a connection to the bus that the drills' readings depend on: the catalog
#: announces, lineage records and relays, notifications acknowledges (`lance.busEnv`).
BUS_CLIENTS = (CATALOG, LINEAGE, NOTIFICATIONS)
#: The dev fixtures standing in for the platform's dependencies.
STORE = os.environ.get("LANCE_E2E_STORE_WORKLOAD", "rask-minio")
BUS = os.environ.get("LANCE_E2E_BUS_WORKLOAD", "rask-nats")
ISSUER = os.environ.get("LANCE_E2E_ISSUER_SERVICE", "rask-dex")
SECRET_STORE = os.environ.get("LANCE_E2E_SECRET_STORE_WORKLOAD", "rask-openbao")

CATALOG_URL = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
STORE_URL = os.environ.get("LANCE_E2E_S3", "").rstrip("/")
#: The catalog's container port (`services.catalog.port`), which its readiness is read on from inside the pod.
CATALOG_PORT = int(os.environ.get("LANCE_E2E_CATALOG_PORT", "2333"))
#: A signer re-reads its key from the store every 300 s while it holds one (`service_kit.governed.signing_key`), so it
#: drops the key within that of the store's loss; the rest is the slack of one failed re-read.
SIGNER_DROP_SECONDS = 360.0
#: How long a sidecar's NATS client goes on reconnecting before it closes for good: nats.go v1.39.1's 60 attempts per
#: server, 2 s apart, which Dapr 1.18.1's jetstream component leaves at their defaults (`service_kit.bus_watchdog`).
RECONNECT_WINDOW_SECONDS = 120.0
#: The container the Dapr injector adds to every annotated pod, an ordinary container while rask sets no native sidecar.
SIDECAR = "daprd"
#: How many times Lance issues a failed listing per dataset open, whatever `client_max_retries` says (measured on
#: pylance 12.0.0, `service_kit.lakehouse.objectfs.StoreTimeouts`).
LISTINGS_PER_OPEN = 6


def kubectl(*args: str, timeout: float = SETTLE_SECONDS, answers: frozenset[int] = frozenset({0}), stdin: str | None = None) -> str:
    """Run ``kubectl args`` in the release's namespace, writing ``stdin`` to it; its stdout. An exit code outside
    ``answers`` raises, naming the command and what kubectl said, never what it was given on stdin: a command run with
    `exec` may answer with a code of its own, as `bao status` answers 2 for sealed."""
    command = ["kubectl", *(("--namespace", KUBE_NAMESPACE) if KUBE_NAMESPACE else ()), *args]
    done = subprocess.run(command, input=stdin, capture_output=True, text=True, check=False, timeout=timeout)  # noqa: S603 — a fixed tool, argv built here
    assert done.returncode in answers, f"{' '.join(command)} exited {done.returncode}: {done.stderr.strip()[:500]}"
    return done.stdout.strip()


class Workload(BaseModel):
    """A Deployment or StatefulSet of the release, as `kubectl` names it."""

    kind: Literal["deployment", "statefulset"]
    name: str

    @property
    def ref(self) -> str:
        return f"{self.kind}/{self.name}"

    def replicas(self) -> int:
        """The replicas its spec asks for."""
        return int(kubectl("get", self.ref, "-o", "jsonpath={.spec.replicas}") or 0)

    def selector(self) -> str:
        """Its pods' label selector, from the spec's `matchLabels`."""
        labels: dict[str, str] = json.loads(kubectl("get", self.ref, "-o", "jsonpath={.spec.selector.matchLabels}"))
        return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))

    def pods(self) -> list[str]:
        """The names of the pods its selector matches, terminating ones included."""
        return kubectl("get", "pods", "-l", self.selector(), "-o", "name").split()


def until(holds: Callable[[], bool], failure: str, *, within: float = SETTLE_SECONDS, every: float = 2.0) -> None:
    """Poll ``holds`` until it is true; fail with ``failure`` once ``within`` seconds are spent."""
    deadline = time.monotonic() + within
    while not holds():
        assert time.monotonic() < deadline, f"{failure} within {within:.0f} s"
        time.sleep(every)


def endpoints(service: str) -> list[str]:
    """The addresses of the ready endpoints the Service ``service`` sends to. Read as JSON: a slice with no endpoints
    carries `endpoints: null`, which kubectl's jsonpath cannot filter."""
    slices = json.loads(kubectl("get", "endpointslices", "-l", f"kubernetes.io/service-name={service}", "-o", "json")).get("items") or []
    return [
        endpoint["addresses"][0]
        for item in slices
        for endpoint in item.get("endpoints") or []
        if (endpoint.get("conditions") or {}).get("ready") and endpoint.get("addresses")
    ]


#: The label a cut Service's selector asks for, which no pod carries.
CUT_LABEL = "rask.dev/chaos-cut"


@contextmanager
def unreachable(service: str) -> Generator[None]:
    """The Service ``service`` sending to no pod: its selector asks for a label no pod carries until its last endpoint is
    gone, then the label is taken out of the selector again and its endpoints come back. The pods behind it keep running,
    and keep what they hold in memory: a dev issuer's signing key, a dev store's contents."""
    assert endpoints(service), f"service/{service} sends to no pod already, so cutting it proves nothing"
    kubectl("patch", f"service/{service}", "--type=merge", "-p", json.dumps({"spec": {"selector": {CUT_LABEL: "unreachable"}}}))
    try:
        until(lambda: not endpoints(service), f"service/{service} still sends to a pod after its cut")
        yield
    finally:
        kubectl("patch", f"service/{service}", "--type=merge", "-p", json.dumps({"spec": {"selector": {CUT_LABEL: None}}}))
        until(lambda: bool(endpoints(service)), f"service/{service} sends to no pod after its cut was undone")


def restarted(workload: Workload) -> None:
    """``workload`` rolled onto new pods, returning once the old ones are gone: a pod that is draining still answers."""
    old = set(workload.pods())
    kubectl("rollout", "restart", workload.ref)
    kubectl("rollout", "status", workload.ref, f"--timeout={SETTLE_SECONDS:.0f}s")
    until(lambda: not old & set(workload.pods()), f"{workload.ref}'s old pods are still running after its restart")


@contextmanager
def scaled_to_zero(workload: Workload) -> Generator[None]:
    """``workload`` with no pod: scaled to zero until every pod of it is gone, then back to the replicas it had and rolled
    out again."""
    replicas = workload.replicas()
    assert replicas > 0, f"{workload.ref} already runs no pod, so taking it down proves nothing"
    kubectl("scale", workload.ref, "--replicas=0")
    try:
        until(lambda: not workload.pods(), f"{workload.ref} still runs a pod after its scale to zero")
        yield
    finally:
        kubectl("scale", workload.ref, f"--replicas={replicas}")
        kubectl("rollout", "status", workload.ref, f"--timeout={SETTLE_SECONDS:.0f}s")


def killed(workload: Workload, *, container: str) -> None:
    """The process ``container`` runs in ``workload``'s pod killed with SIGKILL, returning once the kubelet has restarted
    the container and the pod is Ready again.

    The signal is sent from inside the container, by its own uid, to the one process whose command line names uvicorn
    and is not the container's PID 1: the kernel drops a SIGKILL sent to a namespace's PID 1 from inside it, which is why
    the image's tini must be PID 1 (LH-313). The kill fails the drill unless it found exactly one such process.
    """
    pods = workload.pods()
    assert len(pods) == 1, f"{workload.ref} runs {len(pods)} pods; the kill drill expects one"
    pod = pods[0].removeprefix("pod/")
    restarts = _restarts(pod, container)
    script = (
        "import os, signal\n"
        "me = os.getpid()\n"
        "hits = []\n"
        "for pid in filter(str.isdigit, os.listdir('/proc')):\n"
        "    if int(pid) in (1, me):\n"
        "        continue\n"
        "    try:\n"
        "        cmdline = open(f'/proc/{pid}/cmdline', 'rb').read()\n"
        "    except OSError:\n"
        "        continue\n"
        "    if b'uvicorn' in cmdline:\n"
        "        hits.append(int(pid))\n"
        "assert len(hits) == 1, f'expected one uvicorn process, found {hits}'\n"
        "os.kill(hits[0], signal.SIGKILL)\n"
    )
    kubectl("exec", pod, "-c", container, "--", "python", "-c", script)
    until(lambda: _restarts(pod, container) > restarts, f"{pod}'s {container} was not restarted after its kill")
    kubectl("wait", f"pod/{pod}", "--for=condition=Ready", f"--timeout={SETTLE_SECONDS:.0f}s")


def _restarts(pod: str, container: str) -> int:
    return int(kubectl("get", "pod", pod, "-o", f"jsonpath={{.status.containerStatuses[?(@.name=='{container}')].restartCount}}") or 0)


def sidecar_restarts(deployments: Iterable[str]) -> dict[str, int]:
    """How often the sidecar of each pod of the release's Deployments ``deployments`` has restarted, keyed by pod."""
    pods = [pod.removeprefix("pod/") for name in deployments for pod in Workload(kind="deployment", name=name).pods()]
    return {pod: _restarts(pod, SIDECAR) for pod in pods}


def sidecars_restarted(before: Mapping[str, int]) -> bool:
    """Whether every pod of ``before`` runs a sidecar restarted since it was read, and is Ready again."""
    return all(_restarts(pod, SIDECAR) > count and _ready(pod) for pod, count in before.items())


def _ready(pod: str) -> bool:
    return kubectl("get", "pod", pod, "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}") == "True"


@contextmanager
def sealed_store() -> Generator[None]:
    """The dev secret store sealed in place, then unsealed with the key its server printed when it started.

    `server -dev` keeps the store in memory, so sealing keeps every value it holds, where a new pod would mint every
    signing pair and the bus's trust root again (`docs/OPERATORS.md` § 6). The seal is refused unless the unseal key is
    in the server's log first, so a drill can never leave the store sealed for good. The key reaches `bao` on the exec's
    stdin, never as an argument, so no process listing and no failed call's message carries it.
    """
    workload = Workload(kind="deployment", name=SECRET_STORE)
    pods = workload.pods()
    assert len(pods) == 1, f"{workload.ref} runs {len(pods)} pods; the seal drill expects one"
    pod = pods[0].removeprefix("pod/")
    found = re.search(r"Unseal Key: (\S+)", kubectl("logs", pod, "-c", "openbao"))
    assert found, f"{pod}'s server log names no unseal key, so a sealed store could not be unsealed again; not sealing it"
    unseal_key = found.group(1)
    kubectl("exec", pod, "-c", "seed", "--", "bao", "operator", "seal")
    try:
        until(lambda: _sealed(pod), f"{pod} did not seal")
        yield
    finally:
        kubectl("exec", "-i", pod, "-c", "seed", "--", "bao", "write", "-format=json", "sys/unseal", "key=-", stdin=unseal_key)
        kubectl("wait", f"pod/{pod}", "--for=condition=Ready", f"--timeout={SETTLE_SECONDS:.0f}s")


def _sealed(pod: str) -> bool:
    """Whether the store in ``pod`` reports itself sealed. `bao status` exits 2 for a sealed store and 0 for an unsealed
    one, with the same JSON either way."""
    status: dict[str, object] = json.loads(kubectl("exec", pod, "-c", "seed", "--", "bao", "status", "-format=json", answers=frozenset({0, 2})))
    return status.get("sealed") is True


def catalog_signs() -> bool:
    """Whether the catalog holds its signing key, as its own readiness says, read from inside its pod: off the Service,
    which drops a pod that does not."""
    probe = (
        "import json, urllib.error, urllib.request\n"
        "try:\n"
        f"    body = urllib.request.urlopen('http://127.0.0.1:{CATALOG_PORT}/readyz', timeout=5).read()\n"
        "except urllib.error.HTTPError as error:\n"
        "    body = error.read()\n"
        "print(json.dumps(json.loads(body).get('components') or {}))\n"
    )
    components: dict[str, object] = json.loads(kubectl("exec", f"deployment/{CATALOG}", "-c", "catalog", "--", "python", "-c", probe))
    return "signing" not in components


def catalog_answers() -> bool:
    """Whether the catalog answers its liveness at the address the suites reach it at."""
    try:
        return requests.get(f"{CATALOG_URL}/livez", timeout=5).status_code == 200
    except requests.RequestException:
        return False


def store_answers() -> bool:
    """Whether the object store answers its liveness at the address the suites reach it at."""
    try:
        return requests.get(f"{STORE_URL}/minio/health/live", timeout=5).status_code == 200
    except requests.RequestException:
        return False


def bus_answers() -> bool:
    """Whether the bus answers for the stream the catalog's announcements ride: a stream leader exists again."""
    try:
        bus.last_seq("LINEAGE")
    except Exception:  # noqa: BLE001 — any failure is the bus not answering yet
        return False
    return True


def store_outage_answer_bound() -> float:
    """How long a catalog door may take to answer while its object store is gone, from the deployed catalog's own
    metadata-plane bounds.

    The first store call every door makes is on that plane, and one dataset open there issues its listing
    `LISTINGS_PER_OPEN` times, each attempt bounded by the connect and request timeouts. Twice that covers the registry
    read before it and the backoff between attempts.
    """
    deployed = kubectl("get", f"deployment/{CATALOG}", "-o", "jsonpath={.spec.template.spec.containers[?(@.name=='catalog')].env}")
    env = {item["name"]: item.get("value", "") for item in json.loads(deployed or "[]")}
    bounds = CatalogReadBounds.model_validate({name: value for name, value in env.items() if name.startswith("LANCE_STORE_")})
    per_call = (bounds.store_max_retries + 1) * max(bounds.store_connect_timeout_s, bounds.store_request_timeout_s)
    return 2 * LISTINGS_PER_OPEN * per_call
