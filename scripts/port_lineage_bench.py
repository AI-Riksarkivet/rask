#!/usr/bin/env python
"""Measure the lineage pilot (ADR 0217 point 8, ADR 0238): the Rust binary against the Python service, on one machine,
on the same collaborators, from the same environment.

    KEEP_UP=1 bash scripts/port_lineage_check.sh          # starts the collaborators and prints <work>
    (cd rust && cargo build --release --locked -p lineage --bin lineage)
    source <work>/env && BENCH_RUST_BIN=rust/target/release/lineage \\
        uv run --no-sync python scripts/port_lineage_bench.py <work> --runs 3

Each run starts each service as the chart runs it (the Python under ``opentelemetry-instrument uvicorn``, the Rust as
``lineage serve``, both exporting to the fixture's OTLP sink with the pod's token) and measures: the seconds from the
process's start to ``/readyz`` answering 200; the resident memory once idle after the start and again after the load;
``GET /events`` (a page of 100 off a feed of ``--feed`` rows) and ``POST /api/v1/lineage`` (one run each, written by an
admin) at concurrency 1, 8 and 32, as latency percentiles and requests per second; and the ingest lane's throughput over
1,000 signed events published on the bus. Each run of each service starts from a namespace of its own holding the same
seeded feed, and from an empty stream. The machine's load average is read before and after each run. It prints a JSON
document and a table, each figure the median of the runs with their spread (min to max).

What the numbers do not show is stated with them: every door reads or writes through the Python catalog fixture, one
uvicorn process on real pylance over a local directory, which both services wait on alike, and OpenFGA on its
in-memory store, so a latency is the service's own work plus the same collaborators' and the difference between the two
is the part either service adds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parent))

import port_lineage_fixture as fixture


CONCURRENCY = (1, 8, 32)


def _resident_kib(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def _load() -> float:
    return float(Path("/proc/loadavg").read_text().split()[0])


async def _timed(requests: int, concurrency: int, send: Any) -> dict[str, float]:  # noqa: ANN401 - a coroutine function
    latencies: list[float] = []
    statuses: dict[int, int] = {}
    queue = list(range(requests))

    async def worker() -> None:
        while queue:
            index = queue.pop()
            began = time.perf_counter()
            status = await send(index)
            latencies.append(time.perf_counter() - began)
            statuses[status] = statuses.get(status, 0) + 1

    began = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    took = time.perf_counter() - began
    cut = statistics.quantiles(latencies, n=100)
    return {
        "p50_ms": cut[49] * 1000,
        "p95_ms": cut[94] * 1000,
        "p99_ms": cut[98] * 1000,
        "per_second": requests / took,
        "ok": sum(count for status, count in statuses.items() if status < 300) / requests,
    }


async def _doors(url: str, requests: int) -> dict[str, Any]:
    import httpx

    measured: dict[str, Any] = {}
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=64)
    async with httpx.AsyncClient(base_url=url, timeout=120, limits=limits) as client:
        # A token lives ten minutes, so each measurement is made with one asked for as it starts.
        admin: dict[str, str] = {}

        def fresh() -> None:
            admin["authorization"] = f"Bearer {httpx.get(f'{os.environ["CHECK_ISSUER"]}/token', params={'sub': 'admin'}, timeout=10).text}"

        async def read(_: int) -> int:
            return (await client.get("/events?limit=100", headers=admin)).status_code

        tag = uuid.uuid4().hex[:6]

        async def write(index: int) -> int:
            event = fixture._run(f"bench-{tag}-{index}", ["b"], ["a"])
            return (await client.post("/api/v1/lineage", json=event, headers=admin)).status_code

        for concurrency in CONCURRENCY:
            fresh()
            await read(0)
            measured[f"GET /events c{concurrency}"] = await _timed(requests, concurrency, read)
        for concurrency in CONCURRENCY:
            fresh()
            tag = uuid.uuid4().hex[:6]
            measured[f"POST /api/v1/lineage c{concurrency}"] = await _timed(requests, concurrency, write)
    return measured


def _lane(estate: Any, events: int) -> float:  # noqa: ANN401 - the fixture's Estate
    """Events recorded per second from the bus: ``events`` signed events published at once, timed until the consumer
    holds none of them."""
    tag = uuid.uuid4().hex[:6]
    envelopes = [
        fixture.doors._envelope(fixture.signed(fixture._run(f"lane-{tag}-{n}", ["b"], ["a"]), on_behalf_of="idp~admin")).as_wire() for n in range(events)
    ]

    async def publish() -> None:
        import nats

        client = await nats.connect(os.environ["CHECK_NATS_URL"], token=os.environ["CHECK_NATS_TOKEN"])
        stream = client.jetstream()
        await asyncio.gather(*(stream.publish(fixture.TOPIC, json.dumps(e).encode(), headers={"Nats-Msg-Id": e["id"]}) for e in envelopes))
        await client.close()

    began = time.perf_counter()
    asyncio.run(publish())
    fixture.eventually(lambda: estate.unsettled() == 0, 600, 0.2)
    return events / (time.perf_counter() - began)


def _seed(estate: Any, provenance: str, rows: int) -> None:  # noqa: ANN401 - the fixture's Estate
    """``rows`` events in the feed, recorded by the Python store in group commits."""
    from lineage.services.provenance import ProvenanceStore

    async def record() -> None:
        store = ProvenanceStore(catalog_url=os.environ["CHECK_CATALOG_URL"], namespace=provenance.split("$"), token=lambda: None, refuse_foreign_runs=True)
        await store.ensure()
        tag = uuid.uuid4().hex[:6]
        from lineage.models import parse_event

        for start in range(0, rows, 250):
            batch = [parse_event(fixture._run(f"seed-{tag}-{n}", ["b"], ["a"])) for n in range(start, min(rows, start + 250))]
            await asyncio.gather(*(store.record(event, aliased=frozenset()) for event in batch))

    asyncio.run(record())


def _one(estate: Any, kind: str, provenance: str, requests: int, events: int) -> dict[str, Any]:  # noqa: ANN401 - the fixture's Estate
    env = estate.environment(provenance)
    load_before = _load()
    began = time.perf_counter()
    service = fixture.Service(kind, fixture._port(), env, estate.work)
    if not service.ready(300):
        raise SystemExit(f"{kind} did not start: {service.said()[-800:]}")
    measured: dict[str, Any] = {"start_to_ready_s": time.perf_counter() - began}
    time.sleep(5)
    measured["rss_idle_mib"] = _resident_kib(service.process.pid) / 1024
    measured.update(asyncio.run(_doors(service.url, requests)))
    measured["lane events/s"] = _lane(estate, events)
    time.sleep(3)
    measured["rss_after_load_mib"] = _resident_kib(service.process.pid) / 1024
    service.stop()
    measured["load_before"], measured["load_after"] = load_before, _load()
    return measured


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("work", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--feed", type=int, default=3000, help="rows in the feed GET /events pages")
    parser.add_argument("--requests", type=int, default=100, help="requests per door and concurrency")
    parser.add_argument("--events", type=int, default=1000, help="events published for the lane")
    args = parser.parse_args()
    if os.environ.get("BENCH_RUST_BIN"):
        os.environ["CHECK_RUST_BIN"] = os.environ["BENCH_RUST_BIN"]
    estate = fixture.Estate(args.work)
    runs: dict[str, list[dict[str, Any]]] = {"python": [], "rust": []}
    for run in range(args.runs):
        for kind in ("python", "rust") if run % 2 == 0 else ("rust", "python"):
            # Each run of each service starts from the same tables (a fresh namespace holding the seeded feed) and an empty
            # stream, so neither measures the versions or the deliveries the other left.
            provenance = estate.namespace("bench")
            _seed(estate, provenance, args.feed)
            asyncio.run(estate._declare_the_stream())
            runs[kind].append(_one(estate, kind, provenance, args.requests, args.events))
            print(f"run {run + 1} {kind}: {json.dumps(runs[kind][-1])}", flush=True)
    summary: dict[str, Any] = {}
    for kind, measured in runs.items():
        for name in measured[0]:
            values = measured[0][name]
            if isinstance(values, dict):
                for field in values:
                    series = [m[name][field] for m in measured]
                    summary.setdefault(f"{name} {field}", {})[kind] = (statistics.median(series), min(series), max(series))
            else:
                series = [m[name] for m in measured]
                summary.setdefault(name, {})[kind] = (statistics.median(series), min(series), max(series))
    print(json.dumps({"runs": runs, "summary": summary}, indent=1))
    print(f"{'measure':44} {'python median (min-max)':>30} {'rust median (min-max)':>30}")
    for name, by in summary.items():
        cell = {kind: f"{m:.2f} ({lo:.2f}-{hi:.2f})" for kind, (m, lo, hi) in by.items()}
        print(f"{name:44} {cell['python']:>30} {cell['rust']:>30}")


if __name__ == "__main__":
    main()
