"""Ray as a `RayJob` CUSTOM RESOURCE, not a POST to the Jobs API — `open_compute-decoupling.md` §7.4 step 3.

The estate submits stage work by POSTing to Ray's Jobs REST API. That works and has one structural
cost the whole watcher is built around: **Ray's GCS is not fault-tolerant here** (no external Redis,
a standing rule), so a head restart takes every job record with it. `medallion/workflow.py` carries
the consequence — `MAX_UNSEEN_POLLS`, `MAX_RESUBMITS`, and a poll ceiling that disentangles three
distinct meanings from one `None`.

A `RayJob` CR is an **etcd object**. It survives a head restart, a controller restart and this pod, so
this adapter is the estate's first executor that may honestly advertise `Capability.DURABLE_RECORD` —
and `executor.may_resubmit` then REFUSES a resubmit on `UNKNOWN`, because against a durable record an
unknown handle means "we could not ask", never "the work was lost". The resubmit machinery is not
deleted, it is switched off by the capability, which is the entire reason that rule lives on the port.

It is also what makes Kueue possible (step 4): Kueue admits WORKLOADS, and a Jobs-API submission is
not one. A CR carrying `kueue.x-k8s.io/queue-name` is.

**httpx against the k8s API, no client library.** `ray_submit` states the rule this follows — "uses
only `httpx` … no `ray` package in the mover image" — and the same argument applies harder here: this
module ships inside an image seven lance services share, so a dependency added for one of them is
carried by all. The projected ServiceAccount token and the cluster CA are files every pod already has.

All IO is async; the caller awaits it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

import httpx

from service_kit.lakehouse.executor import Capability, RunFailure, RunHandle, RunState, SubmitOutcome
from service_kit.lakehouse.task_registry import TaskRegistration
from service_kit.lakehouse.work_order import WorkOrder


log = logging.getLogger(__name__)

RAY_ENGINE: Final = "ray"

#: Where every pod finds its own identity. Projected by the kubelet, rotated by it, and readable
#: without any client library — which is the whole reason this module needs none.
_TOKEN_FILE: Final = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
_CA_FILE: Final = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
_NAMESPACE_FILE: Final = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")

#: KubeRay's `.status.jobStatus`, mapped onto the port's vocabulary.
#:
#: `PENDING` and `RUNNING` are Ray's own; `STOPPED` is a terminated job, which is CANCELLED rather
#: than FAILED because somebody asked for it. An empty status is a CR that exists and has not been
#: reconciled yet — PENDING, never UNKNOWN: the record is right there, so "we cannot find it" would
#: be a lie that licenses a resubmit.
_JOB_STATUS: Final = {
    "": RunState.PENDING,
    "PENDING": RunState.PENDING,
    "RUNNING": RunState.RUNNING,
    "SUCCEEDED": RunState.SUCCEEDED,
    "FAILED": RunState.FAILED,
    "STOPPED": RunState.CANCELLED,
}


class RayJobSubmissionError(RuntimeError):
    """The CR could not be created, read or deleted. Distinct from a job that FAILED."""


class WrongEngineError(ValueError):
    """The task belongs to another executor — refused rather than run here."""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class RayJobExecutor:
    """Submits a stage as a `RayJob` CR. Conforms to `service_kit.lakehouse.executor.Executor`.

    The handle is the CR's NAME, derived from the order's idempotency key — so a redelivered order
    creates the same object, the API server answers 409, and this reports `REATTACHED` rather than
    starting a second job. That is the same property a deterministic Jobs-API submission id gives,
    expressed with the arbiter that cannot lose it.
    """

    name = RAY_ENGINE
    #: DURABLE_RECORD is the point of this adapter and the one capability the Jobs-API path cannot
    #: claim. CANCEL is real too: deleting the CR stops the job, which a Jobs-API terminate does not
    #: reliably do here.
    capabilities = frozenset({Capability.DURABLE_RECORD, Capability.CANCEL, Capability.FAILURE_DETAIL})

    def __init__(
        self,
        *,
        api_server: str = "https://kubernetes.default.svc",
        namespace: str = "",
        queue: str = "",
        cluster_selector: dict[str, str] | None = None,
        cluster_spec: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        #: WHICH CLUSTER, and this module invents neither shape. KubeRay's own validating webhook
        #: refuses a RayJob that names none — measured live 2026-09-04:
        #: `spec.rayClusterSpec: Required value: rayClusterSpec is required`. A `clusterSelector`
        #: targets an existing RayCluster; a `rayClusterSpec` has KubeRay create an ephemeral one,
        #: which is also what makes the job a Kueue-sizeable workload. Which applies is a DEPLOYMENT
        #: fact — the rule the registration's `command` already follows — so both arrive as config and
        #: this adapter forwards whichever it was given.
        self._cluster_selector = dict(cluster_selector or {})
        self._cluster_spec = dict(cluster_spec or {})
        self._api = api_server.rstrip("/")
        self._namespace = namespace or _read(_NAMESPACE_FILE) or "default"
        #: The Kueue local queue this workload is admitted through (§7.4 step 4). EMPTY means no
        #: admission control, and the CR is created without the label rather than with a blank one —
        #: Kueue treats a label naming a queue that does not exist as an unadmittable workload, which
        #: would silently park every job.
        self._queue = queue
        self._client = client

    # -- the port -------------------------------------------------------------------------------

    def validate_task(self, registration: TaskRegistration) -> None:
        if registration.engine != RAY_ENGINE:
            raise WrongEngineError(f"task {registration.task!r} is registered for engine {registration.engine!r}, and this executor runs {RAY_ENGINE!r}")

    async def submit(self, order: WorkOrder, registration: TaskRegistration) -> tuple[RunHandle, SubmitOutcome]:
        """Create the CR, or re-attach to the one this order already created."""
        self.validate_task(registration)
        handle = RunHandle(engine=RAY_ENGINE, handle=_cr_name(order))
        body = self.manifest(order, registration)
        response = await self._request("POST", self._collection(), json=body)
        if response.status_code == 409:
            return handle, SubmitOutcome.REATTACHED
        if response.status_code >= 400:
            raise RayJobSubmissionError(f"could not create RayJob {handle.handle!r}: {response.status_code} {response.text[:200]}")
        log.info("rayjob_submitted", extra={"name": handle.handle, "task": order.task, "queue": self._queue or "<none>"})
        return handle, SubmitOutcome.SUBMITTED

    async def status(self, handle: RunHandle) -> RunState:
        response = await self._request("GET", f"{self._collection()}/{handle.handle}")
        if response.status_code == 404:
            # The CR is GONE. Against a durable record that means deleted, not lost — and
            # `may_resubmit` refuses to resubmit on UNKNOWN for an engine advertising
            # DURABLE_RECORD, which is exactly the protection this adapter buys.
            return RunState.UNKNOWN
        if response.status_code >= 400:
            raise RayJobSubmissionError(f"could not read RayJob {handle.handle!r}: {response.status_code} {response.text[:200]}")
        return _JOB_STATUS.get(str((response.json().get("status") or {}).get("jobStatus") or ""), RunState.UNKNOWN)

    async def failure(self, handle: RunHandle) -> RunFailure | None:
        response = await self._request("GET", f"{self._collection()}/{handle.handle}")
        if response.status_code >= 400:
            return None
        status = response.json().get("status") or {}
        if str(status.get("jobStatus") or "") != "FAILED":
            return None
        message = str(status.get("message") or status.get("jobDeploymentStatus") or "the RayJob reported FAILED with no message")
        # `kind` is a CLASSIFICATION a caller can branch on; the message is free text and is not one.
        # OOM is separated from a driver error because they need opposite operator responses — one is
        # a sizing problem, the other a code one — and 137 is SIGKILL under any engine, which is the
        # portable half of that signal. `exit_code` stays absent: KubeRay does not report one, and
        # inventing a number no field carries would be worse than the honest omission.
        kind = "oom" if "oom" in message.lower() or "137" in message else "driver_error"
        return RunFailure(kind=kind, message=message[:2000])

    async def cancel(self, handle: RunHandle) -> None:
        """Delete the CR, which stops the job. Advertised as `Capability.CANCEL` because it works."""
        response = await self._request("DELETE", f"{self._collection()}/{handle.handle}")
        if response.status_code >= 400 and response.status_code != 404:
            raise RayJobSubmissionError(f"could not cancel RayJob {handle.handle!r}: {response.status_code} {response.text[:200]}")

    # -- the manifest ---------------------------------------------------------------------------

    def manifest(self, order: WorkOrder, registration: TaskRegistration) -> dict[str, Any]:
        """The `RayJob` this order becomes. Pure, so a test can read it without an API server.

        `entrypoint` is the REGISTRATION's command, never anything derived from the order: what
        running a task means belongs to the plane that registered it, and this adapter forwards the
        string without parsing it.

        The env comes from `WorkOrder.to_env()`, which is the ONE serialization of an order and
        deliberately carries no credential — `credential_ref` NAMES one. The Ray pods hold their own,
        which is also why nothing here echoes a secret into a CR that anyone with read access can see.
        """
        labels: dict[str, str] = {"app.kubernetes.io/managed-by": "rask-medallion", "rask.io/task": _label(order.task)}
        if self._queue:
            labels["kueue.x-k8s.io/queue-name"] = self._queue
        if not self._cluster_selector and not self._cluster_spec:
            # REFUSED HERE, with the reason, rather than by the admission webhook 400 milliseconds
            # later with `spec.rayClusterSpec: Required value`. A caller that has misconfigured its
            # deployment learns which of the two knobs is missing instead of reading a KubeRay error.
            raise RayJobSubmissionError(
                "a RayJob must name a cluster: set a clusterSelector to target an existing RayCluster, or a rayClusterSpec to have KubeRay create one"
            )
        cluster: dict[str, Any] = {"clusterSelector": self._cluster_selector} if self._cluster_selector else {"rayClusterSpec": self._cluster_spec}
        return {
            "apiVersion": "ray.io/v1",
            "kind": "RayJob",
            "metadata": {
                "name": _cr_name(order),
                "namespace": self._namespace,
                "labels": labels,
                # WHO this is for, readable from OUTSIDE the job after it dies — the same reason the
                # Jobs-API path stamps Ray `metadata`. Annotations rather than labels: a project or a
                # subject is not a selector and need not obey label syntax.
                "annotations": {
                    k: v
                    for k, v in (
                        ("rask.io/project", order.identity.project),
                        ("rask.io/originator", order.identity.originator),
                        ("rask.io/run-id", order.identity.run_id),
                    )
                    if v
                },
            },
            "spec": {
                "entrypoint": registration.command,
                # SUSPEND when a queue governs this. Kueue admits by unsuspending, so a workload
                # created running has already bypassed the admission it was labelled for.
                "suspend": bool(self._queue),
                # FALSE when an existing cluster is targeted — tearing down a SHARED cluster because
                # one job finished would take every other job with it. True only makes sense for an
                # ephemeral cluster this job created, which is `rayClusterSpec`'s case.
                "shutdownAfterJobFinishes": bool(self._cluster_spec),
                "runtimeEnvYAML": _runtime_env_yaml(order),
                **cluster,
            },
        }

    # -- transport ------------------------------------------------------------------------------

    def _collection(self) -> str:
        return f"{self._api}/apis/ray.io/v1/namespaces/{self._namespace}/rayjobs"

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        client = self._client or httpx.AsyncClient(verify=str(_CA_FILE) if _CA_FILE.exists() else True)
        headers = {"Authorization": f"Bearer {_read(_TOKEN_FILE)}", "Content-Type": "application/json"}
        try:
            if self._client is not None:
                return await client.request(method, url, headers=headers, **kwargs)
            async with client:
                return await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise RayJobSubmissionError(f"the kubernetes API is unreachable: {exc}") from exc


def _cr_name(order: WorkOrder) -> str:
    """A DNS-1123 name derived from the order's idempotency key.

    Deterministic, because that is what makes a redelivered order one job: the API server refuses the
    duplicate with 409 and `submit` reports REATTACHED. A random name would make every redelivery a
    second run, which is the defect the Jobs API's deterministic submission id already avoids.
    """
    import hashlib

    digest = hashlib.sha256(order.idempotency_key.encode()).hexdigest()[:16]
    return f"rask-stage-{digest}"


def _label(value: str) -> str:
    """A label-safe rendering. Labels are 63 chars of `[a-z0-9A-Z._-]`, and a task key is author-chosen,
    so an unsanitised one makes the API server refuse the whole CR."""
    safe = "".join(c if c.isalnum() or c in "-._" else "-" for c in value)
    return safe[:63].strip("-._") or "unnamed"


def _runtime_env_yaml(order: WorkOrder) -> str:
    """The order's env, as the YAML document KubeRay expects.

    Hand-rendered rather than via a YAML library: the values are the platform's own strings, this
    module ships in an image seven services share, and the quoting rule is one line. Every value is
    JSON-quoted, which is valid YAML and correct for a string containing a colon, a brace or a newline.
    """
    import json

    lines = "\n".join(f"    {key}: {json.dumps(value)}" for key, value in sorted(order.to_env().items()))
    return f"env_vars:\n{lines}\n" if lines else "env_vars: {}\n"
