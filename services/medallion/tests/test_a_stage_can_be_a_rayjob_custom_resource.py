"""Ray as a `RayJob` CR rather than a POST to the Jobs API — docs/DECISIONS.md "The compute plane is decoupled" (§7.4) step 3.

**Why it is worth doing at all, stated as the capability it changes.** Ray's GCS is not fault-tolerant
in this estate (no external Redis, a standing rule), so a head restart takes every job record with it —
and `medallion/workflow.py` is built around that: `MAX_UNSEEN_POLLS`, `MAX_RESUBMITS`, a poll ceiling,
and three distinct meanings disentangled from one `None`. A `RayJob` is an etcd object. It survives a
head restart, a controller restart and the submitting pod, so this is the estate's first executor that
may honestly advertise `DURABLE_RECORD` — and `may_resubmit` then REFUSES a resubmit on `UNKNOWN`.
That is the resubmit machinery being switched off BY THE CAPABILITY rather than deleted, which is the
whole reason the rule lives on the port instead of in each caller.

It is also what makes Kueue possible (step 4): Kueue admits WORKLOADS, and a Jobs-API POST is not one.

Driven against a stubbed API server, because what is under test is the MANIFEST and the state
mapping — not whether httpx can reach kubernetes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from medallion.services.rayjob_executor import RayJobExecutor, RayJobSubmissionError, WrongEngineError
from service_kit.lakehouse.executor import Capability, Executor, RunHandle, RunState, SubmitOutcome, may_resubmit
from service_kit.lakehouse.task_registry import TaskRegistration
from service_kit.lakehouse.work_order import WorkDestination, WorkIdentity, WorkOrder, WorkSource, WorkStamp


def _order(**over: Any) -> WorkOrder:
    base: dict[str, Any] = {
        "task": "stage-transform",
        "source": WorkSource(uri="s3://b/bronze", table_id="acme-bronze$events"),
        "destination": WorkDestination(uri="s3://b/silver", table_id="acme-silver$features"),
        "stamp": WorkStamp(stage="silver", cardinality="1:1"),
        "identity": WorkIdentity(run_id="run-1", project="acme", originator="alice"),
        "idempotency_key": "silver:tok:bronze$events->silver$features",
    }
    return WorkOrder.model_validate(base | over)


_REGISTRATION = TaskRegistration(task="stage-transform", engine="ray", command="python /home/ray/jobs/ray_stage_job.py")


#: An existing cluster to target. Every executor here names one, because KubeRay's own webhook
#: refuses a RayJob that names none — a fixture that omitted it would test a shape the API server
#: rejects.
_SELECTOR = {"ray.io/cluster": "rask-ray"}


def _executor(handler: Any, *, queue: str = "") -> RayJobExecutor:
    return RayJobExecutor(namespace="default", queue=queue, cluster_selector=_SELECTOR, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _plain(**over: Any) -> RayJobExecutor:
    return RayJobExecutor(namespace="default", cluster_selector=_SELECTOR, **over)


def test_it_SATISFIES_the_port_and_claims_a_durable_record() -> None:
    """The capability is the point. Asked at runtime rather than assumed."""
    ex = _plain()
    assert isinstance(ex, Executor)
    assert Capability.DURABLE_RECORD in ex.capabilities
    assert Capability.CANCEL in ex.capabilities


def test_a_DURABLE_record_turns_the_resubmit_machinery_OFF() -> None:
    """Against the Jobs API an UNKNOWN handle licenses a resubmit, because a lost GCS record means the
    work is gone. Against a CR it does not: the object is in etcd, so UNKNOWN means "we could not
    ask", and resubmitting would put two writers on one destination."""
    ex = _plain()
    assert not may_resubmit(RunState.UNKNOWN, capabilities=ex.capabilities)
    assert may_resubmit(RunState.UNKNOWN, capabilities=frozenset()), "an engine promising nothing still licenses it"


def test_the_manifest_takes_its_ENTRYPOINT_from_the_REGISTRATION() -> None:
    """What running a task means belongs to the plane that registered it. The order names a task; it
    never carries a command, and this adapter forwards the registration's string without parsing it."""
    manifest = _plain().manifest(_order(), _REGISTRATION)

    assert manifest["spec"]["entrypoint"] == "python /home/ray/jobs/ray_stage_job.py"
    assert manifest["kind"] == "RayJob" and manifest["apiVersion"] == "ray.io/v1"


def test_the_manifest_carries_NO_CREDENTIAL() -> None:
    """A CR is readable by anyone with get on the namespace. `WorkOrder.credential_ref` NAMES a
    credential and never carries one, and the Ray pods hold their own — the same reason the Jobs-API
    path stopped echoing S3_SECRET through a runtime_env the dashboard mirrors."""
    rendered = json.dumps(_plain().manifest(_order(), _REGISTRATION))

    for forbidden in ("secret", "aws_secret_access_key", "password", "token"):
        assert forbidden not in rendered.lower(), f"the manifest leaked {forbidden!r}"


def test_a_QUEUE_makes_it_a_KUEUE_WORKLOAD_and_suspends_it() -> None:
    """Step 4. Kueue admits by UNSUSPENDING, so a workload created running has already bypassed the
    admission it was labelled for — the label and the suspend are one decision, not two."""
    manifest = _plain(queue="rask-default").manifest(_order(), _REGISTRATION)

    assert manifest["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "rask-default"
    assert manifest["spec"]["suspend"] is True


def test_NO_queue_means_no_label_and_no_suspend() -> None:
    """An empty queue must not become a blank label: Kueue treats a label naming a queue that does not
    exist as an unadmittable workload, so every job would park forever with nothing saying why."""
    manifest = _plain().manifest(_order(), _REGISTRATION)

    assert "kueue.x-k8s.io/queue-name" not in manifest["metadata"]["labels"]
    assert manifest["spec"]["suspend"] is False


def test_the_NAME_is_deterministic_so_a_redelivery_is_ONE_job() -> None:
    """The API server is the arbiter: the duplicate create answers 409 and this reports REATTACHED.
    A random name would make every redelivery a second run."""
    first = _plain().manifest(_order(), _REGISTRATION)["metadata"]["name"]
    second = _plain().manifest(_order(), _REGISTRATION)["metadata"]["name"]
    other = _plain().manifest(_order(idempotency_key="different"), _REGISTRATION)["metadata"]["name"]

    assert first == second and first != other
    assert first.startswith("rask-stage-") and len(first) <= 63


@pytest.mark.asyncio
async def test_a_DUPLICATE_create_reattaches_rather_than_failing() -> None:
    async def _conflict(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"reason": "AlreadyExists"})

    handle, outcome = await _executor(_conflict).submit(_order(), _REGISTRATION)

    assert outcome is SubmitOutcome.REATTACHED
    assert handle.engine == "ray"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_status", "expected"),
    [
        ("", RunState.PENDING),
        ("PENDING", RunState.PENDING),
        ("RUNNING", RunState.RUNNING),
        ("SUCCEEDED", RunState.SUCCEEDED),
        ("FAILED", RunState.FAILED),
        ("STOPPED", RunState.CANCELLED),
    ],
)
async def test_kuberays_job_status_maps_onto_the_ports_vocabulary(job_status: str, expected: RunState) -> None:
    """An EMPTY status is a CR the controller has not reconciled yet — PENDING, never UNKNOWN. The
    record is right there, so "we cannot find it" would be a lie, and against a durable engine that
    lie is what a resubmit would be built on. STOPPED is CANCELLED because somebody asked for it."""

    async def _status(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": {"jobStatus": job_status}})

    assert await _executor(_status).status(RunHandle(engine="ray", handle="rask-stage-x")) is expected


@pytest.mark.asyncio
async def test_a_DELETED_cr_is_UNKNOWN_and_that_is_not_a_licence_to_resubmit() -> None:
    async def _gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"reason": "NotFound"})

    ex = _executor(_gone)
    assert await ex.status(RunHandle(engine="ray", handle="rask-stage-x")) is RunState.UNKNOWN
    assert not may_resubmit(RunState.UNKNOWN, capabilities=ex.capabilities)


@pytest.mark.asyncio
async def test_a_FAILURE_is_classified_and_a_SUCCEEDED_job_reports_none() -> None:
    async def _failed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": {"jobStatus": "FAILED", "message": "Job exited with code 137"}})

    detail = await _executor(_failed).failure(RunHandle(engine="ray", handle="rask-stage-x"))
    assert detail is not None and detail.kind == "oom", "137 is SIGKILL, which is a sizing problem rather than a code one"

    async def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": {"jobStatus": "SUCCEEDED"}})

    assert await _executor(_ok).failure(RunHandle(engine="ray", handle="rask-stage-x")) is None


@pytest.mark.asyncio
async def test_CANCEL_deletes_the_cr_and_tolerates_one_already_gone() -> None:
    """Advertised as a capability because it works — deleting the CR stops the job. An already-absent
    one is the outcome the caller wanted, so it is not an error."""
    seen: list[str] = []

    async def _delete(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(404 if len(seen) > 1 else 200, json={})

    ex = _executor(_delete)
    await ex.cancel(RunHandle(engine="ray", handle="rask-stage-x"))
    await ex.cancel(RunHandle(engine="ray", handle="rask-stage-x"))
    assert seen == ["DELETE", "DELETE"]


@pytest.mark.asyncio
async def test_a_task_for_ANOTHER_engine_is_refused_before_anything_is_created() -> None:
    async def _never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a task for another engine must not reach the API server")

    with pytest.raises(WrongEngineError, match="inprocess"):
        await _executor(_never).submit(_order(), TaskRegistration(task="t", engine="inprocess", command="x"))


@pytest.mark.asyncio
async def test_an_API_SERVER_error_is_its_own_failure_not_a_failed_job() -> None:
    """A CR that could not be created is a SUBMISSION fault; a job that ran and failed is a run
    outcome. Collapsing them would have the caller report a data failure for an RBAC gap."""

    async def _forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "rayjobs.ray.io is forbidden"})

    with pytest.raises(RayJobSubmissionError, match="403"):
        await _executor(_forbidden).submit(_order(), _REGISTRATION)


def test_the_env_is_the_ORDERs_one_serialization() -> None:
    """`WorkOrder.to_env()` is the single serialization, so a CR and a Jobs-API submission describe
    the same run. Values are JSON-quoted, which is valid YAML and correct for one containing a colon."""
    yaml_doc = _plain().manifest(_order(), _REGISTRATION)["spec"]["runtimeEnvYAML"]

    assert yaml_doc.startswith("env_vars:\n")
    for key in _order().to_env():
        assert f"    {key}: " in yaml_doc


def test_naming_NO_cluster_is_refused_HERE_rather_than_by_the_webhook() -> None:
    """Measured live 2026-09-04: KubeRay's validating webhook answers
    `spec.rayClusterSpec: Required value: rayClusterSpec is required`. Correct, and 400ms too late to
    be useful — a caller reading that learns KubeRay's field names rather than which of its own two
    knobs is unset. Neither shape is invented here; both are deployment facts, the same rule the
    registration's `command` follows."""
    with pytest.raises(RayJobSubmissionError, match="clusterSelector"):
        RayJobExecutor(namespace="default").manifest(_order(), _REGISTRATION)


def test_an_EPHEMERAL_cluster_is_torn_down_and_a_SHARED_one_is_not() -> None:
    """`shutdownAfterJobFinishes` follows from which shape was given, and getting it backwards is
    destructive: tearing down a SHARED cluster because one job finished takes every other job with
    it. An ephemeral cluster this job created is the only one it may reclaim."""
    shared = _plain().manifest(_order(), _REGISTRATION)
    assert shared["spec"]["shutdownAfterJobFinishes"] is False
    assert shared["spec"]["clusterSelector"] == _SELECTOR and "rayClusterSpec" not in shared["spec"]

    spec = {"headGroupSpec": {"template": {"spec": {"containers": [{"name": "ray-head", "image": "rayproject/ray:2.56.1"}]}}}}
    ephemeral = RayJobExecutor(namespace="default", cluster_spec=spec).manifest(_order(), _REGISTRATION)
    assert ephemeral["spec"]["shutdownAfterJobFinishes"] is True
    assert ephemeral["spec"]["rayClusterSpec"] == spec and "clusterSelector" not in ephemeral["spec"]
