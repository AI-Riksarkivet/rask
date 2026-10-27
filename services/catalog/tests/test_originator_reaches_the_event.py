"""A service writing on a person's behalf could not name them, and the field to do it already existed.

`enforce_author` OVERWRITES `author` with the authenticating service's principal key — "never trust the request
body" doing its job — so a catalog write made by a service for a human can never author as the human.
`rask_lance.originator` is the field invented for exactly that, and the emitter carried it end to end:
protocol, no-op, HTTP emitter and the run-event builder all take `originator`.

Nothing could set it. `emit_write_event` is the trailer every catalog write goes through, and it had
no channel for one, so the capability was unreachable from any door. The annotator publishes a
project on a person's behalf with a service bearer; that publish reached the author's own inbox as
the SERVICE and the human's not at all.

The binding is per REQUEST and the emitter is per APP — built once in the lifespan and shared by every
concurrent request. Storing a claim on it would leak one caller's identity onto another caller's
event, which is worse than the silence it replaces: a row in the wrong person's inbox.
"""

from __future__ import annotations

import asyncio
from typing import Any, Unpack

import pytest

from catalog.core.lineage_emit import EmitFields, InputRef, OriginatorBoundEmitter, _BaseLineageEmitter, emit_write_event
from catalog.core.run_context import RunContext
from service_kit.openlineage import DatasetUuids


class _NoUuids:
    """`UuidReader`'s whole signature: a table resolves to the uuid a door passes in, else none."""

    async def table(self, segments: list[str], *, location: str | None = None, uuid: str | None = None) -> DatasetUuids:
        return DatasetUuids(table_uuid=uuid)

    async def branch_identifier(self, segments: list[str], branch: str) -> str | None:
        return None


class _Recording:
    """Structural double for `LineageEmitter` — the estate's fake-by-shape pattern."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def project_for(self, top_ns: str) -> str | None:
        return None

    async def emit_create(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)

    async def emit_write(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)


def _emit(bound: Any) -> None:
    asyncio.run(
        emit_write_event(
            bound,
            ["acme", "silver", "features"],
            author="service-annotator",
            version=3,
            operation="create_table",
            authorization=None,
            uuids=_NoUuids(),
        )
    )


class TestTheBindingCannotCrossRequests:
    def test_two_bindings_over_one_emitter_do_not_see_each_other(self) -> None:
        """The exact hazard that rules out stashing the claim on the app-scoped emitter."""
        inner = _Recording()
        _emit(OriginatorBoundEmitter(inner, "dex~alice"))
        _emit(OriginatorBoundEmitter(inner, "dex~bob"))
        _emit(OriginatorBoundEmitter(inner, None))
        assert [w["originator"] for w in inner.writes] == ["dex~alice", "dex~bob", None]


class TestTheHeaderIsAClaimAndIsBounded:
    """It authorizes nothing — the plane re-derives every recipient's visibility at delivery — so an
    unverified header is sound. It still must not be an arbitrary string: the value becomes an inbox
    actor id."""

    @pytest.mark.parametrize("raw", ["", "a" * 200, "user:alice"])
    def test_a_value_that_is_not_a_person_is_dropped(self, raw: str) -> None:
        from catalog.api.dependencies import originator_hint

        assert originator_hint(raw) is None


# --------------------------------------------------------------------------- #
# CAT-CORE-07 — the optional-metadata keywords are ONE bundle (`EmitFields`), and it must THREAD
# --------------------------------------------------------------------------- #
# `LineageEmitter` is a four-implementation seam, and its optional keywords are a PEP 692
# `Unpack[EmitFields]` that every implementation states by name. A bundle can type cleanly and still
# stop threading a field: an implementation that misses one silently DROPS it, which for `project`
# means an event nobody watching the tenant is told about and for `originator` a row in the wrong
# person's inbox.


class _RecordingEmitter(_BaseLineageEmitter):
    """A real emitter with a recording transport — the bundle must survive the whole chain."""

    _job_namespace = "test"

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def _send(self, event: dict[str, Any], *, operation: str, table_id: str, authorization: str | None) -> None:
        self.events.append(event)


def _optional() -> EmitFields:
    """Every OPTIONAL field, so a dropped one is visible in the emitted event."""
    return EmitFields(
        event_time="2026-10-25T12:00:00+00:00",
        authorization="Bearer x",
        source_uri="s3://bkt/t.lance",
        schema_fields=[],
        inputs=[InputRef("acme", "acme$src", 2)],
        run=RunContext(run_id="pipeline-7", job="nightly"),
        project="acme",
        originator="dex~bob",
    )


async def _create(emitter: Any, **optional: Unpack[EmitFields]) -> None:
    await emitter.emit_create(table_id="acme$t", namespace="acme", author="dex~alice", version=4, **optional)


def test_every_field_in_the_bundle_reaches_the_emitted_event() -> None:
    emitter = _RecordingEmitter()
    asyncio.run(_create(emitter, **_optional()))
    [event] = emitter.events
    facets = event["run"]["facets"]
    assert event["eventTime"] == "2026-10-25T12:00:00+00:00"
    assert facets["rask_lance"]["project"] == "acme"
    assert facets["rask_lance"]["originator"] == "dex~bob"
    assert (facets["rask_lance"]["runId"], facets["rask_lance"]["job"]) == ("pipeline-7", "nightly")
    assert event["inputs"][0]["name"] == "acme$src"
    assert event["outputs"][0]["facets"]["dataSource"]["uri"] == "s3://bkt/t.lance"


def test_the_originator_binding_still_overrides_only_an_absent_claim() -> None:
    inner = _RecordingEmitter()
    asyncio.run(_create(OriginatorBoundEmitter(inner, "dex~carol"), **_optional()))
    assert inner.events[-1]["run"]["facets"]["rask_lance"]["originator"] == "dex~bob", "an explicit originator must win"
    absent = _optional()
    absent["originator"] = None
    asyncio.run(_create(OriginatorBoundEmitter(inner, "dex~carol"), **absent))
    assert inner.events[-1]["run"]["facets"]["rask_lance"]["originator"] == "dex~carol", "the bound claim must fill an absent one"
