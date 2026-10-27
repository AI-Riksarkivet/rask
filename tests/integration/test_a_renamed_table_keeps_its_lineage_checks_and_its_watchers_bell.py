"""A renamed table's grants stay on its uuid, so lineage still admits what is written to it and its watchers are still told.

A rename keeps a table's uuid and every grant held by it (ADR 0173 §8, [[LH-524]]); an event names each table by its
name and carries its uuid in the dataset's `rask_lance` facet (ADR 0173 §10, [[LH-522]]). Lineage and notifications ask the
decision door about that uuid, never the name. So after a live catalog renames `acme$t` to `acme$u`, a write to
`acme$u` whose run read the source under the name it held before the rename, and an outside producer's event naming
`acme$u` (which lineage's HTTP door resolves to the same uuid), are both admitted, and the project's watcher, who reads
the table, finds the write's row in their inbox naming the table's uuid and its name now.

One Act: the write's journey, lineage's gates then the notifications fan-out, which is the path the clause names.
Every decision is the real model's, on a store of this test's own ([[LH-235]]), asked through the `OpenFgaDecider` the
services use. The inbox actor is a list per subject.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pyarrow as pa
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from pydantic import BaseModel

from lineage.api.fga_deps import enforce_bus_authz, enforce_output_authz
from lineage.core.config import LineageSettings
from lineage.models import RunEvent, parse_event
from lineage.services.names import CatalogNames, stamp_resolved_uuids
from notifications.api.fanout import audience_for, fan_out
from notifications.api.lineage_events import LineageRunEvent, notifiable
from notifications.api.visibility import Visibility
from notifications.proxies import TypedActorProxy
from service_kit.governed import fga
from service_kit.governed.decider import OpenFgaDecider
from service_kit.governed.idp_identity import PersonPrincipal
from service_kit.governed.oidc import IDToken
from service_kit.governed.principal import Principal
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from service_kit.openlineage import DatasetUuids
from tests.integration.lineage_app import live_catalog


PIPELINE, WATCHER = "dex~pipeline", "dex~watcher"
_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))


def _dataset(name: str, uuid: str | None = None) -> dict[str, Any]:
    return {"namespace": "acme", "name": name, **({"facets": {"rask_lance": {"tableUuid": uuid}}} if uuid else {})}


def _write(*, outputs: list[dict[str, Any]], inputs: list[dict[str, Any]], producer: str) -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-10-22T10:00:00+00:00",
        "producer": producer,
        "run": {
            "runId": f"run-{producer[-8:]}",
            "facets": {"rask_author": {"name": PIPELINE, "sub": PIPELINE}, "rask_lance": {"operation": "insert", "project": "acme"}},
        },
        "job": {"namespace": "pipeline", "name": "derive"},
        "inputs": inputs,
        "outputs": outputs,
    }


class _Feed:
    """A feed holding no run yet, so each event founds its run."""

    async def run_author(self, _run_id: str) -> str | None:
        return None

    async def recorded_event(self, _run_id: str, _event_type: str | None) -> dict[str, Any] | None:
        return None


class _Inbox:
    def __init__(self, boxes: dict[str, list[dict[str, Any]]], subject: str) -> None:
        self._boxes, self._subject = boxes, subject

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = self._boxes.setdefault(self._subject, [])
        rows.append(payload)
        return {"delivered": True, "unread": len(rows), "rows": len(rows)}


class Renamed(BaseModel):
    """The uuids a live catalog answers for the renamed table and the source its run read."""

    table: str
    source: str


@pytest.fixture
def renamed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Renamed:
    """A live catalog creates `acme$t` and `acme$src` (one row each), renames them `acme$u` and `acme$src2`, and answers their uuids."""
    with live_catalog(tmp_path / "catalog", monkeypatch) as url, httpx.Client(base_url=url, timeout=60) as catalog:
        assert catalog.post("/v1/namespace/acme/create", json={}).status_code == 200
        for table, renamed_to in (("t", "u"), ("src", "src2")):
            created = catalog.post(f"/v1/table/acme${table}/create", content=_ROWS, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
            assert created.status_code == 200, created.text
            moved = catalog.post(f"/v1/table/acme${table}/rename", json={"new_table_name": renamed_to})
            assert moved.status_code == 200, moved.text
        uuid = {name: catalog.post(f"/v1/table/acme${name}/describe", json={}).json()["properties"]["rask.uuid"] for name in ("u", "src2")}
    return Renamed(table=uuid["u"], source=uuid["src2"])


@pytest.fixture
def granted(openfga_store: Any, renamed: Renamed) -> Any:  # noqa: ANN401 — root conftest's OpenFgaStore
    """The pipeline writes the renamed table and reads its source; the watcher is a member of acme and reads the table."""
    openfga_store.write(
        (fga.user_object(PIPELINE), "writer", fga.table_object(renamed.table)),
        (fga.user_object(PIPELINE), "reader", fga.table_object(renamed.source)),
        (fga.user_object(WATCHER), "member", "project:acme"),
        (fga.user_object(WATCHER), "reader", fga.table_object(renamed.table)),
    )
    return openfga_store


@pytest_asyncio.fixture
async def decider(granted: Any) -> AsyncIterator[OpenFgaDecider]:  # noqa: ANN401 — root conftest's OpenFgaStore
    """The decider every check-only service asks, over the store the grants were written to."""
    async with fga.make_client(granted.url, granted.store_id, granted.model_id, token_file=None) as client:
        yield OpenFgaDecider(client)


def _lineage_request(decider: OpenFgaDecider) -> Request:
    """A request to lineage's app, whose state holds the decider and the feed its gates read."""
    app = FastAPI()
    app.state.decider = decider
    app.state.provenance = _Feed()
    return Request({"type": "http", "method": "POST", "path": "/lineage-events", "headers": [], "app": app})


@pytest.mark.asyncio
async def test_a_renamed_tables_writes_pass_lineage_and_ring_its_watchers_bell(decider: OpenFgaDecider, renamed: Renamed) -> None:
    settings = LineageSettings.model_validate(
        {"oidc_enabled": True, "oidc_issuer": "https://idp.example.com", "oidc_audience": "lance", "oidc_connectors": {"local": "dex"}, "fga_enabled": True}
    )
    caller = PersonPrincipal(principal=Principal(idp_id="dex", subject="pipeline"), claims=IDToken(iss="i", sub="pipeline", aud="lance", exp=1, iat=1))
    write = _write(outputs=[_dataset("acme$u", renamed.table)], inputs=[_dataset("acme$src", renamed.source)], producer="https://rask.invalid/catalog")
    outside = parse_event(_write(outputs=[_dataset("acme$u")], inputs=[], producer="https://rask.invalid/outside"))

    class _Catalog:
        """The catalog's DescribeTable as the outside producer's bearer reaches it: `acme$u` names the renamed table."""

        async def table(self, name: str, *, bearer: str | None) -> DatasetUuids:
            return DatasetUuids(table_uuid=renamed.table) if (name, bearer) == ("acme$u", "pipeline-token") else DatasetUuids()

        async def namespace(self, name: str, *, bearer: str | None) -> str | None:
            return None

    view = Visibility(decider=decider, enabled=True)

    async def watchers(_project: str, _at: datetime) -> Sequence[str]:
        return [WATCHER]

    boxes: dict[str, list[dict[str, Any]]] = {}
    notice = notifiable(LineageRunEvent.model_validate(write))
    assert notice is not None

    request = _lineage_request(decider)
    await enforce_bus_authz(RunEvent.model_validate(write), request, settings, write)
    await stamp_resolved_uuids(outside, cast(CatalogNames, _Catalog()), bearer="pipeline-token")
    await enforce_output_authz(outside, request, settings, caller)
    audience = await audience_for(notice, watchers=watchers, members=view.still_a_member)
    await fan_out(notice, audience=[s for s in audience if s == WATCHER], visibility=view, open_inbox=lambda s: cast(TypedActorProxy, _Inbox(boxes, s)))

    assert [(row["reason"], row["object_id"], row["object_name"]) for row in boxes.get(WATCHER, [])] == [("watch", fga.table_object(renamed.table), "acme$u")]
