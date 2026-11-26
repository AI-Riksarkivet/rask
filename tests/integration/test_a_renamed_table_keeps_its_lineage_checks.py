"""A renamed table's grants stay on its uuid, so lineage still admits what is written to it.

A rename keeps a table's uuid and every grant held by it (ADR 0173 §8, [[LH-524]]); an event names each table by its
name and carries its uuid in the dataset's `rask_lance` facet (ADR 0173 §10, [[LH-522]]). Lineage asks the decision door
about that uuid, never the name. So after a live catalog renames `acme$t` to `acme$u`, a write to `acme$u` whose run read
the source under the name it held before the rename, and an outside producer's event naming `acme$u` (which lineage's
HTTP door resolves to the same uuid), are both admitted. So is the catalog's own announcement of the rename, which names
the source's old name as its ``previousIdentifier``: that name resolves to no table once the rename landed, and the
announcement is admitted on the catalog's signature, as every announcement the catalog attests is.

Every decision is the real model's, on a store of this test's own ([[LH-235]]), asked through the `OpenFgaDecider` the
services use.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pyarrow as pa
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import BaseModel

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.events.lineage_events import DatasetUuids
from lakehouse.governed import fga_ids, fga_provision
from lakehouse.governed.decider import OpenFgaDecider
from lakehouse.naming import UUID_PROPERTY
from lineage.api.fga_deps import enforce_bus_authz, enforce_output_authz
from lineage.core.config import LineageSettings, Signing
from lineage.models import RunEvent, parse_event
from lineage.services.names import CatalogNames, stamp_resolved_uuids
from lineage.services.signature import store_published_keys
from tests.callers import person
from tests.integration.lineage_app import live_catalog
from tests.secret_store_double import SecretStoreDouble


PIPELINE = "dex~pipeline"
#: The identity the catalog signs its announcements under, and the dataset namespace it names its tables under.
CATALOG, CATALOG_NAMESPACE = "service-catalog", "lance-namespace://localhost/api/catalog"
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

    async def recorded_event(self, _key: str) -> dict[str, Any] | None:
        return None


class Renamed(BaseModel):
    """The uuids a live catalog answers for the renamed table and the source its run read."""

    table: str
    source: str


@pytest.fixture
def renamed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Renamed:
    """A live catalog creates `acme$t` and `acme$src` (one row each), renames them `acme$u` and `acme$src2`, and answers their uuids."""
    with live_catalog(tmp_path / "catalog", monkeypatch) as url, httpx.Client(base_url=url, timeout=60) as catalog:
        assert catalog.post("/v1/namespace/lance_catalog$acme/create", json={}).status_code == 200
        for table, renamed_to in (("t", "u"), ("src", "src2")):
            created = catalog.post(f"/v1/table/lance_catalog$acme${table}/create", content=_ROWS, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
            assert created.status_code == 200, created.text
            moved = catalog.post(f"/v1/table/lance_catalog$acme${table}/rename", json={"new_table_name": renamed_to})
            assert moved.status_code == 200, moved.text
        uuid = {name: catalog.post(f"/v1/table/lance_catalog$acme${name}/describe", json={}).json()["properties"][UUID_PROPERTY] for name in ("u", "src2")}
    return Renamed(table=uuid["u"], source=uuid["src2"])


@pytest.fixture
def granted(openfga_store: Any, renamed: Renamed) -> Any:  # noqa: ANN401 — root conftest's OpenFgaStore
    """The pipeline writes the renamed table and reads its source."""
    openfga_store.write(
        (fga_ids.user_object(PIPELINE), "writer", fga_ids.table_object(renamed.table)),
        (fga_ids.user_object(PIPELINE), "reader", fga_ids.table_object(renamed.source)),
    )
    return openfga_store


@pytest_asyncio.fixture
async def decider(granted: Any) -> AsyncIterator[OpenFgaDecider]:  # noqa: ANN401 — root conftest's OpenFgaStore
    """The decider every check-only service asks, over the store the grants were written to."""
    async with fga_provision.make_client(granted.url, granted.store_id, granted.model_id, token_file=None) as client:
        yield OpenFgaDecider(client)


def _lineage_app(decider: OpenFgaDecider) -> FastAPI:
    """Lineage's app as its gates read it: its state holds the decider and the feed."""
    app = FastAPI()
    app.state.decider = decider
    app.state.provenance = _Feed()
    return app


def _announced_rename(renamed: Renamed) -> dict[str, Any]:
    """The catalog's announcement of `acme$t`'s rename to `acme$u`, from the catalog's own builder, for the pipeline."""
    from catalog.core.lineage_emit import build_write_event

    return build_write_event(
        table_id="lance_catalog$acme$u",
        namespace=CATALOG_NAMESPACE,
        author=PIPELINE,
        version=None,
        operation="rename_table",
        run_id="0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a04",
        event_time="2026-11-20T10:00:00+00:00",
        job_namespace="lance-catalog",
        uuids=DatasetUuids(table_uuid=renamed.table),
        renamed_from="lance_catalog$acme$t",
    )


@pytest.mark.asyncio
async def test_a_renamed_tables_writes_pass_lineages_checks(
    decider: OpenFgaDecider, renamed: Renamed, event_signer: Any, secret_store: SecretStoreDouble
) -> None:
    """Each gate refuses by raising, so the write, the outside producer's event and the catalog's signed announcement of
    the rename are admitted when none raises."""
    auth = {
        "oidc_enabled": True,
        "oidc_issuer": "https://idp.example.com",
        "oidc_audience": "lance",
        "oidc_idp_id": "dex",
        "oidc_connectors": {"local": "dex"},
        "fga_enabled": True,
    }
    settings = LineageSettings.model_validate(auth)
    caller = person("pipeline")
    write = _write(
        outputs=[_dataset("lance_catalog$acme$u", renamed.table)],
        inputs=[_dataset("lance_catalog$acme$src", renamed.source)],
        producer="https://rask.invalid/catalog",
    )
    outside = parse_event(_write(outputs=[_dataset("lance_catalog$acme$u")], inputs=[], producer="https://rask.invalid/outside"))

    class _Catalog:
        """The catalog's DescribeTable as the outside producer's bearer reaches it: `acme$u` names the renamed table."""

        async def table(self, name: str, *, bearer: str | None) -> DatasetUuids:
            return DatasetUuids(table_uuid=renamed.table) if (name, bearer) == ("lance_catalog$acme$u", "pipeline-token") else DatasetUuids()

        async def namespace(self, name: str, *, bearer: str | None) -> str | None:
            return None

    app = _lineage_app(decider)
    await enforce_bus_authz(RunEvent.model_validate(write), app, settings, arrived=write)
    outside = await stamp_resolved_uuids(outside, cast(CatalogNames, _Catalog()), bearer="pipeline-token")
    await enforce_output_authz(outside, app, settings, token=caller)
    catalog = event_signer(CATALOG)
    secret_store.hold(f"signing-public-{CATALOG}", {"keys": catalog.public})
    announced = catalog.sign(_announced_rename(renamed), on_behalf_of=PIPELINE)
    assert announced["dataset"]["facets"]["lifecycleStateChange"]["previousIdentifier"] == {"namespace": CATALOG_NAMESPACE, "name": "lance_catalog$acme$t"}
    app.state.published_keys = store_published_keys()
    signed = LineageSettings.model_validate({**auth, "signing": Signing(event_signers=frozenset({CATALOG}), event_delegators=frozenset({CATALOG}))})
    await enforce_bus_authz(parse_event(announced), app, signed, arrived=announced)
