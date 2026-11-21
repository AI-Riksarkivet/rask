"""An EXTERNAL source is not a governed object, and authorizing it as one refuses every honest producer.

THE MEASURED FAILURE. An ingest run lands its data in bronze and opens its run with a START event naming
where the data came from — an S3 prefix. The service answered:

    403 {"detail": "can_get_metadata required on inputs: bind86-src/run1"}

and because START was refused, the run was never opened. The terminal event that follows was authorized
perfectly well (measured: `201 ingested`), so bronze filled up over nine runs while lineage held zero
ingest events. Nothing reported it: the ingest plane's I8 guard is *required* never to raise,
so the emit failure is a log line, and every other check asks whether the DATA landed.

WHY NO TUPLE COULD EVER FIX IT. The guard composes `table:<input name>` and checks `can_get_metadata`.
`bind86-src/run1` is a bucket prefix. R23 makes raw the external world and never a governed table, so
that object does not exist in the store — and a check against a non-existent object returns
`allowed=false`, always. The guard asks an unanswerable question and reads the "no" as a denial. Ten
configuration causes were investigated (allowlist, credential, stale image, store id, model id, parent
tuple, seed) before the response BODY was read; each was a real defect, and none of them was this one.

The other repair — minting a `table:` object per S3 prefix so a tuple *could* exist — would put
ungoverned bucket paths into the authorization model as first-class tables. That is worse than the bug.

WHAT THESE TESTS PIN. Not "external inputs are skipped" as a rule to be trusted, but the two halves that
have to stay true together: an external input must pass unauthorized, AND a governed input must still be
refused when the caller cannot see it. A change that relaxes the second to fix the first would pass a
test written only for the first.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from lance_namespace import PermissionDeniedError
from lineage_lane import Feed
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientTuple, ClientWriteRequest

from lakehouse.events.lineage_events import DatasetUuids, with_dataset_uuids
from lakehouse.events.openlineage import DEFAULT_DATASET_NAMESPACE
from lakehouse.governed import fga_ids, fga_provision
from lakehouse.governed.decider import OpenFgaDecider
from lakehouse.governed.machine_identity import ServicePrincipal
from lineage.api.fga_deps import Governed, enforce_output_authz, is_external_source
from lineage.core.config import LineageSettings
from lineage.models import Dataset, Job, Run, RunEvent
from lineage.schemas import EventRecord


GOVERNED = "bind86-bronze$pages"
EXTERNAL_NS = "s3://images-batch"
EXTERNAL_NAME = "bind86-src/run1"


def _dataset(namespace: str, name: str) -> Dataset:
    return Dataset(namespace=namespace, name=name)


def _uuid(name: str) -> str:
    """The uuid the catalog minted for the governed table ``name``, as its events carry it (ADR 0173 §10)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _object(name: str) -> str:
    """The FGA object the governed table ``name``'s grants are held by."""
    return fga_ids.table_object(_uuid(name))


def _table_payload(namespace: str, name: str) -> dict[str, Any]:
    """A governed table as the catalog names it on an event: its name, and its uuid in the ``rask_lance`` facet."""
    return with_dataset_uuids({"namespace": namespace, "name": name}, DatasetUuids(table_uuid=_uuid(name)), producer="rask://test")


def _table(namespace: str, name: str) -> Dataset:
    return Dataset.model_validate(_table_payload(namespace, name))


def _event(*, inputs: list[Dataset] | None = None, outputs: list[Dataset] | None = None) -> RunEvent:
    """A REAL event carrying only the inputs and outputs the guard reads.

    The stub this replaces said a real one "drags the OpenLineage client into a test about an
    authorization decision". It does not: `RunEvent` is this service's OWN pydantic model
    (`lineage.models`) and imports nothing heavier than pydantic. The stub bought nothing and cost
    the type gate five diagnostics — and it could not have caught the guard reading a field the
    real event does not carry, which is the only thing a stub here would be for.
    """
    return RunEvent(
        eventType="START",
        eventTime="2026-08-09T00:00:00Z",
        run=Run(runId="run-1"),
        job=Job(namespace="ingest", name="ingest-run"),
        inputs=inputs or [],
        outputs=outputs or [],
    )


#: The pipeline's own account, the caller every authorization question below is asked as.
_PIPELINE = ServicePrincipal.for_account("system:serviceaccount:etl:service-pipeline")


@pytest.fixture
def settings() -> LineageSettings:
    # FGA refuses to enable without OIDC — it needs a VERIFIED subject to check, and an authz decision
    # over a self-asserted identity is not one. Constructing the pair here rather than patching the flag
    # keeps the test on the configuration the service actually runs.
    return LineageSettings(
        fga_enabled=True,
        oidc_enabled=True,
        oidc_issuer="https://example.invalid/realms/rask",
        oidc_audience="rask",
        oidc_idp_id="dex",
        oidc_connectors={"local": "dex"},
    )


class _Claims(OpenFgaDecider):
    """The store's answers, and a catalog whose claims hold the table ``held`` maps each location to, and none at any other
    location: those are the outside world. Which table a location holds is driven against the real catalog door in
    `tests/integration/test_a_service_account_records_lineage_on_the_tables_it_names.py`."""

    def __init__(self, client: OpenFgaClient, held: dict[str, str]) -> None:
        super().__init__(client)
        self._held = held

    async def locate(self, locations: list[str]) -> dict[str, str | None]:
        return {location: self._held.get(location) for location in locations}


type AppWith = Callable[..., AbstractAsyncContextManager[FastAPI]]


@pytest.fixture
def app_with(openfga_store: Any) -> AppWith:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """An app whose decider asks a real OpenFGA, on a store of the test's own holding the pipeline's ``granted`` rungs.

    A real store answers `allowed=false` for an object no tuple names, which is indistinguishable from a denial: that is
    what made the external source unauthorizable, and what each case here is judged by.
    """

    @asynccontextmanager
    async def _make(granted: dict[str, str], held: dict[str, str] | None = None) -> AsyncGenerator[FastAPI]:
        async with fga_provision.make_client(openfga_store.url, openfga_store.store_id, openfga_store.model_id, token_file=None) as client:
            if granted:
                tuples = [ClientTuple(user=fga_ids.user_object(_PIPELINE.subject), relation=rung, object=_object(name)) for name, rung in granted.items()]
                await client.write(ClientWriteRequest(writes=tuples))
            app = FastAPI()
            app.state.decider = _Claims(client, held or {})
            # The feed lineage's lifespan always wires, here one holding no run, so each event founds its run.
            app.state.provenance = Feed()
            yield app

    return _make


@pytest.mark.anyio
async def test_a_GOVERNED_input_the_caller_cannot_see_is_STILL_refused(settings: LineageSettings, app_with: AppWith) -> None:
    """The half the fix must not cost. This is the forgery the guard exists to stop: an authenticated
    reader recording `I read gold$catalog` into the feed."""
    event = _event(inputs=[_table("gold", "gold$catalog")])

    async with app_with({}) as app:
        with pytest.raises(PermissionDeniedError, match=r"can_get_metadata required on inputs: gold\$catalog"):
            await enforce_output_authz(event, app, settings, token=_PIPELINE)
    async with app_with({"gold$catalog": "reader"}) as app:
        await enforce_output_authz(event, app, settings, token=_PIPELINE)


@pytest.mark.anyio
async def test_a_FORGED_external_namespace_cannot_launder_a_governed_name(settings: LineageSettings, app_with: AppWith) -> None:
    """The attack the exemption invites, and why it does not land.

    A caller could name `gold$catalog` under an `s3://` namespace to skip the check. It skips — and buys
    nothing, because a NAMESPACE IS PART OF A DATASET'S IDENTITY. The recorded identity is
    `s3://anything/gold$catalog`, a different one from the governed `gold$catalog`, on no governed table's
    edges. The assertion is on the identity edges are keyed on, not on the check being skipped, because the
    skip is the mechanism and the identity is the guarantee.
    """
    forged = _dataset("s3://anything", "gold$catalog")
    event = _event(inputs=[forged])

    async with app_with({}) as app:
        await enforce_output_authz(event, app, settings, token=_PIPELINE)

    # ASSERTED ON THE IDENTITY KEY the edges use, never on `(forged.namespace, forged.name)` against a literal,
    # which is unconditionally true and cannot fail.
    governed = _dataset("gold", "gold$catalog")
    assert forged.lineage_name != governed.lineage_name, "the forged input resolved to the governed table's identity — the exemption IS a laundering path"


@pytest.mark.anyio
async def test_OUTPUTS_are_never_exempted(settings: LineageSettings, app_with: AppWith) -> None:
    """Writing is the direction that mutates the estate. An output under an external namespace is a
    producer claiming to have written the outside world — not a case to make permissive, and the
    asymmetry is deliberate rather than an oversight in the filter. Beside it here, a governed table's location the
    caller may write, which is that table and admits no outside output sharing its key under another bucket ([[LH-553]])."""
    located = _dataset("s3://lake", EXTERNAL_NAME)

    async with app_with({GOVERNED: "writer"}, {f"s3://lake/{EXTERNAL_NAME}": _uuid(GOVERNED)}) as app:
        await enforce_output_authz(_event(outputs=[located]), app, settings, token=_PIPELINE)
        with pytest.raises(PermissionDeniedError, match="can_write_data"):
            await enforce_output_authz(_event(outputs=[located, _dataset(EXTERNAL_NS, EXTERNAL_NAME)]), app, settings, token=_PIPELINE)


@pytest.mark.anyio
async def test_a_MIXED_event_authorizes_the_governed_input_and_skips_the_external_one(settings: LineageSettings, app_with: AppWith) -> None:
    """A run that reads a raw source AND a governed table must be judged on the governed half only."""
    event = _event(inputs=[_dataset(EXTERNAL_NS, EXTERNAL_NAME), _table("bind86-bronze", GOVERNED)])

    async with app_with({}) as app:
        with pytest.raises(PermissionDeniedError, match=r"can_get_metadata required on inputs: bind86-bronze\$pages$"):
            await enforce_output_authz(event, app, settings, token=_PIPELINE)
    async with app_with({GOVERNED: "reader"}) as app:
        await enforce_output_authz(event, app, settings, token=_PIPELINE)


@pytest.mark.parametrize(
    ("namespace", "name", "external"),
    [
        ("s3://images-batch", "run1/", True),
        # The BARE external namespaces, which no scheme test can catch: `local-dir` mints `file` and
        # `lance-append` mints `lance`, and both were authorized as governed until they were declared.
        ("file", "/tmp/ingest-fixtures", True),
        ("bronze", "bronze$pages", False),
        # A declared namespace does NOT exempt a governed table id. Both holes would have to be open at
        # once — a namespace literally named `file` AND a table id with no delimiter.
        ("file", "file$gold", False),
        # [[LH-553]] This estate's catalog names its own tables; another catalog's are another estate's.
        (DEFAULT_DATASET_NAMESPACE, "bronze$pages", False),
        ("lance-namespace://other.example.org/api/catalog", "bronze$pages", True),
    ],
)
def test_the_discriminator_is_a_URI_SCHEME_OR_A_DECLARED_NAMESPACE(namespace: str, name: str, external: bool) -> None:
    """TWO forms, because the estate's producers mint both, and the scheme alone missed two of three.

    A store URI carrying a scheme is external by shape. A bare identifier is exactly what a catalog
    namespace looks like, so the bare external namespaces are DECLARED in
    `lakehouse.naming` — the one place both this service and every producer read them
    from. For the bare form the NAME is part of the answer: a governed table id always carries the
    catalog delimiter, so it is never exempted even under a declared namespace."""
    assert is_external_source(namespace, name) is external


# --- THE READ-PATH TWIN --------------------------------------------------------------------------
#
# Everything above is the WRITE path: `enforce_output_authz` exempts an external input, because R23
# says raw is the external world and no tuple could ever be written for it.
#
# The READ path never got the same treatment, and it cannot apply the rule as written: the feed's `inputs`
# column keeps `[d.name for d in event.inputs]` — the NAMESPACE is discarded — so by the time `GET /events`
# governs a row it has only bare names, and `is_external_source` needs the namespace. Every event with
# an external input is therefore governed against `table:<bare name>`, which nobody holds a grant on,
# and the whole row is hidden from EVERY caller of the feed, whichever client it is.
#
# The namespace is not actually lost — the full event payload is stored alongside, and
# `_column_lineage_datasets` already reads it for exactly this kind of question.


def _record(*, inputs: list[str], outputs: list[str], event: dict) -> EventRecord:
    """A real `EventRecord`, not a stand-in — `_governed_datasets` takes the type the store yields,
    and a duck-typed double would only prove the helper works on a shape production never sends."""
    return EventRecord(seq=1, inputs=inputs, outputs=outputs, event=event)


def test_the_read_path_can_recover_the_namespace_the_columns_dropped() -> None:
    """The fix's precondition, stated as a test: the payload still knows what the columns forgot."""
    from lineage.api.v1.endpoints.events import _governed_datasets

    payload = _event(
        inputs=[_dataset("s3://lake/batch", "img.png")],
        outputs=[_table("bronze", "bronze-media$objects")],
    ).model_dump(by_alias=True)
    # `inputs` is what the feed keeps: the bare NAME, namespace discarded.
    record = _record(inputs=["img.png"], outputs=["bronze-media$objects"], event=payload)

    governed_on = _governed_datasets(record)

    # Exact: the external source is in neither set (else every event naming one stays hidden), and the output is
    # governed as the table its uuid names (outputs are never exempted).
    assert governed_on == Governed(objects=frozenset({_object("bronze-media$objects")}))


def test_a_summary_row_is_governed_on_the_uuids_it_recorded() -> None:
    """`summary=true` leaves the payload out of the read, so the namespace genuinely is not there.

    The row's recorded uuids are then all there is: each is the table it names, and a governed table the row recorded
    without a uuid stays unresolved, so the row is hidden rather than governed on nothing.
    """
    from lineage.api.v1.endpoints.events import _governed_datasets

    record = EventRecord(
        seq=1,
        inputs=["bronze$pages"],
        outputs=["bronze-media$objects"],
        table_uuids=[_uuid("bronze-media$objects")],
        unresolved_tables=["bronze$pages"],
        event={},
    )

    assert _governed_datasets(record) == Governed(objects=frozenset({_object("bronze-media$objects")}), unresolved=frozenset({"bronze$pages"}))


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# THE IDENTITY THE EDGES ARE KEYED ON — `Dataset.lineage_name`, not the pydantic object
# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
# An external source is qualified by its namespace, so it can never name a governed table's edges
# ([[LIN-3]], pinned through the real store by
# `tests/integration/test_an_erasure_finds_every_table_derived_from_a_source.py`).


def test_two_external_sources_with_the_same_name_stay_distinct_by_namespace() -> None:
    """The mirror: qualification must not collapse genuinely different sources onto one node either."""
    from lineage.models import Dataset

    a = Dataset(namespace="s3://bucket-a", name="run1")
    b = Dataset(namespace="s3://bucket-b", name="run1")

    assert a.lineage_name != b.lineage_name


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# THE AUTHZ SET MUST COVER EVERY DATASET THE EVENT ASSERTS READING
# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
# An event names datasets in a THIRD place besides `event.outputs` and `event.inputs`: inside
# `outputs[].facets.columnLineage.fields[*].inputFields[]`. The feed stores the whole event and the forwarder
# sends it to the configured graph backend, so a caller holding `can_write_data` on ONE sandbox table could
# otherwise name a GOVERNED dataset as a column upstream and have a derivation from it recorded and forwarded —
# with the governed name never presented to a single FGA check.


def _event_with_column_upstream(output: dict[str, Any], up_ns: str, up_name: str, *, inputs: list[dict[str, Any]] | None = None) -> RunEvent:
    """A COMPLETE event whose reference to `up_name` is inside the columnLineage facet, beside ``inputs``."""
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-08-16T12:00:00Z",
            "producer": "rask://test",
            "job": {"namespace": "test", "name": "j"},
            "run": {"runId": "11111111-2222-4333-8444-555555555555"},
            "inputs": inputs or [],
            "outputs": [
                {
                    **output,
                    "facets": {
                        **output.get("facets", {}),
                        "columnLineage": {"fields": {"ssn": {"inputFields": [{"namespace": up_ns, "name": up_name, "field": "ssn"}]}}},
                    },
                }
            ],
        }
    )


@pytest.mark.anyio
async def test_a_GOVERNED_column_upstream_is_AUTHORIZED_not_written_blind(settings: LineageSettings, app_with: AppWith) -> None:
    """The defect: the caller may write `mine$table` and holds nothing on `gold$catalog`, yet naming it
    as a column upstream would record and forward a derivation from it."""
    event = _event_with_column_upstream(_table_payload("mine", "mine$table"), "gold", GOVERNED)

    async with app_with({"mine$table": "writer"}) as app:  # write on the sandbox only
        with pytest.raises(PermissionDeniedError, match=r"can_get_metadata required on inputs: bind86-bronze\$pages"):
            await enforce_output_authz(event, app, settings, token=_PIPELINE)


@pytest.mark.anyio
async def test_a_column_upstream_the_caller_CAN_see_passes(settings: LineageSettings, app_with: AppWith) -> None:
    """The mirror — the check must not refuse a legitimate cross-table derivation, which is the whole
    point of column lineage."""
    event = _event_with_column_upstream(_table_payload("mine", "mine$table"), "gold", GOVERNED, inputs=[_table_payload("gold", GOVERNED)])

    async with app_with({"mine$table": "writer", GOVERNED: "reader"}) as app:
        await enforce_output_authz(event, app, settings, token=_PIPELINE)


@pytest.mark.anyio
async def test_an_EXTERNAL_column_upstream_stays_exempt(settings: LineageSettings, app_with: AppWith) -> None:
    """Same rule as inputs, and for the same reason: an external source has no `table:` object, so
    authorizing it is unsatisfiable rather than strict. Its identity is namespace-qualified, so it cannot
    name a governed table either way."""
    event = _event_with_column_upstream(_table_payload("mine", "mine$table"), "s3://raw", "gold$catalog")

    async with app_with({"mine$table": "writer"}) as app:
        await enforce_output_authz(event, app, settings, token=_PIPELINE)
