"""What a submitter SENDS and what the Ray job READS are one contract, declared in two files.

Nothing tied them together, and they disagree today. Measured 2026-09-07:

    WorkOrder.to_env() supplies   RASK_SOURCE_URI, RASK_DEST_URI, RASK_STAGE, RASK_CARDINALITY, ...
    scripts/ray_stage_job.py reads FROM_URI, TO_URI, STAGE, STAGE_CARDINALITY, BASE_VERSION, LINEAGE_JSON
    overlap                        0 of 6

THIS IS WHY THE PORT'S RAY ADAPTER WAS NEVER WIRED, and it is a better explanation than the one the
goal file carries. `RayJobExecutor` renders `order.to_env()` into the RayJob's `runtime_env`, so a job
submitted through the port would start with none of its inputs bound and read every one of them as
absent — an empty `FROM_URI` is not a crash, it is a job that scans nothing and writes nothing. The
adapter is not merely unused; against these scripts it could not have worked, which no test said.

`test_ray_job_wire_parity.py` is the neighbouring gate and does NOT cover this: it compares the
Python and TypeScript declarations of the `RayJob` **schema** — what the jobs board parses — not the
env contract between a submitter and the program it submits.

THE DIRECTION OF THE FIX IS THE PLATFORM'S VOCABULARY, not a translation in the adapter.
`WorkOrder.to_env` is documented as "the ONE serialization, so no adapter hand-rolls it"; an adapter
that renamed six fields on the way out would restore exactly the second vocabulary the port exists to
remove, and the next engine's adapter would need its own copy of the same table.
"""

from __future__ import annotations

import pathlib
import re


REPO = pathlib.Path(__file__).resolve().parents[2]

#: Every program a STAGE submission feeds. `ray_train_job.py` and `ray_lance_job.py` are absent
#: deliberately — they answer different submitters with different contracts (TRAIN_*, RUN), and
#: folding them in would assert an agreement that was never claimed.
#:
#: THE SEALED RUNNER IS HERE ON PURPOSE, and the boundary is worth stating because it looks like a
#: violation. A runner owns its stage graph, its models and its output format, and the platform
#: describes none of those. It does NOT own the env contract by which the platform hands it a unit of
#: work — that contract has two sides and the platform writes one of them, so a runner reading a name
#: no order supplies is a platform defect that happens to surface in a runner.
JOB_SCRIPTS = (
    "scripts/ray_stage_job.py",
    "scripts/ray_dummy_job.py",
    "runners/dummy/src/dummy_runner/job.py",
)

#: Names the job legitimately reads that no `WorkOrder` supplies — the pod's own environment rather
#: than this run's. Listed so the gate compares the ORDER's half and nothing else.
NOT_FROM_THE_ORDER = frozenset({
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_SERVICE_NAME",
    "OTEL_RESOURCE_ATTRIBUTES",
    "TRACEPARENT",
    "TRACESTATE",
    "S3_ENDPOINT",
    "S3_REGION",
    "S3_KEY_ID",
    "S3_KEY",
    "S3_SECRET",
    "RAY_ADDRESS",
    "RASK_STAGE_MEDIA_BATCH_ROWS",
})

_READ = re.compile(r'(?:os\.environ|environ|e)\s*(?:\.get\(\s*|\[\s*)"([A-Z][A-Z0-9_]*)"')


def _names_the_job_reads(path: pathlib.Path) -> set[str]:
    return set(_READ.findall(path.read_text(encoding="utf-8"))) - NOT_FROM_THE_ORDER


def _names_the_order_supplies() -> set[str]:
    from service_kit.lakehouse.work_order import WorkDestination, WorkIdentity, WorkOrder, WorkSource, WorkStamp

    # EVERY OPTIONAL FIELD SET. `to_env` omits an absent floor rather than blanking it, so a probe
    # order built with defaults understates what an order CAN supply — and the gate would then report
    # a legitimately-optional name as a mismatch. The question is "can an order ever bind this?", not
    # "does this particular one".
    order = WorkOrder(
        task="t",
        source=WorkSource(uri="s3://a", table_id="ns$a", version_floor=7),
        destination=WorkDestination(uri="s3://b", table_id="ns$b"),
        stamp=WorkStamp(stage="silver", cardinality="1:1", lineage_document="{}"),
        identity=WorkIdentity(run_id="r", project="p", originator="user:someone", code_version="c"),
        idempotency_key="k",
    )
    return set(order.to_env())


def test_the_gate_can_see_both_halves() -> None:
    """A regex matching nothing, or a moved script, would make the assertion below vacuous."""
    assert _names_the_order_supplies(), "WorkOrder.to_env() emitted nothing — the gate is blind"
    for rel in JOB_SCRIPTS:
        path = REPO / rel
        assert path.exists(), f"{rel} has moved — this gate now checks nothing"
        assert _names_the_job_reads(path), f"{rel}: the reader regex matched no env names"


def test_every_name_the_job_reads_is_one_the_ORDER_supplies() -> None:
    """THE GATE. A job reading a name no submitter sends binds it to the empty string, and an empty
    source URI is not a crash — it is a run that scans nothing, writes nothing and reports success."""
    supplies = _names_the_order_supplies()
    for rel in JOB_SCRIPTS:
        missing = sorted(_names_the_job_reads(REPO / rel) - supplies)
        assert not missing, (
            f"{rel} reads {missing}, which no WorkOrder supplies — a job submitted through "
            f"`service_kit.lakehouse.executor` starts with those unbound. The order supplies "
            f"{sorted(supplies)}."
        )
