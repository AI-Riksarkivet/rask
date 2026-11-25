"""End-to-end observability test against the deployed kind stack (catalog + the bus + lineage + GreptimeDB).

The regression guard for the thing that must never silently break while refactoring: the event-driven
pipeline AND its observability. ONE catalog table-create drives the real path, and we assert it produced
the four things that make this system trustworthy — exercising the *deployed* services and the *real*
GreptimeDB store, not mocks:

  1. Durable provenance                    — the create's event landed on lineage's feed (the pipeline actually works)
  2. The custom domain metric in GreptimeDB — lineage_events_processed_total by lance_lineage_outcome went UP
  3. A distributed trace in GreptimeDB      — ONE trace spans catalog → its publish on the bus → lineage → its feed write
  4. Logs in GreptimeDB                      — apps (OTLP) + infra (the gateway's filelog) → opentelemetry_logs

Run (port-forward the three services first), or `make e2e-obs`:

    kubectl port-forward svc/rask-catalog                      2333:2333 &
    kubectl port-forward svc/rask-lineage                      8000:8000 &
    kubectl port-forward -n observability svc/greptimedb      4000:4000 &
    LANCE_E2E_CATALOG_URL=http://localhost:2333 \
    LANCE_E2E_LINEAGE_URL=http://localhost:8000 \
    LANCE_E2E_GREPTIME_URL=http://localhost:4000 \
    uv run pytest tests/e2e-py/test_observability_e2e.py -v
"""

import os
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pyarrow as pa
import pytest
import requests
from credentials import Credential, credential
from liveness import skip_unless_live
from principals import DEX_CLIENT, DEX_SCOPE
from topology import namespace_id


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "")
LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "")
GREPTIME = os.environ.get("LANCE_E2E_GREPTIME_URL", "")
DEX = os.environ.get("LANCE_E2E_DEX", "http://localhost:5556/dex")
DEX_SECRET = credential(Credential.DEX_SECRET, "lance-catalog-secret")
ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
#: The topic the catalog announces each commit on and lineage takes it from (`pubsub.topic`).
LINEAGE_TOPIC = "lineage.events.v1"

pytestmark = [pytest.mark.e2e, pytest.mark.observability]


def _auth_headers() -> dict[str, str]:
    """Bearer headers when the deployed stack is GOVERNED, ``{}`` when it is open.

    This suite long predates auth-on stacks and drove the catalog unauthenticated; once the kind deploy
    runs with ``auth.enabled=true`` every setup call 401'd and all four signals went unverifiable. Probe
    the catalog: open stack → stay unauthenticated (docker-compose / CI e2e keeps working with no Dex
    forward); 401 → password-grant a Dex user exactly like test_governance_e2e (skip if Dex unreachable).
    """
    try:
        probe = requests.get(f"{CATALOG}/v1/namespace/$/list", timeout=5)
    except Exception:
        return {}
    if probe.status_code != 401:
        return {}
    data = {
        "grant_type": "password",
        "client_id": DEX_CLIENT,
        "username": "alice@example.com",
        "password": "password",
        "scope": DEX_SCOPE,
    }
    if DEX_SECRET:
        data["client_secret"] = DEX_SECRET
    try:
        body = requests.post(f"{DEX}/token", data=data, timeout=10).json()
    except Exception:
        pytest.skip(f"stack is governed (catalog 401s) but Dex is not reachable at {DEX}")
    if "id_token" not in body:
        pytest.skip(f"stack is governed but the Dex password grant failed: {body}")
    return {"Authorization": f"Bearer {body['id_token']}"}


def _eventually(fn: Callable[[], Any], *, timeout: float = 60.0, interval: float = 3.0) -> Any:
    """Poll ``fn`` until it returns a truthy value (the signal arrived), else fail with the last result.
    Signals are asynchronous here — a delivery handed back, the 5s metric export interval, trace batching."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = fn()
            if last:
                return last
        except Exception as exc:
            last = exc
        time.sleep(interval)
    pytest.fail(f"condition not met within {timeout}s (last={last!r})")


#: The window every log assertion asks about. A time-series table answers a bounded question in
#: milliseconds and an unbounded one not at all once it is large enough — measured 2026-09-24, the
#: same count is 18 ms bounded and HTTP 500 unbounded at 180 million rows. Fifteen minutes is far
#: longer than the poll budget below, so a slow collector still passes; a dead one does not.
_RECENT = "timestamp > now() - INTERVAL '15 minutes'"

#: The positive form. Its De Morgan twin and `NOT (...)` both fail the engine outright on an indexed
#: `trace_id` — measured 2026-09-14 — which is why the infra half is a subtraction rather than a
#: predicate.
_HAS_TRACE = "trace_id IS NOT NULL AND trace_id != ''"


def _gt_sql(query: str) -> list[list[Any]]:
    """Run SQL against GreptimeDB and return the result rows."""
    r = requests.get(f"{GREPTIME}/v1/sql", params={"db": "public", "sql": query}, timeout=10)
    r.raise_for_status()
    return r.json()["output"][0]["records"]["rows"]


def _gt_promql_sum(query: str) -> float:
    """Sum of a PromQL instant query's series values via GreptimeDB's Prometheus-compatible API."""
    r = requests.get(f"{GREPTIME}/v1/prometheus/api/v1/query", params={"query": query}, timeout=10)
    r.raise_for_status()
    return sum(float(s["value"][1]) for s in r.json()["data"]["result"])


def _create_table(table_id: str, namespace: str, auth: dict[str, str], trace_id: str) -> None:
    """Create a namespace + table via the catalog — the action the catalog announces on the bus — the table's create
    carried in the trace `trace_id` names (a W3C `traceparent`, which the services' instrumentation continues)."""
    requests.post(f"{CATALOG}/v1/table/{table_id}/drop", headers=auth, timeout=30)
    requests.post(f"{CATALOG}/v1/namespace/{namespace}/drop", headers=auth, timeout=30)
    created = requests.post(f"{CATALOG}/v1/namespace/{namespace}/create", json={}, headers=auth, timeout=30)
    assert created.status_code == 200, created.text
    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, rows.schema) as writer:
        writer.write_table(rows)
    resp = requests.post(
        f"{CATALOG}/v1/table/{table_id}/create?mode=overwrite",
        data=sink.getvalue().to_pybytes(),
        headers=ARROW | auth | {"traceparent": f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01"},
        timeout=60,
    )
    assert resp.status_code == 200, resp.text


@pytest.fixture(scope="module")
def pipeline_run() -> Iterator[dict[str, Any]]:
    """Drive the real pipeline once: baseline the metric, create a table in a trace of the run's own, yield the run
    context, then drop the namespace with its table. Skips cleanly unless all three services are reachable (the
    LANCE_E2E_*_URL env vars)."""
    if not (CATALOG and LINEAGE and GREPTIME):
        pytest.skip("set LANCE_E2E_CATALOG_URL / _LINEAGE_URL / _GREPTIME_URL (see module docstring)")
    probes = (
        ("catalog", CATALOG, "/livez"),
        ("lineage", LINEAGE, "/livez"),
        ("greptime", GREPTIME, "/health"),
    )
    for name, url, probe in probes:
        skip_unless_live(f"{url.rstrip('/')}{probe}", what=name)

    auth = _auth_headers()
    token = uuid.uuid4().hex[:8]
    namespace = namespace_id(f"obsns{token}")
    table_id = f"{namespace}$obsns{token}tbl"  # catalog id == lineage Dataset
    metric_query = 'sum(lineage_events_processed_total{lance_lineage_outcome="ingested"})'
    before = _gt_promql_sum(metric_query)

    trace_id = uuid.uuid4().hex
    _create_table(table_id, namespace, auth, trace_id)
    yield {"table_id": table_id, "metric_query": metric_query, "ingested_before": before, "auth": auth, "trace_id": trace_id}
    dropped = requests.post(f"{CATALOG}/v1/namespace/{namespace}/drop", json={"behavior": "Cascade"}, headers=auth, timeout=60)
    assert dropped.status_code == 200, dropped.text[-400:]


def test_custom_metric_incremented(pipeline_run: dict[str, Any]) -> None:
    """2. The custom domain metric went UP in GreptimeDB/PromQL — metrics are queryable + valuable."""
    before = pipeline_run["ingested_before"]
    query = pipeline_run["metric_query"]

    def metric_increased() -> float | None:
        after = _gt_promql_sum(query)
        return after if after > before else None

    assert _eventually(metric_increased) > before


def test_distributed_trace_spans_catalog_to_lineage(pipeline_run: dict[str, Any]) -> None:
    """3. The create's own trace spans catalog → its publish on the bus → lineage → its feed write — true distributed tracing.

    The publish is the catalog's own span (`lakehouse.events.bus`, named `publish <topic>`), and the envelope carries its
    `traceparent` to lineage, whose delivery of it is a CONSUMER span `process <topic>` in the same trace. The span is
    read with its service: the maintenance worker's commit lane names its own delivery of the same publish alike.

    Read off the trace the create's `traceparent` names, never the newest trace that spans both services: lineage
    writes its feed through the catalog's doors, so each relay tick spans catalog and lineage with no publish in it.

    The wait is for the two spans themselves. A span is exported when it ends, and lineage's delivery span ends after
    every call its handling makes, so one of those calls is in the trace a batch before the delivery that encloses it.
    """
    query = f"SELECT DISTINCT service_name, span_name FROM opentelemetry_traces WHERE trace_id = '{pipeline_run['trace_id']}' AND {_RECENT}"
    joined = {("catalog", f"publish {LINEAGE_TOPIC}"), ("lineage", f"process {LINEAGE_TOPIC}")}

    def joined_trace() -> set[tuple[str, str]]:
        named = {(service, name) for service, name in _gt_sql(query)}
        assert joined <= named, f"the trace lacks {sorted(joined - named)}; it holds {sorted(named)}"
        return named

    _eventually(joined_trace)


@pytest.mark.usefixtures("pipeline_run")
def test_logs_populated() -> None:
    """4. Both log paths land in GreptimeDB's `opentelemetry_logs` — OTel-first, one table:
    the apps export OTLP logs (trace-correlated) as themselves to the gateway, and the gateway's filelog receiver
    (the observability fixture's Collector) tails the no-SDK infra pods into the SAME table."""

    def app_and_infra_logs_present() -> bool | None:
        # opentelemetry_logs has no top-level service_name column (service.name lives in resource_attributes);
        # distinguish the two paths by trace_id instead. App OTLP logs are emitted inside a span → carry a
        # trace_id; the gateway's file-tailed infra logs have no span context → no trace_id. Both present in
        # the ONE table proves both paths landed (Vector is gone — its lance_logs table no longer exists).
        #
        # THE INFRA HALF IS A SUBTRACTION, because GreptimeDB cannot answer it directly. `trace_id` is
        # indexed, and any predicate that must MATCH NULLs on it fails the query engine outright —
        # measured live 2026-09-14 against `opentelemetry_logs` (101,385,870 rows): both
        # `NOT (trace_id IS NOT NULL AND trace_id != '')` and its De Morgan twin
        # `trace_id IS NULL OR trace_id = ''` answer HTTP 500, while the positive form returns in 75 ms.
        # So this leg reported "logs not populated" about a table holding a hundred million rows. Total
        # minus the trace-carrying count is the same number and both halves are supported queries.
        # BOUNDED BY TIME, and the same window on BOTH halves. The subtraction below is only
        # meaningful if `app` and `total` cover the same rows, so the window is one constant applied
        # to each rather than a filter on one of them.
        #
        # The note above fixed the NULL-matching predicate and left the query unbounded, which held
        # while the table did. It no longer does: measured live 2026-09-24 the positive form ALSO
        # answers HTTP 500 —
        #   Exceeded memory limit: 352.0KiB requested, 1024.0MiB used globally (99%), hard limit 1.0GiB
        # — against 180,141,239 rows, up from the 101,385,870 that returned in 75 ms ten days earlier.
        # The table is not broken and retention is working (`ttl = '14days'` on the table itself); the
        # QUESTION was wrong. Bounded to this window the same count answers in 18 ms over 130,961 rows.
        #
        # It is also the question this leg actually asks. "Logs are populated" means arriving NOW, not
        # that the table has ever held a row — an estate whose collector died an hour ago would pass
        # the unbounded form for another fourteen days.
        app = int(_gt_sql(f"SELECT count(*) FROM opentelemetry_logs WHERE {_RECENT} AND {_HAS_TRACE}")[0][0])
        total = int(_gt_sql(f"SELECT count(*) FROM opentelemetry_logs WHERE {_RECENT}")[0][0])
        infra = total - app
        return (app > 0 and infra > 0) or None

    assert _eventually(app_and_infra_logs_present)

    # Log↔trace CORRELATION, not just presence (otel signals.md: "Every log emitted inside an
    # active span carries trace_id"): if the logging auto-instrumentation env flag were dropped,
    # the table would still fill and the suite would pass while correlation silently died.
    def correlated_log_exists() -> bool | None:
        rows = _gt_sql(f"SELECT count(*) FROM opentelemetry_logs WHERE {_RECENT} AND {_HAS_TRACE}")
        return int(rows[0][0]) > 0 or None

    assert _eventually(correlated_log_exists)
