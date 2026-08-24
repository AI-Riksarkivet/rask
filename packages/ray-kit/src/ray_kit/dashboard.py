"""Thin wrapper around Ray's Job Submission SDK + Dashboard HTTP.

The SDK (`JobSubmissionClient`) is sync and constructs with a connection check.
We cache one instance on `app.state.ray_client` in lifespan and call its
methods from the event loop via `anyio.to_thread.run_sync`.

Two surfaces stay on raw httpx because the SDK doesn't model them:
  - `cluster_status` (uses `/api/cluster_status` + `/nodes`)
  - `proxy` (forwards arbitrary paths for the iframe)

Unreachable-Ray cases return `ok=False` so the UI can render "offline" instead
of bubbling 5xx.
"""

import logging
import re
import time
from http import HTTPStatus
from urllib.parse import urlencode

import httpx
import ray
import requests
from anyio import to_thread
from ray.exceptions import AuthenticationError
from ray.job_submission import JobSubmissionClient

from ray_kit.auth import auth_headers
from ray_kit.metrics import RayOutcome, classify_ray_error, record_jobs_known, record_probe
from ray_kit.schemas import (
    ProxyResponse,
    RayActor,
    RayActorsPayload,
    RayClusterPayload,
    RayEvent,
    RayGpu,
    RayHealth,
    RayJob,
    RayJobLogsPayload,
    RayJobsPayload,
    RayLogsPayload,
    RayNode,
    RayOverviewPayload,
    RayTask,
    RayTasksPayload,
)


log = logging.getLogger(__name__)

#: How many jobs a `/jobs` read may materialise, newest first.
#:
#: Ray's Jobs API takes NO query parameters (`GET /api/jobs`, spec v4.0.0) — no limit, offset or
#: status filter — so it hands back every job the cluster has ever seen and the bound can only be
#: applied here. The live cluster measured 81,155 jobs / 164.7 MB in one response; validating all of
#: them peaked at 1179 MiB against a 1536 MiB container limit (two concurrent calls: 1488 MiB).
#:
#: 200 is chosen against the CONSUMER, not the producer: the jobs board paginates and no rask surface
#: renders more than a screenful of recent runs. `total`/`truncated` on the payload keep the cap
#: honest rather than silent.
MAX_JOBS = 200

#: Row cap for the task state API.
#:
#: This was `10000`, which is EXACTLY Ray's `RAY_MAX_LIMIT_FROM_API_SERVER` ceiling — the code asked
#: for the largest page the server will ever produce, on an endpoint polled every 5 s by two separate
#: pages. `detail=1` is unfortunately load-bearing (`required_resources`, `error_message` and the
#: three timestamps are all `state_column(detail=True)` in Ray 2.56, and the state API offers no
#: column projection), so each row carries `runtime_env_info`, `events`, `profiling_data` and
#: `call_site` whether or not anything reads them. The only lever is the row count.
MAX_TASKS = 500

# Errors meaning "Ray is unreachable / refused us", raised by the Job SDK at runtime.
# `requests.*` subclass OSError (NOT builtin ConnectionError); the SDK only translates
# its construct-time check to builtin ConnectionError, so live calls can still raise
# requests exceptions directly. AuthenticationError (a RayError) surfaces on 401/403
# from an authenticated cluster. (Its other consumers, core's submission.py +
# orchestrator/derive.py, died at P7a — the ray service and the medallion Ray seam remain.)
RAY_TRANSIENT_ERRORS = (RuntimeError, ConnectionError, requests.exceptions.RequestException, AuthenticationError)

_BATCH_RE = re.compile(r"--batch[\s=]+(\S+)")
_ERROR_MSG_MAX_LEN = 400  # truncate exception strings so they fit in one log/UI line

# Ray Dashboard JSON keys we read from /api/cluster_status and /nodes responses.
# Grouped here so a Ray-side rename is a one-spot edit.
_RAY_KEY_DATA = "data"
_RAY_KEY_CLUSTER_STATUS = "clusterStatus"
_RAY_KEY_LOAD_METRICS = "loadMetricsReport"
_RAY_KEY_USAGE = "usage"
_RAY_KEY_SUMMARY = "summary"
_RAY_KEY_NODE_LOGICAL_RESOURCES = "nodeLogicalResources"
_RAY_KEY_RAYLET = "raylet"
_RAY_KEY_NODE_ID = "nodeId"
_RAY_KEY_RESOURCES_TOTAL = "resourcesTotal"
_RAY_KEY_NODE_MANAGER_ADDRESS = "nodeManagerAddress"
_RAY_KEY_IP = "ip"
_RAY_KEY_RAYLET_STATE = "state"
_RAYLET_STATE_ALIVE = "ALIVE"
_NODES_VIEW_PARAM = "view=summary"
# Per-node host telemetry (top-level node dict) + raylet identity fields.
_RAY_KEY_HOSTNAME = "hostname"
_RAY_KEY_CPU = "cpu"  # host CPU utilization %
_RAY_KEY_MEM = "mem"  # [total, available, percent, used] bytes
_RAY_KEY_GPUS = "gpus"
_RAY_KEY_NODE_TYPE_NAME = "nodeTypeName"
_RAY_KEY_IS_HEAD = "isHeadNode"
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "x-frame-options",
    "content-security-policy",
}
# Proxy hardening for a token-authed Ray (gate 7 / R3). Inbound: browser credentials must never
# reach the dashboard — stripping the incoming `authorization` also lets the httpx client's own
# default Bearer token (auth_headers() at construction) apply instead of being shadowed by
# request-level junk; `x-ray-authorization` is Ray's fallback auth header and `cookie` could carry
# Ray's `ray-authentication-token` browser cookie. Outbound: Ray's /api/authenticate sets that
# cookie — it must never reach the browser, so the token lives only in pod env + server-side calls.
_REQUEST_STRIP = _HOP_BY_HOP | {"host", "authorization", "x-ray-authorization", "cookie"}
_RESPONSE_STRIP = _HOP_BY_HOP | {"set-cookie"}
_BYTE_UNITS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}


def _parse_batches(entrypoint: str | None) -> list[str]:
    if not entrypoint:
        return []
    return _BATCH_RE.findall(entrypoint)


def build_client(dashboard_url: str) -> JobSubmissionClient | None:
    """Construct the SDK client. Returns None if Ray's dashboard is unreachable.

    The constructor issues HTTP calls to verify the API version. Ray's SDK
    documents `RuntimeError` for protocol failures and raises `ConnectionError`
    when the dashboard isn't listening; we treat both as "offline".

    Against a token-authed cluster (RAY_AUTH_MODE=token) the Bearer token from
    RASK_RAY_AUTH_TOKEN / RAY_AUTH_TOKEN is attached as default headers — passed
    explicitly so it works even when the calling process doesn't itself export
    RAY_AUTH_MODE (Ray's own auto-attach requires that). No token => no headers,
    exactly the previous behavior.
    """
    try:
        client = JobSubmissionClient(address=dashboard_url, headers=auth_headers() or None)
    except RAY_TRANSIENT_ERRORS as exc:
        # CLASSIFY BEFORE DISCARDING. `RAY_TRANSIENT_ERRORS` catches four causes with one `except`,
        # and this function can only answer `None` — so without the classification a rotated token, a
        # missing RASK_RAY_AUTH_TOKEN and a scope mistake all reach an operator as the fixed literal a
        # dead cluster produces ("Ray dashboard unreachable"), sending them to debug KubeRay, the node
        # pool and networking when the fix is a Secret.
        #
        # WARNING, not INFO: this is the estate losing its only window onto Ray. At INFO it was below
        # the level the fleet's stdout handler emits by default, so the one line naming the real cause
        # was not merely buried — on a default deployment it was never written.
        outcome = classify_ray_error(exc)
        record_probe("build_client", outcome)
        log.warning(
            "ray_client_unavailable",
            extra={"dashboard_url": dashboard_url, "outcome": outcome.value, "error_type": type(exc).__name__, "error": str(exc)[:_ERROR_MSG_MAX_LEN]},
        )
        return None
    record_probe("build_client", RayOutcome.OK)
    return client


async def health(client: JobSubmissionClient | None, dashboard_url: str) -> RayHealth:
    # EVERY EXIT RECORDS. This route answers HTTP 200 even when Ray is dead, so the automatic
    # `http.server.*` series cannot see the failure — a totally dead head renders as 0% error rate on
    # the RED dashboard. `ray.control.probes{op="health"}` is the only series a rule can fire on.
    started = time.perf_counter()
    if client is None:
        record_probe("health", RayOutcome.UNREACHABLE, duration_seconds=time.perf_counter() - started)
        return RayHealth(ok=False, dashboard_url=dashboard_url, error="Ray dashboard unreachable")
    try:
        await to_thread.run_sync(client.get_version)
    except RAY_TRANSIENT_ERRORS as exc:
        record_probe("health", classify_ray_error(exc), duration_seconds=time.perf_counter() - started)
        return RayHealth(ok=False, dashboard_url=dashboard_url, error=f"{type(exc).__name__}: {exc!s}"[:_ERROR_MSG_MAX_LEN])
    record_probe("health", RayOutcome.OK, duration_seconds=time.perf_counter() - started)
    # get_version() is called purely as a liveness probe (its return is the
    # Jobs-API version, not the cluster Ray version — see RayHealth).
    return RayHealth(ok=True, dashboard_url=dashboard_url, client_ray_version=ray.__version__)


#: The metadata keys this projection keeps — an EXPLICIT ALLOWLIST, not a prefix.
#:
#: A `rask.` prefix was tried first and is wrong: the medallion's submitter also stamps
#: ``rask.token`` (`ray_submit.py`), so a prefix match puts that token into every jobs-board row —
#: precisely the leak `test_ray_job_wire_parity` was written to prevent, and precisely why
#: `metadata` was stripped whole before this. An allowlist cannot regress that way: a new key is
#: kept only when someone adds it here and says why.
#:
#: What survives is the run's IDENTITY — who it was for, which tenant, which stage, which
#: declaration. Not the work token, which names nothing a reader needs and is the one value the
#: original strip existed to contain.
_IDENTITY_KEYS = frozenset({"rask.lane", "rask.stage", "rask.project", "rask.originator"})


async def list_jobs(client: JobSubmissionClient | None, dashboard_url: str, *, max_jobs: int = MAX_JOBS) -> RayJobsPayload:
    started = time.perf_counter()
    if client is None:
        record_probe("list_jobs", RayOutcome.UNREACHABLE, duration_seconds=time.perf_counter() - started)
        return RayJobsPayload(ok=False, dashboard_url=dashboard_url, error="Ray dashboard unreachable")
    try:
        details = await to_thread.run_sync(client.list_jobs)
    except RAY_TRANSIENT_ERRORS as exc:
        record_probe("list_jobs", classify_ray_error(exc), duration_seconds=time.perf_counter() - started)
        return RayJobsPayload(ok=False, dashboard_url=dashboard_url, error=f"{type(exc).__name__}: {exc!s}"[:_ERROR_MSG_MAX_LEN])
    # The call this timing exists for: `GET /api/jobs/` accepts NO parameters, so it always returns
    # every job Ray has ever seen. Measured at 81,155 jobs / 164.7 MB, which OOM-killed the pod. The
    # cap below fixed the crash and made the GROWTH invisible; `ray.control.jobs_known` is the series
    # that shows it coming back, and the duration is the cost of the call that carries it.
    record_probe("list_jobs", RayOutcome.OK, duration_seconds=time.perf_counter() - started)
    record_jobs_known(len([d for d in details if d is not None]))
    # SORT AND CAP FIRST, VALIDATE SECOND. The old order — validate every job, then sort — built a
    # `.dict()` copy AND a `RayJob` for every job Ray had ever seen. Measured against the live
    # cluster: 81,155 jobs / 164.7 MB in one response, peaking at 1179 MiB of RSS for a single call
    # against a 1536 MiB limit, and 1488 MiB for two concurrent ones. That is the OOMKill.
    #
    # Ray's Jobs API cannot help. `GET /api/jobs` is specified (v4.0.0) with NO parameters at all —
    # no limit, no offset, no status filter — so it always returns every job ever submitted and the
    # bound has to be ours. The list only grows: the HTR pipeline submits one job per chunk and
    # dashboard job history never expires, so this is not a plateau we can wait out.
    #
    # Sorting Ray's own `JobDetails` objects is cheap (they already exist — the SDK materialised
    # them) and lets us pay the expensive `.dict()` + `model_validate` for `max_jobs` rows instead
    # of all of them.
    present = [d for d in details if d is not None]
    recent = sorted(present, key=lambda d: getattr(d, "start_time", None) or 0, reverse=True)[:max_jobs]

    jobs: list[RayJob] = []
    for d in recent:
        # `.dict()` (V1 API) is required: Ray ships `JobDetails` as a Pydantic V1 model
        # so `model_dump()` doesn't exist. See `schemas/ray.py` for the rationale.
        payload = d.dict()
        payload["batches"] = _parse_batches(d.entrypoint)
        # IDENTITY KEPT, BULK DROPPED. `metadata` is an arbitrary user dict, so carrying it whole
        # reopens exactly the unbounded-growth hole `MAX_JOBS` closed — a job may stamp any number
        # of keys of any size. But the medallion puts a job's IDENTITY here on purpose
        # (`rask.originator`/`project`/`token`/`stage`/`lane`), because `metadata` is what survives
        # to `GET /api/jobs/<id>` and is readable after the job fails.
        #
        # So the projection is by PREFIX, not all-or-nothing: the handful of `rask.` keys the estate
        # renders survive, everything else is dropped at this boundary. A job whose metadata is
        # entirely foreign keeps no metadata at all, which is what the bulk-drop test asserts and
        # still asserts unchanged.
        # Reuse `payload` — a second `d.dict()` would double the per-job cost this loop exists to
        # bound. OMITTED entirely when nothing survives, rather than left as `{}`: a job with no
        # rask identity carries no metadata, which is what the bulk-drop invariant asserts.
        identity = {k: v for k, v in (payload.get("metadata") or {}).items() if k in _IDENTITY_KEYS}
        if identity:
            payload["metadata"] = identity
        else:
            payload.pop("metadata", None)
        payload["logs_url"] = f"{dashboard_url}/#/jobs/{d.submission_id}" if d.submission_id else None
        jobs.append(RayJob.model_validate(payload))
    # `total` + `truncated` so the cap is VISIBLE. A silently shortened list reads as "the cluster
    # has 200 jobs", which is a lie the UI would have no way to detect.
    return RayJobsPayload(
        ok=True,
        dashboard_url=dashboard_url,
        jobs=jobs,
        total=len(present),
        truncated=len(present) > len(jobs),
    )


def _parse_res_value(s: str) -> float:
    m = re.match(r"^([0-9.]+)([A-Za-z]*)$", s.strip())
    if not m:
        return 0.0
    return float(m.group(1)) * _BYTE_UNITS.get(m.group(2), 1) if m.group(2) else float(m.group(1))


def _parse_logical(text: str) -> dict[str, float]:
    used: dict[str, float] = {}
    for raw in (text or "").splitlines():
        # Placement-group reservations (e.g. Serve-LLM engines) append a suffix:
        #   "1.0/1.0 GPU (1.0 used of 1.0 reserved in placement groups)"
        # Strip it, or rpartition(" ") reads the name as "groups)" and the GPU
        # count is silently dropped (the viewer under-reported cluster GPU use).
        line = raw.strip().split(" (")[0]
        if "/" not in line or " " not in line:
            continue
        ratio, _, name = line.rpartition(" ")
        used_s, _, _total_s = ratio.partition("/")
        used[name] = _parse_res_value(used_s)
    return used


def _parse_gpu(g: dict) -> RayGpu:
    return RayGpu(
        index=g.get("index"),
        uuid=g.get("uuid"),
        name=g.get("name"),
        utilization_percent=g.get("utilizationGpu"),
        memory_used_mb=g.get("memoryUsed"),
        memory_total_mb=g.get("memoryTotal"),
        temperature_c=g.get("temperatureC"),
    )


def _assign_physical_gpus(nodes: list[RayNode]) -> None:
    """Trim each node's `gpus[]` so a physical GPU (by uuid) shows on only one node.

    Co-located KubeRay pods all enumerate every host GPU via nvidia-smi, so the
    same uuid appears on multiple rows. Greedily assign each uuid to the first node
    that can take it, capped to that node's logical GPU allocation. Mutates in place.
    """
    claimed: set[str] = set()
    for n in nodes:
        cap = int(n.resources_total.get("GPU", 0) or 0)
        kept: list[RayGpu] = []
        for g in n.gpus:
            if len(kept) >= cap:
                break
            if g.uuid and g.uuid in claimed:
                continue
            kept.append(g)
            if g.uuid:
                claimed.add(g.uuid)
        n.gpus = kept


async def cluster_status(http: httpx.AsyncClient, dashboard_url: str) -> RayClusterPayload:
    total = {"CPU": 0.0, "GPU": 0.0, "memory": 0.0}
    used = {"CPU": 0.0, "GPU": 0.0, "memory": 0.0}
    nodes: list[RayNode] = []

    try:
        cs_resp = await http.get(f"{dashboard_url}/api/cluster_status")
        cs_resp.raise_for_status()
        # `.get(key, {})` returns the *actual* value when the key exists — and
        # non-autoscaling clusters (e.g. the dev KubeRay) report clusterStatus:
        # null, which then crashed `.get` below. `or {}` coerces null -> {}.
        cs = (cs_resp.json().get(_RAY_KEY_DATA) or {}).get(_RAY_KEY_CLUSTER_STATUS) or {}
        usage = (cs.get(_RAY_KEY_LOAD_METRICS) or {}).get(_RAY_KEY_USAGE) or {}
        for k in total:
            pair = usage.get(k)
            if isinstance(pair, list) and len(pair) == 2:
                used[k] = float(pair[0] or 0)
                total[k] = float(pair[1] or 0)

        try:
            ns_resp = await http.get(f"{dashboard_url}/nodes?{_NODES_VIEW_PARAM}")
            ns_resp.raise_for_status()
            data = ns_resp.json().get(_RAY_KEY_DATA) or {}
            summary = data.get(_RAY_KEY_SUMMARY) or []
            logical = data.get(_RAY_KEY_NODE_LOGICAL_RESOURCES) or {}
            for n in summary:
                raylet = n.get(_RAY_KEY_RAYLET) or {}
                node_id = raylet.get(_RAY_KEY_NODE_ID)
                if not node_id:
                    continue
                parsed_used = _parse_logical(logical.get(node_id, ""))
                rtotal = raylet.get(_RAY_KEY_RESOURCES_TOTAL) or {}
                mem = n.get(_RAY_KEY_MEM) or []  # [total, available, percent, used]
                nodes.append(
                    RayNode(
                        node_id=node_id,
                        node_ip=raylet.get(_RAY_KEY_NODE_MANAGER_ADDRESS) or n.get(_RAY_KEY_IP),
                        hostname=n.get(_RAY_KEY_HOSTNAME),
                        node_type=raylet.get(_RAY_KEY_NODE_TYPE_NAME),
                        is_head=bool(raylet.get(_RAY_KEY_IS_HEAD)),
                        alive=raylet.get(_RAY_KEY_RAYLET_STATE) == _RAYLET_STATE_ALIVE,
                        resources_total={k: float(rtotal.get(k, 0) or 0) for k in total},
                        resources_used={k: parsed_used.get(k, 0.0) for k in total},
                        host_cpu_percent=n.get(_RAY_KEY_CPU),
                        host_mem_total=float(mem[0]) if len(mem) >= 1 else None,
                        host_mem_used=float(mem[3]) if len(mem) >= 4 else None,
                        gpus=[_parse_gpu(g) for g in (n.get(_RAY_KEY_GPUS) or [])],
                    )
                )
        except httpx.HTTPError:
            log.debug("per-node detail unavailable; aggregates still returned", exc_info=True)
    except httpx.HTTPError as exc:
        return RayClusterPayload(ok=False, dashboard_url=dashboard_url, error=f"{type(exc).__name__}: {exc!s}"[:_ERROR_MSG_MAX_LEN])

    # The autoscaler's loadMetricsReport is empty on non-autoscaling clusters
    # (e.g. the dev KubeRay) -> cluster totals stay 0. The per-node /nodes data
    # is authoritative, so sum it whenever loadMetrics gave us nothing.
    if nodes and not any(total.values()):
        for k in total:
            total[k] = sum(n.resources_total.get(k, 0.0) for n in nodes)
            used[k] = sum(n.resources_used.get(k, 0.0) for n in nodes)

    # KubeRay runs N worker pods per physical host, and each pod's nvidia-smi
    # enumerates ALL host GPUs — so co-located pods report the same physical GPUs
    # (same uuid), double-counting them across rows. Assign each physical GPU to
    # exactly one node, capped to that node's logical GPU allocation, so a GPU
    # never shows up twice. Greedy over uuid; nodes with no uuid telemetry pass through.
    _assign_physical_gpus(nodes)

    return RayClusterPayload(
        ok=True,
        dashboard_url=dashboard_url,
        node_count=len(nodes),
        alive_count=sum(1 for n in nodes if n.alive),
        total_resources=total,
        used_resources=used,
        nodes=nodes,
    )


def _int_or_none(v: object) -> int | None:
    if not isinstance(v, (int, float, str)):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _merge_actor(state: dict, logical: dict | None) -> RayActor:
    """Combine a state-API actor row with its /logical/actors counterpart (if any).

    State API owns namespace + structured death cause; /logical owns live process,
    GPU and task telemetry plus uptime timestamps. Keyed by actor_id upstream.
    """
    lg = logical or {}
    ps = lg.get("processStats") or {}
    mem = ps.get("memoryInfo") or {}
    addr = lg.get("address") or {}
    death_ctx = (state.get("death_cause") or {}).get("actor_died_error_context") or {}
    # /logical reports exitDetail "-" (and "" ) as the no-death placeholder for
    # live actors — treat both as "no reason" so alive actors don't show a death.
    reason = death_ctx.get("error_message") or lg.get("exitDetail") or None
    if reason in ("-", ""):
        reason = None
    if reason:
        reason = reason.split("\n")[0][:_ERROR_MSG_MAX_LEN]
    return RayActor(
        actor_id=state.get("actor_id"),
        class_name=state.get("class_name") or lg.get("className") or "",
        name=state.get("name") or lg.get("name"),
        repr_name=state.get("repr_name") or lg.get("reprName") or None,
        state=state.get("state") or lg.get("state") or "",
        pid=_int_or_none(state.get("pid")),
        node_id=state.get("node_id") or addr.get("nodeId"),
        job_id=state.get("job_id"),
        ray_namespace=state.get("ray_namespace"),
        num_restarts=_int_or_none(state.get("num_restarts")) or 0,
        is_detached=bool(state.get("is_detached")),
        placement_group_id=state.get("placement_group_id"),
        required_resources={k: float(v) for k, v in (state.get("required_resources") or {}).items()},
        death_reason=reason,
        start_time_ms=_int_or_none(lg.get("startTime")) or None,
        end_time_ms=_int_or_none(lg.get("endTime")) or None,
        cpu_percent=ps.get("cpuPercent"),
        rss_bytes=mem.get("rss"),
        num_fds=_int_or_none(ps.get("numFds")),
        gpu_util=ps.get("gpuUtilization"),
        gpu_mem_mb=ps.get("gpuMemoryUsage"),
        num_executed_tasks=_int_or_none(lg.get("numExecutedTasks")),
        num_running_tasks=_int_or_none(lg.get("numRunningTasks")),
        num_pending_tasks=_int_or_none(lg.get("numPendingTasks")),
        task_queue_length=_int_or_none(lg.get("taskQueueLength")),
        ip_address=addr.get("ipAddress"),
        worker_id=addr.get("workerId"),
    )


async def list_actors(http: httpx.AsyncClient, dashboard_url: str) -> RayActorsPayload:
    """Actors merged from the state API + /logical/actors. The latter is best-effort:
    if it's unreachable, actors still return with telemetry fields left null."""
    try:
        st_resp = await http.get(f"{dashboard_url}/api/v0/actors?detail=1&limit=1000")
        st_resp.raise_for_status()
        state_rows = (((st_resp.json().get("data") or {}).get("result") or {}).get("result")) or []
    except httpx.HTTPError as exc:
        return RayActorsPayload(ok=False, dashboard_url=dashboard_url, error=f"{type(exc).__name__}: {exc!s}"[:_ERROR_MSG_MAX_LEN])

    logical: dict[str, dict] = {}
    # Enrich ONLY the actors the state page returned: /logical/actors without `ids` dumps EVERY
    # actor's processStats/memoryInfo telemetry (no server cap exists on that route), all parsed
    # and then discarded beyond the <=MAX state rows (#140). `ids` is comma-separated per Ray's
    # node_head.py:703-704.
    actor_ids = ",".join(i for i in (row.get("actor_id") for row in state_rows) if i)
    try:
        lg_resp = await http.get(f"{dashboard_url}/logical/actors?{urlencode({'ids': actor_ids})}" if actor_ids else f"{dashboard_url}/logical/actors?ids=none")
        lg_resp.raise_for_status()
        logical = (lg_resp.json().get("data") or {}).get("actors") or {}
    except httpx.HTTPError:
        log.debug("logical actors unavailable; telemetry omitted", exc_info=True)

    actors = [_merge_actor(row, logical.get(row.get("actor_id"))) for row in state_rows]
    return RayActorsPayload(ok=True, dashboard_url=dashboard_url, actors=actors)


def _state_rows(payload: dict) -> list[dict]:
    return (((payload.get("data") or {}).get("result") or {}).get("result")) or []


async def list_tasks(http: httpx.AsyncClient, dashboard_url: str, job_id: str | None = None) -> RayTasksPayload:
    """Tasks from the state API (`/api/v0/tasks`), optionally filtered to ONE job SERVER-SIDE.

    The job-detail page used to pull the whole cluster's task table and filter client-side; the
    state API takes `filter_keys/filter_predicates/filter_values` (state_api_utils.py:66-71), so
    the narrowing now happens where the rows live (#140).
    """
    try:
        query = f"detail=1&limit={MAX_TASKS}"
        if job_id:
            query += "&" + urlencode({"filter_keys": "job_id", "filter_predicates": "=", "filter_values": job_id})
        resp = await http.get(f"{dashboard_url}/api/v0/tasks?{query}")
        resp.raise_for_status()
        rows = _state_rows(resp.json())
    except httpx.HTTPError as exc:
        return RayTasksPayload(ok=False, dashboard_url=dashboard_url, error=f"{type(exc).__name__}: {exc!s}"[:_ERROR_MSG_MAX_LEN])

    tasks = [
        RayTask(
            task_id=t.get("task_id"),
            name=t.get("name"),
            func_or_class_name=t.get("func_or_class_name"),
            type=t.get("type"),
            state=t.get("state") or "",
            job_id=t.get("job_id"),
            actor_id=t.get("actor_id"),
            node_id=t.get("node_id"),
            worker_pid=_int_or_none(t.get("worker_pid")),
            attempt_number=_int_or_none(t.get("attempt_number")),
            error_type=t.get("error_type"),
            error_message=(t.get("error_message") or "").split("\n")[0][:_ERROR_MSG_MAX_LEN] or None,
            required_resources={k: float(v) for k, v in (t.get("required_resources") or {}).items()},
            creation_time_ms=_int_or_none(t.get("creation_time_ms")),
            start_time_ms=_int_or_none(t.get("start_time_ms")),
            end_time_ms=_int_or_none(t.get("end_time_ms")),
        )
        for t in rows
    ]
    return RayTasksPayload(ok=True, dashboard_url=dashboard_url, tasks=tasks)


async def overview(http: httpx.AsyncClient, dashboard_url: str) -> RayOverviewPayload:
    """Cluster version/session + recent events feed (newest first)."""
    version: dict = {}
    try:
        v_resp = await http.get(f"{dashboard_url}/api/version")
        v_resp.raise_for_status()
        version = v_resp.json() or {}
    except httpx.HTTPError as exc:
        return RayOverviewPayload(ok=False, dashboard_url=dashboard_url, error=f"{type(exc).__name__}: {exc!s}"[:_ERROR_MSG_MAX_LEN])

    events: list[RayEvent] = []
    try:
        e_resp = await http.get(f"{dashboard_url}/api/v0/cluster_events")
        e_resp.raise_for_status()
        rows = _state_rows(e_resp.json())
        events = [
            RayEvent(
                event_id=e.get("event_id"),
                severity=e.get("severity") or "INFO",
                message=e.get("message") or "",
                time=e.get("time"),
                source_type=e.get("source_type"),
            )
            for e in rows
        ]
        events.sort(key=lambda e: e.time or "", reverse=True)
    except httpx.HTTPError:
        log.debug("cluster events unavailable", exc_info=True)

    return RayOverviewPayload(
        ok=True,
        dashboard_url=dashboard_url,
        ray_version=version.get("ray_version"),
        session_name=version.get("session_name"),
        events=events,
    )


async def job_logs(
    http: httpx.AsyncClient,
    client: JobSubmissionClient | None,
    dashboard_url: str,
    submission_id: str,
    tail: int = 2000,
) -> RayJobLogsPayload:
    """Driver logs for one submitted job — the last ``tail`` lines, BOUNDED SERVER-SIDE.

    The SDK's ``get_job_logs`` returns the ENTIRE driver log as one string, and Ray never rotates
    ``job-driver-<id>.log`` while ``log_to_driver=True`` funnels every worker's output into it — so
    the old client-side ``splitlines()[-tail:]`` held ~3-4x the whole log in memory, re-incurred on
    every 5 s poll of a RUNNING job, i.e. precisely while the log grows (#140). Ray's log-file API
    accepts ``lines=`` (the same endpoint :func:`logs` already uses), so the tail is now cut where
    the log lives: one job-info lookup resolves the driver node, one bounded read fetches the tail.
    """
    if client is None:
        return RayJobLogsPayload(ok=False, submission_id=submission_id, error="Ray dashboard unreachable")
    try:
        info = await to_thread.run_sync(client.get_job_info, submission_id)
    except Exception as exc:  # SDK raises plain RuntimeError on unknown ids
        return RayJobLogsPayload(ok=False, submission_id=submission_id, error=f"{type(exc).__name__}: {exc!s}"[:_ERROR_MSG_MAX_LEN])
    node_id = getattr(info, "driver_node_id", None)
    if not node_id:
        # PENDING jobs have no driver yet; an honest empty beats a whole-log fallback that would
        # reintroduce the defect for exactly the jobs an operator watches.
        return RayJobLogsPayload(ok=True, submission_id=submission_id, logs="(no driver log yet — the job has not started a driver)")
    qs = urlencode({"node_id": node_id, "filename": f"job-driver-{submission_id}.log", "lines": tail})
    try:
        resp = await http.get(f"{dashboard_url}/api/v0/logs/file?{qs}")
    except httpx.HTTPError as exc:
        return RayJobLogsPayload(ok=False, submission_id=submission_id, error=f"{type(exc).__name__}: {exc!s}"[:_ERROR_MSG_MAX_LEN])
    if resp.status_code >= HTTPStatus.BAD_REQUEST:
        # Ray's log endpoint 500s on empty files — same note logs() uses.
        return RayJobLogsPayload(ok=True, submission_id=submission_id, logs="(empty or unavailable)")
    return RayJobLogsPayload(ok=True, submission_id=submission_id, logs=resp.text.lstrip("\x00\x01"))


async def logs(
    http: httpx.AsyncClient,
    dashboard_url: str,
    node_id: str,
    filename: str | None = None,
    lines: int = 200,
) -> RayLogsPayload:
    """List a node's log files, or (with `filename`) return that file's tail."""
    try:
        if filename:
            qs = urlencode({"node_id": node_id, "filename": filename, "lines": lines})
            resp = await http.get(f"{dashboard_url}/api/v0/logs/file?{qs}")
            # Ray's log endpoint 500s on empty files — surface that as a clean note
            # rather than a stack-trace-y error.
            if resp.status_code >= HTTPStatus.BAD_REQUEST:
                return RayLogsPayload(ok=True, node_id=node_id, filename=filename, text="(empty or unavailable)")
            # Ray streams logs with a 1-byte success framing prefix on each chunk;
            # strip a leading non-printable so the viewer shows clean text.
            text = resp.text.lstrip("\x00\x01")
            return RayLogsPayload(ok=True, node_id=node_id, filename=filename, text=text)
        resp = await http.get(f"{dashboard_url}/api/v0/logs?node_id={node_id}")
        resp.raise_for_status()
        raw = (resp.json().get("data") or {}).get("result") or {}
        # Drop directory entries (e.g. "old/") — only real files are readable.
        files = {k: [f for f in v if isinstance(f, str) and not f.endswith("/")] for k, v in raw.items() if isinstance(v, list)}
        files = {k: v for k, v in files.items() if v}
        return RayLogsPayload(ok=True, node_id=node_id, files=files)
    except httpx.HTTPError as exc:
        return RayLogsPayload(ok=False, node_id=node_id, error=f"{type(exc).__name__}: {exc!s}"[:_ERROR_MSG_MAX_LEN])


async def proxy(
    http: httpx.AsyncClient,
    dashboard_url: str,
    path: str,
    method: str,
    query: bytes | str,
    headers: dict[str, str],
    body: bytes,
) -> ProxyResponse:
    """Forward an arbitrary path to the Ray Dashboard, same-origin to the browser.

    Credential-tight in both directions (`_REQUEST_STRIP` / `_RESPONSE_STRIP`):
    inbound browser auth/cookies never reach Ray (the httpx client's own default
    Bearer token authenticates server-side), and Ray's auth cookie never reaches
    the browser.
    """
    url = f"{dashboard_url}/{path.lstrip('/')}"
    fwd = {k: v for k, v in headers.items() if k.lower() not in _REQUEST_STRIP}
    qs = query.decode() if isinstance(query, bytes) else query
    try:
        resp = await http.request(method, url, params=qs, headers=fwd, content=body or None)
    except httpx.HTTPError as exc:
        return ProxyResponse(
            content=f"ray dashboard unreachable: {exc}".encode(),
            status_code=HTTPStatus.BAD_GATEWAY,
            headers={"content-type": "text/plain"},
        )
    return ProxyResponse(
        content=resp.content,
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items() if k.lower() not in _RESPONSE_STRIP},
    )
