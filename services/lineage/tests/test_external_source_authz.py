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
from lineage.schemas import EventRecord


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

    # ASSERTED ON THE VERTEX KEY, which is what the docstring above always claimed and what this
    # assertion never checked: it used to compare `(forged.namespace, forged.name)` — the fixture two
    # lines up — to a different literal, which is unconditionally true and could not fail. The property
    # it names was false the whole time (`MERGE (d:Dataset {name:$name})` keys on NAME ALONE), so the
    # forged input landed on the governed vertex. `vertex_name` is what makes the claim true.
    governed = _dataset("gold", "gold$catalog")
    assert forged.vertex_name != governed.vertex_name, "the forged input resolved to the governed node's identity — the exemption IS a laundering path"


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


# --- THE READ-PATH TWIN --------------------------------------------------------------------------
#
# Everything above is the WRITE path: `enforce_output_authz` exempts an external input, because R23
# says raw is the external world and no tuple could ever be written for it.
#
# The READ path never got the same treatment, and it cannot apply the rule as written: `consumer.py`
# persists `[d.name for d in event.inputs]` — the NAMESPACE is discarded — so by the time `GET /events`
# governs a row it has only bare names, and `is_external_source` needs the namespace. Every event with
# an external input is therefore governed against `table:<bare name>`, which nobody holds a grant on,
# and the whole row is hidden from EVERY caller. Not just from notifications: the lakehouse events
# board reads the same feed.
#
# The namespace is not actually lost — the full event payload is stored alongside, and
# `_column_lineage_datasets` already reads it for exactly this kind of question.


def _record(*, inputs: list[str], outputs: list[str], event: dict) -> EventRecord:
    """A real `EventRecord`, not a stand-in — `_governed_datasets` takes the type the repository yields,
    and a duck-typed double would only prove the helper works on a shape production never sends."""
    return EventRecord(seq=1, inputs=inputs, outputs=outputs, event=event)


def test_the_read_path_can_recover_the_namespace_the_columns_dropped() -> None:
    """The fix's precondition, stated as a test: the payload still knows what the columns forgot."""
    from lineage.api.v1.endpoints.runs import _governed_datasets

    payload = _event(
        inputs=[_dataset("s3://lake/batch", "img.png")],
        outputs=[_dataset("bronze", "bronze-media$objects")],
    ).model_dump(by_alias=True)
    # `inputs` is what consumer.py persists: the bare NAME, namespace discarded.
    record = _record(inputs=["img.png"], outputs=["bronze-media$objects"], event=payload)

    governed_on = _governed_datasets(record)

    assert "img.png" not in governed_on, "the external source is still being authorized, so every event naming one stays hidden"
    assert "bronze-media$objects" in governed_on, "outputs are never exempted"


def test_a_summary_row_keeps_todays_behaviour() -> None:
    """`summary=true` drops the payload at the SQL layer, so the namespace genuinely is not there.

    Governing on the bare names is then the only option, and it is what happens today — so this fix
    must not change that path, only the one where the information exists.
    """
    from lineage.api.v1.endpoints.runs import _governed_datasets

    record = _record(inputs=["img.png"], outputs=["bronze-media$objects"], event={})

    assert _governed_datasets(record) == {"img.png", "bronze-media$objects"}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# THE GUARANTEE, ASSERTED WHERE IT LIVES — the vertex key, not the pydantic object
# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
# `test_a_forged_external_namespace_cannot_launder_a_governed_dataset` above claims in its docstring to
# assert "the resulting graph identity". It does not. It compares the fixture it just built
# (`("s3://anything", "gold$catalog")`) to a different literal, which is unconditionally true and can
# never fail. The guarantee it names was never tested, and it is FALSE:
#
#     repository.py:245   MERGE (d:Dataset {name:$name}) SET d.namespace=$ns
#
# The vertex is keyed on NAME ALONE and the namespace is overwritten, so a forged external input lands
# on the governed vertex and rewrites it — the exact laundering the exemption's docstring says is
# impossible. These tests assert the identity the storage layer actually uses.


def test_a_governed_dataset_keeps_its_bare_name_as_its_vertex_identity() -> None:
    """The half that must NOT change: a governed table's vertex name is its catalog id, which is what
    every read door (`/datasets/<name>/...`), every edge and every existing row already uses."""
    from lineage.models import Dataset

    assert Dataset(namespace="gold", name="gold$catalog").vertex_name == "gold$catalog"


def test_an_EXTERNAL_dataset_is_a_DIFFERENT_VERTEX_from_a_governed_one_of_the_same_name() -> None:
    """THE FIX. The exemption is legitimate — an external source has no catalog entry and therefore no
    tuple that could ever authorize it — but it is only safe if an unauthorized external reference
    cannot reach a governed vertex. Qualifying the external vertex by its namespace makes that
    structural rather than heuristic: no name a caller can choose collides."""
    from lineage.models import Dataset

    forged = Dataset(namespace="s3://anything", name="gold$catalog")
    governed = Dataset(namespace="gold", name="gold$catalog")

    assert forged.vertex_name != governed.vertex_name, "a forged external input still resolves to the governed vertex — the laundering path is open"
    assert governed.vertex_name in forged.vertex_name or "s3://anything" in forged.vertex_name, (
        "the external vertex must remain identifiable, not hashed into noise"
    )


def test_two_external_sources_with_the_same_name_stay_distinct_by_namespace() -> None:
    """The mirror: qualification must not collapse genuinely different sources onto one node either."""
    from lineage.models import Dataset

    a = Dataset(namespace="s3://bucket-a", name="run1")
    b = Dataset(namespace="s3://bucket-b", name="run1")

    assert a.vertex_name != b.vertex_name


def test_the_exemption_is_only_reached_by_a_dataset_that_cannot_be_governed() -> None:
    """`is_external_source` is the discriminator, and it must stay keyed on the URI-scheme convention
    the naming spec already uses — a bare catalog namespace is never exempt."""
    from lineage.api.fga_deps import is_external_source

    assert is_external_source("s3://bucket") is True
    assert is_external_source("iiif://host") is True
    assert is_external_source("gold") is False
    assert is_external_source("bind86-bronze") is False


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# THE AUTHZ SET MUST COVER THE WRITE SET
# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
# `enforce_output_authz` authorizes two sets: `event.outputs`, and non-external `event.inputs`.
# `ingest_event` writes a THIRD: dataset names appearing only inside
# `outputs[].facets.columnLineage.fields[*].inputFields[]`, which it MERGEs as stub vertices and links
# `DERIVED_FROM` edges against (`repository.py` — the `stub_ns` pass).
#
# So a caller holding `can_write_data` on ONE sandbox table can name a GOVERNED dataset as a column
# upstream and have the ingest merge that governed vertex and assert a derivation into it — with the
# governed name never presented to a single FGA check. The gap is not the exemption (these are
# governed namespaces, not external ones); it is that the check never enumerated this set at all.


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
    as a column upstream makes the ingest merge the governed vertex and assert a derivation into it."""
    request, _fake = request_with({"table:mine$table"})  # write on the sandbox only
    event = _event_with_column_upstream(_dataset("mine", "mine$table"), "gold", GOVERNED)

    with pytest.raises(PermissionDeniedError):
        await enforce_output_authz(event, request, settings, _Principal())


@pytest.mark.anyio
async def test_a_column_upstream_the_caller_CAN_see_passes(settings, request_with) -> None:
    """The mirror — the check must not refuse a legitimate cross-table derivation, which is the whole
    point of column lineage."""
    request, fake = request_with({"table:mine$table", f"table:{GOVERNED}"})
    event = _event_with_column_upstream(_dataset("mine", "mine$table"), "gold", GOVERNED)

    await enforce_output_authz(event, request, settings, _Principal())

    assert f"table:{GOVERNED}" in fake.asked, "the governed column upstream was never presented to a check"


@pytest.mark.anyio
async def test_an_EXTERNAL_column_upstream_stays_exempt(settings, request_with) -> None:
    """Same rule as inputs, and for the same reason: an external source has no `table:` object, so
    authorizing it is unsatisfiable rather than strict. Its vertex is namespace-qualified, so it cannot
    reach a governed node either way."""
    request, _fake = request_with({"table:mine$table"})
    event = _event_with_column_upstream(_dataset("mine", "mine$table"), "s3://raw", "gold$catalog")

    await enforce_output_authz(event, request, settings, _Principal())


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# MUTATING AN EXISTING RUN IS AUTHORIZED BY THE DATA THAT RUN WROTE
# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
# `MERGE (r:Run {run_id:$rid})` merges on the run id ALONE and last-event-wins-SETs `event_type`,
# `author`, `producer`, `error_message` and `operation`. Run ids are PUBLIC — `/runs`, `/events` and
# `/producers` serve them, and the in-repo ones are deterministic UUID5 seeds. Both existing authz
# blocks are gated on a non-empty list, so an event naming NO dataset is authorized having checked
# nothing, and still rewrites the run.
#
# Two harms, both silent: set `operation` to `drop_table` and the reconcile sweep skips that dataset
# FOREVER (dropped-ness is derived from the last successful run's operation), so storage loss and
# contract violations on a live governed table stop being reported; or set `eventType: FAIL` and serve
# an attacker-chosen author and error to every authorized viewer of `/datasets/<name>/producers`.
#
# THE CHECK KEYS ON DATA ENTITLEMENT, NOT ON IDENTITY, and that is the load-bearing choice. Author
# equality looks like the obvious rule and is WRONG here: the HTTP door overwrites the author with the
# caller's verified sub (`enforce_author`) while the BUS handler (`on_lineage_event`) applies neither
# that nor this check — it is gated only by the shared Dapr token. So one run's events can legitimately
# carry different author strings depending on which door they arrived through, and an identity rule
# would refuse honest traffic. "May you write what this run wrote" is stable across both doors.
#
# A run that does NOT yet exist is untouched by this: creating a run is harmless, and refusing it would
# re-break the ingest plane's START event, whose only input is an external S3 prefix and which
# therefore authorizes nothing by design.


class _FakeRepo:
    """Answers the one question the guard asks: what did this run already write?"""

    def __init__(self, outputs_by_run: dict[str, list[str]]) -> None:
        self._outputs = outputs_by_run
        self.asked: list[str] = []

    async def run_output_names(self, run_id: str) -> list[str]:
        self.asked.append(run_id)
        return list(self._outputs.get(run_id, []))


def _with_repo(request, repo: _FakeRepo):
    request.app.state.repository = repo
    return request


VICTIM_RUN = "11111111-2222-4333-8444-555555555555"


def _bare_event(run_id: str) -> RunEvent:
    """Names no dataset at all — the shape both existing blocks skip."""
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-08-16T12:00:00Z",
            "producer": "rask://attacker",
            "job": {"namespace": "test", "name": "j"},
            "run": {"runId": run_id, "facets": {"lance": {"operation": "drop_table"}}},
            "inputs": [],
            "outputs": [],
        }
    )


@pytest.mark.anyio
async def test_overwriting_a_run_that_WROTE_a_governed_table_is_refused(settings, request_with) -> None:
    """The attack: a principal with no grants rewrites a run that wrote `gold$catalog`, flipping its
    operation to `drop_table` so the reconcile sweep abandons that dataset permanently."""
    request, _fake = request_with(set())
    _with_repo(request, _FakeRepo({VICTIM_RUN: [GOVERNED]}))

    with pytest.raises(PermissionDeniedError):
        await enforce_output_authz(_bare_event(VICTIM_RUN), request, settings, _Principal())


@pytest.mark.anyio
async def test_a_principal_who_MAY_write_that_table_still_may_amend_its_run(settings, request_with) -> None:
    """The legitimate case this must not cost: the producer that wrote the outputs sends its own
    terminal event for the same run. Keyed on the data, so it passes through EITHER door."""
    request, _fake = request_with({f"table:{GOVERNED}"})
    _with_repo(request, _FakeRepo({VICTIM_RUN: [GOVERNED]}))

    await enforce_output_authz(_bare_event(VICTIM_RUN), request, settings, _Principal())


@pytest.mark.anyio
async def test_a_run_that_does_NOT_yet_exist_is_created_freely(settings, request_with) -> None:
    """Creating a run is harmless — a vertex with no edges is noise, not forgery — and refusing it
    would re-break ingest's START event, whose only input is an external prefix it cannot authorize."""
    request, _fake = request_with(set())
    _with_repo(request, _FakeRepo({}))

    await enforce_output_authz(_bare_event("99999999-2222-4333-8444-555555555555"), request, settings, _Principal())


@pytest.mark.anyio
async def test_an_EXTERNAL_only_START_still_opens_its_run(settings, request_with) -> None:
    """The exact event the exemption was created for, driven end to end: an external input, no outputs,
    a run that does not exist. It must pass, or the ingest plane goes dark again."""
    request, _fake = request_with(set())
    _with_repo(request, _FakeRepo({}))
    event = _event(inputs=[_dataset("s3://raw", "bind86-src/run1")])

    await enforce_output_authz(event, request, settings, _Principal())
