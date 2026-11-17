#!/usr/bin/env python
"""The Python half of lineage's lanes check (ADR 0235): the real producer, the real Python lane, and a read of the rows.

Run by ``rust/crates/lineage/tests/lanes_live.rs`` (through ``scripts/port_lanes_check.sh``), one command a call, its
arguments a JSON file, its answer one JSON line on stdout:

``produce``
    The real ``lakehouse.outbox.publish_with_outbox``: stage the envelope in the outbox under the producer's own store
    session (its projected token exchanged at the store's STS), publish it on the bus, drop the staged copy. With no
    ``bus_url`` the bus is away: the publish fails and the event stays staged. Answers ``{"left_staged": bool}``.
``lane``
    The real Python lane's handling (``lineage.services.consumer.handle_cloud_event``) of one envelope, recording into
    ``namespace`` of the catalog. Answers ``{"verdict": ...}``.
``rows``
    Every row of the feed, the edges and the run bindings of ``namespace``, in row-id order, the stamp of receipt left
    out (each store stamps its own clock), as ``scripts/port_store_cross.py`` reads them.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[1]


async def _produce(spec: dict[str, Any]) -> dict[str, Any]:
    from port_outbox_fixture import PRODUCER, _projected

    from lakehouse.bus import Bus, BusSettings
    from lakehouse.cloud_events import lineage_envelope
    from lakehouse.outbox import UnpublishedEventError, publish_with_outbox
    from lakehouse.storage_options import StorageOptions
    from lakehouse.store_session import WebIdentity, exchange

    with tempfile.TemporaryDirectory() as tokens:
        identity = WebIdentity(
            token_file=_projected(spec["issuer"], PRODUCER, Path(tokens)),
            endpoint=spec["endpoint"],
            region="us-east-1",
            role_arn="arn:aws:iam::000000000000:role/rask",
        )
        session = exchange(identity, session_name="port-lanes-check", duration_seconds=900)
    options = StorageOptions(key_pair=session.key_pair, options={}).with_endpoint(spec["endpoint"])
    url = spec["bus_url"] or "nats://127.0.0.1:1"
    bus = Bus(BusSettings.model_validate({"RASK_BUS_URL": url, "RASK_BUS_USER": "catalog", "RASK_BUS_TOKEN_FILE": spec["token_file"]}))
    keeping = asyncio.create_task(bus.keep_connected()) if spec["bus_url"] else None
    for _ in range(200):
        if bus.connected or keeping is None:
            break
        await asyncio.sleep(0.05)
    envelope = lineage_envelope(spec["event"])
    try:
        await publish_with_outbox(
            bus, envelope, outbox_uri=spec["outbox_uri"], storage_options=options, topic="lineage.events.v1", timeout_seconds=5, lane="lineage"
        )
        left_staged = False
    except UnpublishedEventError as exc:
        left_staged = exc.staged
    if keeping is not None:
        keeping.cancel()
        await asyncio.gather(keeping, return_exceptions=True)
    await bus.close()
    return {"id": envelope.id, "left_staged": left_staged}


async def _lane(spec: dict[str, Any]) -> dict[str, Any]:
    from lineage.core.metrics import Door
    from lineage.services.consumer import handle_cloud_event
    from lineage.services.provenance import ProvenanceStore

    store = ProvenanceStore(catalog_url=spec["catalog_url"], namespace=spec["namespace"], token=lambda: None, refuse_foreign_runs=True)
    await store.ensure()
    verdict = await handle_cloud_event(store, spec["envelope"], None, door=Door.SUBSCRIBER)
    return {"verdict": verdict.value}


def _rows(spec: dict[str, Any]) -> dict[str, Any]:
    from port_store_cross import _raw

    return {table: _raw(spec["catalog_url"], spec["namespace"], table)[1] for table in ("events", "edges", "runs")}


def main() -> None:
    sys.path.insert(0, str(_ROOT / "services"))
    sys.path.insert(0, str(_ROOT / "scripts"))
    command, path = sys.argv[1:3]
    spec = json.loads(Path(path).read_text())
    if command == "produce":
        answer = asyncio.run(_produce(spec))
    elif command == "lane":
        answer = asyncio.run(_lane(spec))
    else:
        answer = _rows(spec)
    print(json.dumps(answer, default=str), flush=True)


if __name__ == "__main__":
    main()
