"""An EXTERNAL source is not a governed object, and authorizing it as one refuses every honest producer.

THE MEASURED FAILURE. An ingest run lands its data in bronze and opens its run with a START event naming
where the data came from — an S3 prefix. The service answered:

    403 {"detail": "can_get_metadata required on inputs: bind86-src/run1"}

and because START was refused, the run was never opened. The terminal event that follows was authorized
perfectly well (measured: `201 ingested`), so bronze filled up over nine runs while lineage held zero
ingest events. Nothing reported it: the ingest plane's I8 guard is *required* never to raise,
so the emit failure is a log line, and every other check asks whether the DATA landed.

WHY NO TUPLE COULD EVER FIX IT. The guard composes `table:<input name>` and checks `can_get_metadata`.
`bind86-src/run1` is a bucket prefix. R23 makes raw the external world and never a governed tier, so
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

from typing import Any, cast

import pytest
from lance_namespace import PermissionDeniedError

from lineage.api.fga_deps import enforce_output_authz, is_external_source
from lineage.core.config import LineageSettings
from lineage.models import Dataset, Job, Run, RunEvent
from lineage.schemas import EventRecord
from service_kit.governed import fga
from service_kit.governed.decider import OpenFgaDecider
from service_kit.governed.machine_identity import ServicePrincipal


GOVERNED = "bind86-bronze$pages"
EXTERNAL_NS = "s3://images-batch"
EXTERNAL_NAME = "bind86-src/run1"


def _dataset(namespace: str, name: str) -> Dataset:
    return Dataset(namespace=namespace, name=name)


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
_PIPELINE = ServicePrincipal(service_account="system:serviceaccount:etl:service-pipeline")


class _FakeFGA:
    """An OpenFGA that answers only for objects it was told about — like the real one.

    The DEFAULT IS FALSE and that is the whole point: a real store returns `allowed=false` for an object
    that does not exist, which is indistinguishable from a denial. A fake that raised on an unknown
    object would make the bug impossible to reproduce here.
    """

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.asked: list[str] = []

    async def batch_check(self, _client, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        self.asked.extend(objects)
        return {o: (o in self.allowed) for o in objects}


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
        oidc_connectors={"local": "dex"},
        fga_object_type="table",
    )


@pytest.fixture
def request_with(monkeypatch):
    """Wire a fake FGA into the request's decider, through the `fga` helper it reads."""

    def _make(allowed: set[str]) -> tuple[object, _FakeFGA]:
        fake = _FakeFGA(allowed)

        monkeypatch.setattr(fga, "batch_check", fake.batch_check)

        class _App:
            state = type("S", (), {"decider": OpenFgaDecider(cast("Any", object()))})()

        return type("R", (), {"app": _App()})(), fake

    return _make


@pytest.mark.anyio
async def test_a_GOVERNED_input_the_caller_cannot_see_is_STILL_refused(settings, request_with) -> None:
    """The half the fix must not cost. This is the forgery the guard exists to stop: an authenticated
    reader recording `I read gold$catalog` into the feed."""
    request, _ = request_with(set())
    event = _event(inputs=[_dataset("gold", "gold$catalog")])

    with pytest.raises(PermissionDeniedError, match="can_get_metadata"):
        await enforce_output_authz(event, request, settings, _PIPELINE)


@pytest.mark.anyio
async def test_a_FORGED_external_namespace_cannot_launder_a_governed_name(settings, request_with) -> None:
    """The attack the exemption invites, and why it does not land.

    A caller could name `gold$catalog` under an `s3://` namespace to skip the check. It skips — and buys
    nothing, because a NAMESPACE IS PART OF A DATASET'S IDENTITY. The recorded identity is
    `s3://anything/gold$catalog`, a different one from the governed `gold$catalog`, on no governed table's
    edges. The assertion is on the identity edges are keyed on, not on the check being skipped, because the
    skip is the mechanism and the identity is the guarantee.
    """
    request, _ = request_with(set())
    forged = _dataset("s3://anything", "gold$catalog")
    event = _event(inputs=[forged])

    await enforce_output_authz(event, request, settings, _PIPELINE)

    # ASSERTED ON THE IDENTITY KEY the edges use, never on `(forged.namespace, forged.name)` against a literal,
    # which is unconditionally true and cannot fail.
    governed = _dataset("gold", "gold$catalog")
    assert forged.lineage_name != governed.lineage_name, "the forged input resolved to the governed table's identity — the exemption IS a laundering path"


@pytest.mark.anyio
async def test_OUTPUTS_are_never_exempted(settings, request_with) -> None:
    """Writing is the direction that mutates the estate. An output under an external namespace is a
    producer claiming to have written the outside world — not a case to make permissive, and the
    asymmetry is deliberate rather than an oversight in the filter."""
    request, _ = request_with(set())
    event = _event(outputs=[_dataset(EXTERNAL_NS, EXTERNAL_NAME)])

    with pytest.raises(PermissionDeniedError, match="can_write_data"):
        await enforce_output_authz(event, request, settings, _PIPELINE)


@pytest.mark.anyio
async def test_a_MIXED_event_authorizes_the_governed_input_and_skips_the_external_one(settings, request_with) -> None:
    """A run that reads a raw source AND a governed table must be judged on the governed half only."""
    request, fake = request_with({f"table:{GOVERNED}"})
    event = _event(inputs=[_dataset(EXTERNAL_NS, EXTERNAL_NAME), _dataset("bind86-bronze", GOVERNED)])

    await enforce_output_authz(event, request, settings, _PIPELINE)

    assert f"table:{GOVERNED}" in fake.asked, "the governed input was not authorized"
    assert not any(EXTERNAL_NAME in o for o in fake.asked), "the external input was authorized"


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
    ],
)
def test_the_discriminator_is_a_URI_SCHEME_OR_A_DECLARED_NAMESPACE(namespace: str, name: str, external: bool) -> None:
    """TWO forms, because the estate's producers mint both, and the scheme alone missed two of three.

    A store URI carrying a scheme is external by shape. A bare identifier is exactly what a catalog
    namespace looks like, so the bare external namespaces are DECLARED in
    `service_kit.lakehouse.naming` — the one place both this service and every producer read them
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
# and the whole row is hidden from EVERY caller. Not just from notifications: the lakehouse events
# board reads the same feed.
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
        outputs=[_dataset("bronze", "bronze-media$objects")],
    ).model_dump(by_alias=True)
    # `inputs` is what the feed keeps: the bare NAME, namespace discarded.
    record = _record(inputs=["img.png"], outputs=["bronze-media$objects"], event=payload)

    governed_on = _governed_datasets(record)

    assert "img.png" not in governed_on, "the external source is still being authorized, so every event naming one stays hidden"
    assert "bronze-media$objects" in governed_on, "outputs are never exempted"


def test_a_summary_row_keeps_todays_behaviour() -> None:
    """`summary=true` leaves the payload out of the read, so the namespace genuinely is not there.

    Governing on the bare names is then the only option, and it is what happens today — so this fix
    must not change that path, only the one where the information exists.
    """
    from lineage.api.v1.endpoints.events import _governed_datasets

    record = _record(inputs=["img.png"], outputs=["bronze-media$objects"], event={})

    assert _governed_datasets(record) == {"img.png", "bronze-media$objects"}


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


def _event_with_column_upstream(output: Dataset, up_ns: str, up_name: str) -> RunEvent:
    """A COMPLETE event whose only reference to `up_name` is inside the columnLineage facet."""
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-08-16T12:00:00Z",
            "producer": "rask://test",
            "job": {"namespace": "test", "name": "j"},
            "run": {"runId": "11111111-2222-4333-8444-555555555555"},
            "inputs": [],
            "outputs": [
                {
                    "namespace": output.namespace,
                    "name": output.name,
                    "facets": {"columnLineage": {"fields": {"ssn": {"inputFields": [{"namespace": up_ns, "name": up_name, "field": "ssn"}]}}}},
                }
            ],
        }
    )


@pytest.mark.anyio
async def test_a_GOVERNED_column_upstream_is_AUTHORIZED_not_written_blind(settings, request_with) -> None:
    """The defect: the caller may write `mine$table` and holds nothing on `gold$catalog`, yet naming it
    as a column upstream would record and forward a derivation from it."""
    request, _fake = request_with({"table:mine$table"})  # write on the sandbox only
    event = _event_with_column_upstream(_dataset("mine", "mine$table"), "gold", GOVERNED)

    with pytest.raises(PermissionDeniedError):
        await enforce_output_authz(event, request, settings, _PIPELINE)


@pytest.mark.anyio
async def test_a_column_upstream_the_caller_CAN_see_passes(settings, request_with) -> None:
    """The mirror — the check must not refuse a legitimate cross-table derivation, which is the whole
    point of column lineage."""
    request, fake = request_with({"table:mine$table", f"table:{GOVERNED}"})
    event = _event_with_column_upstream(_dataset("mine", "mine$table"), "gold", GOVERNED)

    await enforce_output_authz(event, request, settings, _PIPELINE)

    assert f"table:{GOVERNED}" in fake.asked, "the governed column upstream was never presented to a check"


@pytest.mark.anyio
async def test_an_EXTERNAL_column_upstream_stays_exempt(settings, request_with) -> None:
    """Same rule as inputs, and for the same reason: an external source has no `table:` object, so
    authorizing it is unsatisfiable rather than strict. Its identity is namespace-qualified, so it cannot
    name a governed table either way."""
    request, _fake = request_with({"table:mine$table"})
    event = _event_with_column_upstream(_dataset("mine", "mine$table"), "s3://raw", "gold$catalog")

    await enforce_output_authz(event, request, settings, _PIPELINE)
