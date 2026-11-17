#!/usr/bin/env python
"""Show the Python and the Rust bus working one stream together, as they will while the estate runs both (ADR 0225).

Starts the pinned nats-server (``scripts/nats_server.sh``) on a free port with a token, declares ``LINEAGE`` and its
durable consumer ``lineage-ingest`` as the chart's stream Job does, then: Python's ``publish_event`` publishes two
events and the Rust ``Bus`` (``cargo run --example bus_interop``) takes them; the Rust bus publishes two and Python's
``Bus.consume`` takes them. Each side connects with the token read from a file, as a pod does. Exits non-zero unless
every event arrives on the other side under the id it was published with.

Run: ``uv run --no-sync python scripts/bus_interop.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[1]
_TOPIC = "lineage.events.v1"
_ACK_WAIT_SECONDS = 30


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _declare(url: str, token: str) -> None:
    """The stream and the consumer, as `chart/templates/nats-stream-job.yaml` and `lance.busConsumers` declare them."""
    import nats

    admin = await nats.connect(url, token=token)
    stream = {
        "name": "LINEAGE",
        "subjects": ["lineage.events.>"],
        "retention": "limits",
        "storage": "file",
        "discard": "old",
        "num_replicas": 1,
        "duplicate_window": 120_000_000_000,
        "allow_direct": True,
    }
    consumer = {
        "durable_name": "lineage-ingest",
        "ack_policy": "explicit",
        "ack_wait": _ACK_WAIT_SECONDS * 1_000_000_000,
        "max_deliver": -1,
        "filter_subject": _TOPIC,
        "deliver_policy": "all",
        "replay_policy": "instant",
        "max_ack_pending": 4,
        "max_waiting": 512,
    }
    for subject, body in (
        ("$JS.API.STREAM.CREATE.LINEAGE", stream),
        ("$JS.API.CONSUMER.CREATE.LINEAGE.lineage-ingest", {"stream_name": "LINEAGE", "config": consumer, "action": "create"}),
    ):
        answer = json.loads((await admin.request(subject, json.dumps(body).encode(), timeout=5)).data)
        if "error" in answer:
            raise SystemExit(f"{subject}: {answer['error']}")
    await admin.close()


def _event(run: str) -> dict[str, Any]:
    return {"eventType": "COMPLETE", "eventTime": "2026-11-17T10:00:00Z", "run": {"runId": run}, "producer": "rask/python"}


def _rust(environment: dict[str, str], *arguments: str) -> list[str]:
    command = ["cargo", "run", "-q", "--manifest-path", str(_ROOT / "rust" / "Cargo.toml"), "-p", "lakehouse-bus", "--example", "bus_interop", "--", *arguments]
    done = subprocess.run(command, env=os.environ | environment, capture_output=True, text=True, timeout=600, check=False)  # noqa: S603 - our own example
    if done.returncode != 0:
        raise SystemExit(f"the Rust bus failed: {done.stderr.strip()}")
    return done.stdout.split()


async def _python_side(environment: dict[str, str], token_file: Path) -> None:
    from lakehouse.bus import Bus, BusSettings, publish_event
    from lakehouse.bus_delivery import Delivery, Lane, Verdict
    from lakehouse.cloud_events import lineage_envelope

    settings = BusSettings.model_validate({"RASK_BUS_URL": environment["RASK_BUS_URL"], "RASK_BUS_USER": "lineage", "RASK_BUS_TOKEN_FILE": str(token_file)})
    bus = Bus(settings)
    keeping = asyncio.create_task(bus.keep_connected())
    while not bus.connected:
        await asyncio.sleep(0.05)

    sent = [lineage_envelope(_event(run)) for run in ("python-1", "python-2")]
    for envelope in sent:
        await publish_event(bus, envelope, topic=_TOPIC, timeout_seconds=5)
    taken_by_rust = await asyncio.to_thread(_rust, environment, "consume", "2")
    print(f"Python published {[envelope.id for envelope in sent]}; Rust took {taken_by_rust}")
    if taken_by_rust != [envelope.id for envelope in sent]:
        raise SystemExit("the Rust bus did not take what Python published")

    published_by_rust = await asyncio.to_thread(_rust, environment, "publish", "rust-1", "rust-2")
    taken: list[str] = []
    arrived = asyncio.Event()

    async def handle(delivery: Delivery) -> Verdict:
        envelope = delivery.envelope() or {}
        taken.append(str(envelope.get("id")))
        if len(taken) == len(published_by_rust):
            arrived.set()
        return Verdict.TAKEN

    async def park(_: Delivery, reason: str) -> None:
        raise RuntimeError(f"nothing is parked here: {reason}")

    lane = Lane(stream="LINEAGE", consumer="lineage-ingest", attempts=3, retry_seconds=1, ack_wait_seconds=_ACK_WAIT_SECONDS, in_flight=4)
    consuming = asyncio.create_task(bus.consume(lane, handle, park=park))
    await asyncio.wait_for(arrived.wait(), timeout=30)
    consuming.cancel()
    await asyncio.gather(consuming, return_exceptions=True)
    print(f"Rust published {published_by_rust}; Python took {taken}")
    keeping.cancel()
    await asyncio.gather(keeping, return_exceptions=True)
    await bus.close()
    if taken != published_by_rust:
        raise SystemExit("the Python bus did not take what Rust published")


def main() -> None:
    server = subprocess.run(["bash", str(_ROOT / "scripts" / "nats_server.sh")], capture_output=True, text=True, check=True).stdout.strip()  # noqa: S603, S607
    port, token = _free_port(), "interop-token"
    with tempfile.TemporaryDirectory() as work:
        token_file = Path(work) / "token"
        token_file.write_text(token)
        process = subprocess.Popen(  # noqa: S603 - the pinned release
            [server, "-js", "-a", "127.0.0.1", "-p", str(port), "--auth", token, "-sd", str(Path(work) / "store")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            url = f"nats://127.0.0.1:{port}"
            for _ in range(500):
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", port)) == 0:
                        break
                asyncio.run(asyncio.sleep(0.02))
            asyncio.run(_declare(url, token))
            environment = {"RASK_BUS_URL": url, "RASK_BUS_USER": "lineage", "RASK_BUS_TOKEN_FILE": str(token_file)}
            asyncio.run(_python_side(environment, token_file))
        finally:
            process.terminate()
            process.wait()
    print("the Python and the Rust bus interoperate on one stream")


if __name__ == "__main__":
    sys.path.insert(0, str(_ROOT / "services"))
    main()
