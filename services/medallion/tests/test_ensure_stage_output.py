"""The mover ASKS the catalog where to write, instead of telling it afterwards.

Rule I2 — "resolve the location through the CATALOG, never compose a path" —
is applied today to the READ side only; `transform.py` says so outright: "the mover still owns where
it WRITES." That half is the defect behind everything the live cascade hit. The mover composed
`{root}/medallion/{tier}`, a layout the catalog has never vended, wrote there, and then told the
catalog that was the table's home. The catalog's own binding said otherwise, so publish opened the
catalog's answer and found nothing.

The ingest plane already does this correctly (`CatalogServiceClient.ensure`): describe, create if
absent, and take the location from the create's own response. This is that, for the medallion.

THE CREATE'S JOB IS A GOVERNED LOCATION, NOT A SCHEMA. The mover does not know its output schema
until it has computed, and it does not need to: a Lance `overwrite` replaces the schema wholesale, so
the empty table exists only to make the catalog mint and govern a URI the mover can then write to.
"""

from __future__ import annotations

import httpx
import pyarrow as pa
import pytest
import respx

from medallion.services.catalog_register import RegisterError, ensure_stage_output


CATALOG = "http://catalog.test"
SCHEMA = pa.schema([pa.field("id", pa.int64())])
VENDED = "s3://acme-bucket/8f3a21bc_silver$features"


def _ensure(
    *,
    catalog_url: str = CATALOG,
    app_token: str | None = None,
    service_identity: str | None = None,
) -> str:
    return ensure_stage_output(
        catalog_url=catalog_url,
        table_id="silver$features",
        schema=SCHEMA,
        app_token=app_token,
        service_identity=service_identity,
    )


class TestItTakesTheLocationTheCatalogVends:
    @respx.mock
    def test_an_existing_table_is_described_not_recreated(self, respx_allows_unused_routes) -> None:
        describe = respx.post(f"{CATALOG}/v1/table/silver$features/describe").mock(return_value=httpx.Response(200, json={"location": VENDED}))
        create = respx.post(f"{CATALOG}/v1/table/silver$features/create").mock(return_value=httpx.Response(200, json={}))

        assert _ensure() == VENDED
        assert describe.called
        assert not create.called, "an existing table must not be recreated — that is the steady state"

    @respx.mock
    def test_an_absent_table_is_created_and_the_CREATE_answers_with_the_location(self) -> None:
        """One call, not two: the create's own response carries the location, and re-asking a read
        door afterwards asks a question that door cannot answer for a table it may not see."""
        respx.post(f"{CATALOG}/v1/table/silver$features/describe").mock(return_value=httpx.Response(403, json={}))
        create = respx.post(f"{CATALOG}/v1/table/silver$features/create").mock(return_value=httpx.Response(200, json={"location": VENDED}))

        assert _ensure() == VENDED
        assert create.called

    @respx.mock
    def test_the_created_table_is_EMPTY(self) -> None:
        """Zero rows, so no data byte transits the catalog — it is minting a URI, not ingesting."""
        respx.post(f"{CATALOG}/v1/table/silver$features/describe").mock(return_value=httpx.Response(403, json={}))
        create = respx.post(f"{CATALOG}/v1/table/silver$features/create").mock(return_value=httpx.Response(200, json={"location": VENDED}))

        _ensure()

        body = create.calls.last.request.content
        reader = pa.ipc.open_stream(pa.BufferReader(body))
        assert reader.read_all().num_rows == 0


class TestItNeverComposesAPath:
    @respx.mock
    def test_a_catalog_that_vends_NO_location_is_an_error_not_a_guess(self) -> None:
        """The whole point. Falling back to a composed path here would restore the defect exactly."""
        respx.post(f"{CATALOG}/v1/table/silver$features/describe").mock(return_value=httpx.Response(200, json={}))

        with pytest.raises(RegisterError, match="no location"):
            _ensure()

    @respx.mock
    def test_an_unreachable_catalog_raises_so_the_stage_retries(self) -> None:
        respx.post(f"{CATALOG}/v1/table/silver$features/describe").mock(side_effect=httpx.ConnectError("down"))

        with pytest.raises(RegisterError, match="unreachable"):
            _ensure()

    def test_no_catalog_url_refuses_at_the_seam(self) -> None:
        with pytest.raises(RegisterError, match="MEDALLION_CATALOG_URL"):
            _ensure(catalog_url="")


class TestItAuthenticatesAsAService:
    @respx.mock
    def test_both_calls_carry_the_service_door_headers(self) -> None:
        describe = respx.post(f"{CATALOG}/v1/table/silver$features/describe").mock(return_value=httpx.Response(403, json={}))
        create = respx.post(f"{CATALOG}/v1/table/silver$features/create").mock(return_value=httpx.Response(200, json={"location": VENDED}))

        _ensure(app_token="stamped", service_identity="service-bronze-to-silver")

        for route in (describe, create):
            assert route.calls.last.request.headers["x-lance-service-identity"] == "service-bronze-to-silver"
