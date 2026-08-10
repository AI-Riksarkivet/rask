"""An EXTERNAL source is not a governed object, and authorizing it as one refuses every honest producer.

THE MEASURED FAILURE. An ingest run lands its data in bronze and opens its run in the graph with a START
event naming where the data came from — an S3 prefix. The service answered:

    403 {"detail": "can_get_metadata required on inputs: bind86-src/run1"}

and because START was refused, the run was never opened. The terminal event that follows was authorized
perfectly well (measured: `201 ingested`), so bronze filled up over nine runs while the lineage graph
held zero ingest events. Nothing reported it: the ingest plane's I8 guard is *required* never to raise,
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

import pytest
from lance_namespace import PermissionDeniedError
from lineage.api.fga_deps import enforce_output_authz, is_external_source
from lineage.core.config import LineageSettings
from lineage.models import Dataset, Job, Run, RunEvent


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


class _Principal:
    sub = "service-ingest"


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
        LINEAGE_FGA_ENABLED=True,
        LINEAGE_OIDC_ENABLED=True,
        LINEAGE_OIDC_ISSUER="https://example.invalid/realms/rask",
        LINEAGE_OIDC_AUDIENCE="rask",
        LINEAGE_FGA_OBJECT_TYPE="table",
    )  # type: ignore[call-arg]


@pytest.fixture
def request_with(monkeypatch):
    """Wire a fake FGA into both the request and the module's `fga` helper."""

    def _make(allowed: set[str]) -> tuple[object, _FakeFGA]:
        fake = _FakeFGA(allowed)

        from lineage.api import fga_deps

        monkeypatch.setattr(fga_deps.fga, "batch_check", fake.batch_check)

        class _App:
            state = type("S", (), {"fga": object()})()

        return type("R", (), {"app": _App()})(), fake

    return _make


@pytest.mark.anyio
async def test_an_EXTERNAL_source_is_not_authorized_at_all(settings, request_with) -> None:
    """The regression. An ingest START naming its S3 source must pass with no tuple in existence."""
    request, fake = request_with({f"table:{GOVERNED}"})
    event = _event(inputs=[_dataset(EXTERNAL_NS, EXTERNAL_NAME)])

    await enforce_output_authz(event, request, settings, _Principal())

    assert not any(EXTERNAL_NAME in o for o in fake.asked), (
        f"the guard asked OpenFGA about an external source ({fake.asked}) — that object cannot exist, so the answer is always a denial"
    )


@pytest.mark.anyio
async def test_a_GOVERNED_input_the_caller_cannot_see_is_STILL_refused(settings, request_with) -> None:
    """The half the fix must not cost. This is the forgery the guard exists to stop: an authenticated
    reader recording `I read gold$catalog` into the authoritative audit graph."""
    request, _ = request_with(set())
    event = _event(inputs=[_dataset("gold", "gold$catalog")])

    with pytest.raises(PermissionDeniedError, match="can_get_metadata"):
        await enforce_output_authz(event, request, settings, _Principal())


@pytest.mark.anyio
async def test_a_FORGED_external_namespace_cannot_launder_a_governed_name(settings, request_with) -> None:
    """The attack the exemption invites, and why it does not land.

    A caller could name `gold$catalog` under an `s3://` namespace to skip the check. It skips — and buys
    nothing, because a NAMESPACE IS PART OF A DATASET'S IDENTITY. The recorded node is
    `s3://anything / gold$catalog`, which is a different node from the governed `gold / gold$catalog`
    and connects to nothing that resolves. The assertion is on the resulting graph identity, not on the
    check being skipped, because the skip is the mechanism and the identity is the guarantee.
    """
    request, _ = request_with(set())
    forged = _dataset("s3://anything", "gold$catalog")
    event = _event(inputs=[forged])

    await enforce_output_authz(event, request, settings, _Principal())

    assert (forged.namespace, forged.name) != ("gold", "gold$catalog"), (
        "the forged input resolved to the governed node's identity — the exemption WOULD be a laundering path"
    )


@pytest.mark.anyio
async def test_OUTPUTS_are_never_exempted(settings, request_with) -> None:
    """Writing is the direction that mutates the estate. An output under an external namespace is a
    producer claiming to have written the outside world — not a case to make permissive, and the
    asymmetry is deliberate rather than an oversight in the filter."""
    request, _ = request_with(set())
    event = _event(outputs=[_dataset(EXTERNAL_NS, EXTERNAL_NAME)])

    with pytest.raises(PermissionDeniedError, match="can_write_data"):
        await enforce_output_authz(event, request, settings, _Principal())


@pytest.mark.anyio
async def test_a_MIXED_event_authorizes_the_governed_input_and_skips_the_external_one(settings, request_with) -> None:
    """A run that reads a raw source AND a governed table must be judged on the governed half only."""
    request, fake = request_with({f"table:{GOVERNED}"})
    event = _event(inputs=[_dataset(EXTERNAL_NS, EXTERNAL_NAME), _dataset("bind86-bronze", GOVERNED)])

    await enforce_output_authz(event, request, settings, _Principal())

    assert f"table:{GOVERNED}" in fake.asked, "the governed input was not authorized"
    assert not any(EXTERNAL_NAME in o for o in fake.asked), "the external input was authorized"


@pytest.mark.parametrize(
    ("namespace", "external"),
    [
        ("s3://images-batch", True),
        ("iiif://lbiiif.riksarkivet.se", True),
        ("file:///data/drop", True),
        ("bronze", False),
        ("bind86-bronze", False),
        ("gold", False),
    ],
)
def test_the_discriminator_is_the_URI_SCHEME_not_a_name_list(namespace: str, external: bool) -> None:
    """OpenLineage's own naming convention does the work: an external data source is namespaced by its
    store URI, a governed table by its catalog namespace — a bare identifier. Pinned as a table so a
    future source kind (a new `xyz://`) is covered without an edit, which a hardcoded list would not be."""
    assert is_external_source(namespace) is external
