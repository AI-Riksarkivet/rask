"""A service account's HTTP lineage event is decided on the table each name resolves to, by the catalog ([[LH-531]]).

Every grant is held by a table's uuid (ADR 0173 §8, [[LH-524]]), and an outside producer's event names its tables only.
Lineage resolves a name with DescribeTable under the producer's own bearer; a service account's projected token is
audience-bound to lineage, so the catalog refuses it and the name resolves to nothing there. Lineage then asks the
catalog's decision door by name, as the producer: the catalog resolves the name, decides on the uuid, and answers the
uuid with the decision. So a pipeline on workload identity records lineage for a table it may write, keyed by that
table's uuid as the catalog's own events are, and is refused one it may not.

Nothing stands in: the catalog's own app on a real `dir` namespace with OpenFGA on a store of this test's own, lineage's
own app and lifespan asking it through `CatalogDecider` and recording into its provenance namespace, and every caller a
token a real verifier checks: the loopback Dex for the person who makes the tables, the loopback service-account issuer
for lineage and the producer. The grants a deployment's bootstrap hook writes are seeded straight to the store.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pytest
from pydantic import BaseModel

from service_kit.governed import fga
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from tests.integration.lineage_app import live_catalog, live_lineage, run_event


#: Lineage's service account and the pipeline's, as the kubelet names them.
_LINEAGE_SA, _PRODUCER_SA = "rask-sa-lineage", "producer"
_LINEAGE = f"kubernetes~system:serviceaccount:default:{_LINEAGE_SA}"
_PRODUCER = f"kubernetes~system:serviceaccount:etl:{_PRODUCER_SA}"
#: The table the producer may write, and one it holds no grant on.
_MINE, _THEIRS = "acme$mine", "acme$theirs"
_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))


class Estate(BaseModel):
    """Lineage's address, the uuid of each table the catalog made, and of their namespace."""

    lineage_url: str
    uuids: dict[str, str]
    namespace_uuid: str


@pytest.fixture
def estate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dex_issuer: Any, sa_issuer: Any, openfga_store: Any) -> Iterator[Estate]:
    """alice makes `acme$mine` and `acme$theirs` in a governed catalog; the producer may write the first; lineage is booted against it."""
    sa = {"RASK_SA_ISSUER": sa_issuer.issuer, "RASK_SA_FETCH_TOKEN_FILE": str(sa_issuer.fetch_token_file), "RASK_SA_CA_FILE": str(sa_issuer.ca_file)}
    people = {
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": dex_issuer.issuer,
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_OIDC_ALLOW_INSECURE": "true",
        "RASK_OIDC_CONNECTORS": json.dumps({"local": "dex"}),
    }
    catalog_env = {
        **people,
        **sa,
        "RASK_SA_AUDIENCE": "rask-catalog",
        "RASK_FGA_ENABLED": "true",
        "RASK_FGA_API_URL": openfga_store.url,
        "RASK_FGA_STORE_ID": openfga_store.store_id,
        "RASK_FGA_MODEL_ID": openfga_store.model_id,
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_PROVENANCE_WRITERS": json.dumps([_LINEAGE]),
    }
    alice = {"Authorization": f"Bearer {dex_issuer.mint('alice', connector='local', audience='lance-catalog')}"}
    with (
        live_catalog(tmp_path / "catalog", monkeypatch, **catalog_env) as catalog_url,
        httpx.Client(base_url=catalog_url, headers=alice, timeout=60) as catalog,
    ):
        assert catalog.post("/v1/namespace/acme/create", json={}).status_code == 200
        namespace_uuid = catalog.post("/v1/namespace/acme/describe", json={}).json()["properties"]["rask.uuid"]
        uuids: dict[str, str] = {}
        for table in (_MINE, _THEIRS):
            created = catalog.post(f"/v1/table/{table}/create", content=_ROWS, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
            assert created.status_code == 200, created.text
            uuids[table] = catalog.post(f"/v1/table/{table}/describe", json={"vend_credentials": False}).json()["properties"]["rask.uuid"]
        openfga_store.write(
            (fga.user_object(_LINEAGE), "decider", "estate:rask"),
            (fga.user_object(_LINEAGE), "governance_prober", "estate:rask"),
            (fga.user_object(_PRODUCER), "writer", fga.table_object(uuids[_MINE])),
        )
        lineage_token = tmp_path / "lineage-catalog-token"
        lineage_token.write_text(sa_issuer.mint(_LINEAGE_SA, audience="rask-catalog"))
        for key, value in {
            **sa,
            "RASK_SA_AUDIENCE": "rask-lineage",
            "RASK_FGA_ENABLED": "true",
            "RASK_FGA_DECIDER_URL": catalog_url,
            "RASK_FGA_DECIDER_TOKEN_FILE": str(lineage_token),
            "RASK_CATALOG_IDENTITY_TOKEN_FILE": str(lineage_token),
        }.items():
            monkeypatch.setenv(key, value)
        with live_lineage(catalog_url, monkeypatch) as lineage_url:
            yield Estate(lineage_url=lineage_url, uuids=uuids, namespace_uuid=namespace_uuid)


@pytest.mark.parametrize(
    ("table", "status"),
    [pytest.param(_MINE, 201, id="a-table-it-may-write"), pytest.param(_THEIRS, 403, id="a-table-it-holds-no-grant-on")],
)
def test_a_service_accounts_event_is_admitted_only_for_a_table_it_may_write_and_recorded_by_its_uuid(
    estate: Estate, sa_issuer: Any, table: str, status: int
) -> None:
    producer = {"Authorization": f"Bearer {sa_issuer.mint(_PRODUCER_SA, namespace='etl', audience='rask-lineage')}"}
    with httpx.Client(base_url=estate.lineage_url, headers=producer, timeout=60) as lineage:
        posted = lineage.post("/api/v1/lineage", json=run_event("run-1", "COMPLETE", "2026-10-24T10:00:00+00:00", outputs=[table]))
        feed = lineage.get("/events").json()["events"]

    assert posted.status_code == status, posted.text
    assert [(row["outputs"], row["table_uuids"], row["namespace_uuids"], row["unresolved_tables"]) for row in feed] == (
        [([table], [estate.uuids[table]], [estate.namespace_uuid], [])] if status == 201 else []
    )
