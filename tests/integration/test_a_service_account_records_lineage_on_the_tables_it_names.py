"""A service account's HTTP lineage event is decided on the table each name resolves to, by the catalog ([[LH-531]]).

Every grant is held by a table's uuid (ADR 0173 §8, [[LH-524]]), and an outside producer's event names its tables only.
Lineage resolves a name with DescribeTable under the producer's own bearer; a service account's projected token is
audience-bound to lineage, so the catalog refuses it and the name resolves to nothing there. Lineage then asks the
catalog's decision door by name, as the producer: the catalog resolves the name, decides on the uuid, and answers the
uuid with the decision. So a pipeline on workload identity records lineage for a table it may write, keyed by that
table's uuid as the catalog's own events are, and is refused one it may not.

A graph backend joins a dataset by every identifier it carries, and the catalog names each table's storage location in
its events' ``symlinks`` facet ([[LH-553]]). So a producer naming a table by its location, or listing the table among its
own dataset's symlinks, is decided on that table: lineage asks the catalog's ``locate`` door which table a location
holds, and resolves a symlinked name as the producer. What the catalog answered scopes the event for every reader after
([[LH-598]]): its feed row, whole or as a summary, and its staged copy at the outbox door are shown only to a reader who
may see every table it names, under whatever identifier it names them.

The catalog is asked about an event's names in one question, never once per name: a producer that may see none of
them costs it the one question that says so. An event naming more identifiers than one question holds is refused
before the catalog is asked anything.

Nothing stands in: the catalog's own app on a real `dir` namespace with OpenFGA on a store of this test's own, lineage's
own app and lifespan asking it through `CatalogDecider` and recording into its provenance namespace, and every caller a
token a real verifier checks: the loopback Dex for the person who makes the tables, the loopback service-account issuer
for lineage and the producer. The grants a deployment's declaration writes are seeded straight to the store.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pytest
from pydantic import BaseModel, ConfigDict

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.events import outbox
from lakehouse.events.cloud_events import lineage_envelope
from lakehouse.events.lineage_events import DatasetUuids, with_dataset_uuids
from lakehouse.events.openlineage import SYMLINKS_FACET_SCHEMA_URL, author_facet
from lakehouse.governed import fga
from lakehouse.governed.decider import MAX_OBJECTS_PER_QUESTION
from lakehouse.naming import location_identifier
from lakehouse.storage_options import StorageOptions
from tests.integration.lineage_app import live_catalog, live_lineage, run_event
from tests.integration.store_requests import CountedStore, counted


#: Lineage's service account, the pipeline's, and that of a reader of the table the pipeline writes, as the kubelet names them.
_LINEAGE_SA, _PRODUCER_SA, _READER_SA = "rask-sa-lineage", "producer", "reader"
_LINEAGE = f"kubernetes~system:serviceaccount:default:{_LINEAGE_SA}"
_PRODUCER = f"kubernetes~system:serviceaccount:etl:{_PRODUCER_SA}"
_READER = f"kubernetes~system:serviceaccount:etl:{_READER_SA}"
#: The table the producer may write, one it may only read, and one it holds no grant on.
_MINE, _READ, _THEIRS = "lance_catalog$acme$mine", "lance_catalog$acme$read", "lance_catalog$acme$theirs"
_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))


class Estate(BaseModel):
    """Lineage's address, the uuid and storage location of each table the catalog made, their namespace's uuid, what
    lineage asked the catalog, and the outbox lineage's doors read staged events from."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    lineage_url: str
    uuids: dict[str, str]
    locations: dict[str, str]
    namespace_uuid: str
    asked: CountedStore
    outbox_uri: str


@pytest.fixture
def estate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dex_issuer: Any, sa_issuer: Any, openfga_store: Any) -> Iterator[Estate]:
    """alice makes `lance_catalog$acme$mine`, `$read` and `$theirs` in a governed catalog; the producer may write the first
    and read the second, the reader may read only the first; lineage is booted against it."""
    sa = {"RASK_SA_ISSUER": sa_issuer.issuer, "RASK_SA_FETCH_TOKEN_FILE": str(sa_issuer.fetch_token_file), "RASK_SA_CA_FILE": str(sa_issuer.ca_file)}
    people = {
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": dex_issuer.issuer,
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_OIDC_ALLOW_INSECURE": "true",
        "RASK_OIDC_IDP_ID": "dex",
        "RASK_OIDC_CONNECTORS": json.dumps({"local": "dex"}),
    }
    catalog_env = {
        **people,
        **sa,
        "RASK_SA_AUDIENCE": "rask-catalog",
        "RASK_FGA_ENABLED": "true",
        "RASK_FGA_API_URL": openfga_store.url,
        "RASK_FGA_STORE_ID": openfga_store.store_id,
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_PROVENANCE_WRITERS": json.dumps([_LINEAGE]),
    }
    alice = {"Authorization": f"Bearer {dex_issuer.mint('alice', connector='local', audience='lance-catalog')}"}
    with (
        live_catalog(tmp_path / "catalog", monkeypatch, **catalog_env) as catalog_url,
        httpx.Client(base_url=catalog_url, headers=alice, timeout=60) as catalog,
        ExitStack() as stack,
    ):
        # alice owns the estate's own warehouse, as the catalog's declaration makes its admin, so she may create in it.
        openfga_store.write((fga.user_object("dex~alice"), "owner", "warehouse:lance_catalog"))
        assert catalog.post("/v1/namespace/lance_catalog$acme/create", json={}).status_code == 200
        namespace_uuid = catalog.post("/v1/namespace/lance_catalog$acme/describe", json={}).json()["properties"]["rask.uuid"]
        uuids: dict[str, str] = {}
        locations: dict[str, str] = {}
        for table in (_MINE, _READ, _THEIRS):
            created = catalog.post(f"/v1/table/{table}/create", content=_ROWS, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
            assert created.status_code == 200, created.text
            described = catalog.post(f"/v1/table/{table}/describe", json={"vend_credentials": False}).json()
            uuids[table], locations[table] = described["properties"]["rask.uuid"], described["location"]
        openfga_store.write(
            (fga.user_object(_LINEAGE), "decider", "estate:rask"),
            (fga.user_object(_LINEAGE), "governance_prober", "estate:rask"),
            (fga.user_object(_PRODUCER), "writer", fga.table_object(uuids[_MINE])),
            (fga.user_object(_PRODUCER), "reader", fga.table_object(uuids[_READ])),
            (fga.user_object(_READER), "reader", fga.table_object(uuids[_MINE])),
        )
        asked = stack.enter_context(counted(catalog_url))
        outbox_uri = f"file://{tmp_path}/lineage-outbox"
        lineage_token = tmp_path / "lineage-catalog-token"
        lineage_token.write_text(sa_issuer.mint(_LINEAGE_SA, audience="rask-catalog"))
        for key, value in {
            **sa,
            "RASK_SA_AUDIENCE": "rask-lineage",
            "RASK_FGA_ENABLED": "true",
            "RASK_FGA_DECIDER_URL": asked.url,
            "RASK_FGA_DECIDER_TOKEN_FILE": str(lineage_token),
            "RASK_CATALOG_IDENTITY_TOKEN_FILE": str(lineage_token),
            "LINEAGE_OUTBOX_URI": outbox_uri,
        }.items():
            monkeypatch.setenv(key, value)
        with live_lineage(asked.url, monkeypatch) as lineage_url:
            yield Estate(lineage_url=lineage_url, uuids=uuids, locations=locations, namespace_uuid=namespace_uuid, asked=asked, outbox_uri=outbox_uri)


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


@pytest.mark.parametrize(
    ("named_as", "status"),
    [
        pytest.param(lambda estate: location_input(estate.locations[_THEIRS]), 403, id="the-location-of-a-table-it-may-not-see"),
        pytest.param(lambda estate: location_input(estate.locations[_THEIRS] + "-elsewhere"), 201, id="a-location-no-table-holds"),
        pytest.param(lambda estate: symlinked_input(_THEIRS), 403, id="a-symlink-to-a-table-it-may-not-see"),
    ],
)
def test_a_dataset_named_by_a_location_or_symlink_is_decided_on_the_table_it_names(
    estate: Estate, sa_issuer: Any, named_as: Callable[[Estate], dict[str, Any]], status: int
) -> None:
    producer = {"Authorization": f"Bearer {sa_issuer.mint(_PRODUCER_SA, namespace='etl', audience='rask-lineage')}"}
    event = run_event("run-2", "COMPLETE", "2026-11-03T10:00:00+00:00", outputs=[_MINE])
    event["inputs"] = [named_as(estate)]
    with httpx.Client(base_url=estate.lineage_url, headers=producer, timeout=60) as lineage:
        posted = lineage.post("/api/v1/lineage", json=event)
        recorded = [(row["outputs"], sorted(row["table_uuids"])) for row in lineage.get("/events").json()["events"]]

    # A location no table holds is the outside world, so the row names the output alone.
    assert (posted.status_code, recorded) == (status, [([_MINE], [estate.uuids[_MINE]])] if status == 201 else []), posted.text


def _shown(lineage: httpx.Client, staged_run: str) -> dict[str, object]:
    """What lineage's read doors show the caller of ``lineage``: the feed whole and as a summary, the staged events at
    the outbox door, and that door's answer to a replay of ``staged_run``."""
    feed = lineage.get("/events").json()["events"]
    summary = lineage.get("/events", params={"summary": "true"}).json()["events"]
    staged = lineage.get("/admin/dlq").json()["events"]
    replay = lineage.post(f"/admin/dlq/{staged_run}/replay")
    return {
        "feed": [(row["outputs"], [(d["namespace"], d["name"]) for d in row["event"]["inputs"]], sorted(row["table_uuids"])) for row in feed],
        "summary": [(row["inputs"], row["outputs"], row["event"]) for row in summary],
        "staged": [(event["run_id"], event["inputs"], event["outputs"]) for event in staged],
        "replay": (
            replay.status_code,
            replay.headers["content-type"],
            {key: replay.json().get(key) for key in ("status", "title", "detail", "code", "run_id")},
        ),
    }


@pytest.mark.parametrize(
    "aliased",
    [
        pytest.param(lambda estate: {"inputs": [location_input(estate.locations[_READ])]}, id="an-input-by-its-location"),
        pytest.param(lambda estate: {"inputs": [symlinked_input(_READ)]}, id="an-input-by-a-symlink"),
        pytest.param(lambda estate: {"outputs": [column_upstream_at(_MINE, estate.locations[_READ])]}, id="a-column-upstream-by-its-location"),
    ],
)
def test_an_event_naming_a_table_by_an_alias_is_shown_only_to_a_reader_of_that_table(
    estate: Estate, sa_issuer: Any, aliased: Callable[[Estate], dict[str, Any]]
) -> None:
    """A producer that may read a table records reading it under its location or a symlink, into a table a second reader
    may see ([[LH-598]]). That reader holds nothing on the table read, so no door shows it the event: not its feed row,
    whole or as a summary, not its staged copy, and a replay of that copy answers as for a run nothing staged. The
    producer, who may see both tables, is shown each."""
    recorded, staged = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a01", "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a02"
    naming = aliased(estate)
    event = run_event(recorded, "COMPLETE", "2026-11-16T10:00:00+00:00", outputs=[_MINE]) | naming
    author = {"rask_author": author_facet("https://example.invalid/etl", _PRODUCER)}
    unrecorded = run_event(staged, "COMPLETE", "2026-11-16T10:00:01+00:00", outputs=[_MINE], facets=author) | naming
    # A producer that stages names each table it wrote by the uuids the catalog answered it, as the catalog's own events do.
    mine = DatasetUuids(table_uuid=estate.uuids[_MINE], namespace_uuid=estate.namespace_uuid)
    unrecorded["outputs"] = [with_dataset_uuids(output, mine, producer="https://example.invalid/etl") for output in unrecorded["outputs"]]
    outbox.stage_envelope(estate.outbox_uri, StorageOptions(), lineage_envelope(unrecorded))
    producer = {"Authorization": f"Bearer {sa_issuer.mint(_PRODUCER_SA, namespace='etl', audience='rask-lineage')}"}
    reader = {"Authorization": f"Bearer {sa_issuer.mint(_READER_SA, namespace='etl', audience='rask-lineage')}"}
    with httpx.Client(base_url=estate.lineage_url, timeout=60) as lineage:
        posted = lineage.post("/api/v1/lineage", json=event, headers=producer)
        lineage.headers.update(reader)
        to_the_reader = _shown(lineage, staged)
        lineage.headers.update(producer)
        to_the_producer = _shown(lineage, staged)

    assert posted.status_code == 201, posted.text
    inputs = [(dataset["namespace"], dataset["name"]) for dataset in event["inputs"]]
    both = sorted([estate.uuids[_MINE], estate.uuids[_READ]])
    absent = {"status": 404, "title": "TransactionNotFoundError", "detail": f"no staged lineage event for run {staged}", "code": 10, "run_id": None}
    assert (to_the_reader, to_the_producer) == (
        {"feed": [], "summary": [], "staged": [], "replay": (404, "application/problem+json", absent)},
        {
            "feed": [([_MINE], inputs, both)],
            "summary": [([name for _, name in inputs], [_MINE], {})],
            "staged": [(staged, [name for _, name in inputs], [_MINE])],
            "replay": (200, "application/json", {"status": "replayed", "title": None, "detail": None, "code": None, "run_id": staged}),
        },
    )


def _named_by_more_than_one_question_holds() -> dict[str, Any]:
    """An event whose one output lists, in its ``symlinks`` facet, one identifier more than a decision question names."""
    identifiers = [{"namespace": "s3://elsewhere", "name": f"raw/{index}", "type": "LOCATION"} for index in range(MAX_OBJECTS_PER_QUESTION)]
    event = run_event("run-4", "COMPLETE", "2026-11-12T10:00:00+00:00", outputs=[_THEIRS])
    event["outputs"][0]["facets"] = {
        "symlinks": {"_producer": "https://example.invalid/etl", "_schemaURL": SYMLINKS_FACET_SCHEMA_URL, "identifiers": identifiers}
    }
    return event


@pytest.mark.parametrize(
    ("event", "status", "questions"),
    [
        pytest.param(
            run_event("run-3", "COMPLETE", "2026-11-12T10:00:00+00:00", outputs=[_THEIRS], inputs=(_READ, _MINE)), 403, 1, id="names-it-may-not-write"
        ),
        pytest.param(_named_by_more_than_one_question_holds(), 422, 0, id="more-names-than-one-question-holds"),
    ],
)
def test_a_producer_is_judged_on_one_question_whatever_the_names_its_event_carries(
    estate: Estate, sa_issuer: Any, event: dict[str, Any], status: int, questions: int
) -> None:
    producer = {"Authorization": f"Bearer {sa_issuer.mint(_PRODUCER_SA, namespace='etl', audience='rask-lineage')}"}
    estate.asked.taken()
    with httpx.Client(base_url=estate.lineage_url, headers=producer, timeout=60) as lineage:
        posted = lineage.post("/api/v1/lineage", json=event)

    # What lineage asked about the event's names; its reads of its own provenance tables are not about them.
    about_names = [
        path
        for path in (request.path for request in estate.asked.taken())
        if path.startswith("/v1/table/lance_catalog$acme$") or path.endswith("/decisions/check")
    ]
    assert (posted.status_code, about_names) == (status, ["/management/v1/decisions/check"] * questions), posted.text


def location_input(location: str) -> dict[str, Any]:
    """An input named by a storage location, as OpenLineage's clients name one (``lakehouse.naming.location_identifier``)."""
    namespace, name = location_identifier(location)
    return {"namespace": namespace, "name": name}


def symlinked_input(table: str) -> dict[str, Any]:
    """An outside source whose ``symlinks`` facet lists the governed ``table`` by its catalog name."""
    identifiers = [{"namespace": "lance-namespace://localhost/api/catalog", "name": table, "type": "TABLE"}]
    return {
        "namespace": "s3://elsewhere",
        "name": "raw/drop",
        "facets": {"symlinks": {"_producer": "https://example.invalid/etl", "_schemaURL": SYMLINKS_FACET_SCHEMA_URL, "identifiers": identifiers}},
    }


def column_upstream_at(table: str, location: str) -> dict[str, Any]:
    """The output ``table`` whose ``columnLineage`` facet names, as the source of one column, the dataset at ``location``."""
    namespace, name = location_identifier(location)
    lineage = {
        "_producer": "https://example.invalid/etl",
        "_schemaURL": "https://openlineage.io/spec/facets/1-2-0/ColumnLineageDatasetFacet.json#/$defs/ColumnLineageDatasetFacet",
    }
    fields = {"id": {"inputFields": [{"namespace": namespace, "name": name, "field": "id"}]}}
    return {"namespace": table.split("$")[0], "name": table, "facets": {"columnLineage": lineage | {"fields": fields}}}
