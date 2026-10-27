"""Criterion 4, events are correct, read off one drive of the shared scenario (XC-094, `byo_scenario`).

Delivery durability is proven elsewhere (the outbox and chaos drills); that an event arrives says nothing about
whether what arrived is right. This module captures every event the scenario causes, as the bus stored it and as
lineage's feed serves it, and holds them to six claims:

1. each is the shape its contract writes: an OpenLineage event validates against the 2-0-2 schema, its
   `rask_signature` facet against `spec/facets/rask/RaskSignatureRunFacet.json`, and each `rask_lance` and `rask_author`
   facet, in any facet bag, against `RaskLanceFacet.json` and `RaskAuthorFacet.json` and the `_schemaURL` naming it
   ([[LH-500]]); a control event parses strictly as
   the control envelope (`service_kit.control_events.CatalogControlEvent`);
2. lineage recorded each as the person who called the catalog's door: the author is that person's principal, the
   project is the tenant, and the catalog signed it on their behalf;
3. each reached exactly the people it names (`rask-notifications`);
4. each is recorded once, and a redelivery of the very bytes the bus stored adds no feed row and no inbox row;
5. a forged copy with its signature dropped, on either lane, is recorded by nobody and told to nobody, and the catalog,
   which subscribes to its own control lane, keeps the forged control event out of the feed it serves
   (`GET /management/v1/events`, [[CTL-32]]);
6. nothing the scenario caused, nor the redelivery and forgeries above, parked on the DLQ stream or stayed staged in
   the outbox.

WHAT IS NOT CLAIMED, and why. The edge list's one door,
`/tables/{t}/derived`, answers a set of tables, so a duplicated edge row is invisible there and (4) reads the feed,
which shows every row. Refusing a publish from a client the bus does not admit is the bus's own work (operator mode,
proven by `test_nats_admits_each_client_only_to_its_own_subjects.py`), and both targets run the bus with it off
(XC-159, #650, parked), so (5) rests on the signature alone. The dead-letter counter repeats what the DLQ stream holds
one hop later, in a telemetry store the kind lane does not run, so (6) reads the stream.

The module talks to the bus as an outside client (`bus`): it reads by sequence and publishes as a forger would. Each
publish waits until the consumers that act on its lane have acknowledged it, so an absence asserted afterwards is a
decision those services made, never a delivery still in flight. The redelivery and the forgeries are module fixtures:
each acts once, after the capture that (1) to (3) read, and (6) depends on both, so its window holds them whichever
tests run and in whatever order.

Run with `bash scripts/e2e_live.sh -m phase1 --require-live` against the deployed estate, `make e2e-phase1` with the
addresses set, or the e2e-stack lane, which runs it on every push.
"""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import bus
import byo_scenario as scenario
import pytest
from bus import BusMessage
from byo_scenario import Identity, ScenarioRun, Tenant
from correctness import feed_of, settled_outbox_depth, told
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from service_kit.control_events import CatalogControlEvent
from service_kit.governed.fga import user_object
from service_kit.openlineage import event_identity
from tests.unit.rask_facets import rask_facet_errors


RELEASE = os.environ.get("LANCE_E2E_RELEASE", "rask")
#: The namespace the release runs in; its ServiceAccounts are the services' signing identities.
RELEASE_NAMESPACE = os.environ.get("LANCE_E2E_RELEASE_NAMESPACE", "default")
#: The catalog's signing identity: its ServiceAccount as the cluster's issuer names it ([[LH-474]]).
CATALOG_SIGNER = f"kubernetes~system:serviceaccount:{RELEASE_NAMESPACE}:{RELEASE}-sa-catalog"

LINEAGE_TOPIC = "lineage.events.v1"
CONTROL_TOPIC = "catalog.control.v1"
#: The durable consumers that act on each lane: lineage records the feed, notifications fills the inboxes.
LINEAGE_DURABLES = ("lineage-durable", "notifications-durable")
CONTROL_DURABLES = ("notifications-control-durable",)

_REPO = Path(__file__).resolve().parents[2]
_OPENLINEAGE: dict[str, Any] = json.loads((_REPO / "tests" / "data" / "openlineage-2-0-2.json").read_text())
_SIGNATURE_FACET: dict[str, Any] = json.loads((_REPO / "spec" / "facets" / "rask" / "RaskSignatureRunFacet.json").read_text())
#: The facet schema refers to OpenLineage's `RunFacet` by its published URL, served here from the vendored copy.
_REGISTRY: Registry[Any] = Registry().with_resource(_OPENLINEAGE["$id"], Resource.from_contents(_OPENLINEAGE, default_specification=DRAFT202012))

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.phase1,
    pytest.mark.skipif(not (scenario.LIVE and bus.NATS), reason=f"{scenario.REQUIRES}, and LANCE_E2E_NATS_URL (the bus)"),
]


class Captured(BaseModel):
    """What the drive caused, read once it ended: the feed's rows over its two tables, oldest first, and the
    tenant's messages on each lane as the bus stored them."""

    feed: list[dict[str, Any]]
    lineage: list[BusMessage]
    control: list[BusMessage]


class Redelivery(BaseModel):
    """What publishing silver's insert again changed: the feed rows recorded for it after that publish, and the
    pipeline's inbox rows carrying its notification."""

    notification: str
    landed: list[int]
    in_inbox: list[str]


class Forgeries(BaseModel):
    """Where the two forgeries went: the feed rows recorded for the forged run, the pipeline's inbox rows for either
    forgery, and the catalog's own control feed rows for the forged control event."""

    recorded: list[int]
    told: list[str]
    kept: list[str]


def _kept_by_the_catalog(admin: Identity, *, forged: str, marker: str) -> list[str]:
    """The forged control event's rows in the catalog's control feed, read once the feed holds the binding of
    ``marker``, a namespace the warehouse door made after the forgery was published (`warehouse_bound`, naming it).

    Each catalog replica's broadcast consumer is named by its pod, so the test reads the feed rather than a consumer's
    acknowledgement; the stream reaches it in order, so a feed holding an event published after the forgery has judged the forgery too. The marker
    is also the feed's own control: a subscription refusing every event would hold neither.
    """
    deadline = time.monotonic() + scenario.SETTLE_SECONDS
    while True:
        events = scenario.ok(admin.http.get(f"{scenario.CATALOG}/management/v1/events", params={"since": 0}, timeout=30))["events"]
        if any(event.get("action") == "warehouse_bound" and (event.get("extra") or {}).get("namespace") == marker for event in events):
            return [str(event["event_id"]) for event in events if event.get("event_id") == forged]
        assert time.monotonic() < deadline, f"the catalog's control feed never showed {marker}'s binding within {scenario.SETTLE_SECONDS:.0f} s"
        time.sleep(2)


def _facets(event: dict[str, Any]) -> dict[str, Any]:
    """The facet bag the author and the signature ride: the run's on a RunEvent, the dataset's on a DatasetEvent."""
    carrier = event.get("run") if "run" in event else event.get("dataset")
    facets = carrier.get("facets") if isinstance(carrier, dict) else None
    return facets if isinstance(facets, dict) else {}


def _tables_of(event: dict[str, Any]) -> set[str]:
    """The tables an OpenLineage event says it wrote: its outputs, or the dataset a static change names."""
    named = [item.get("name") for item in event.get("outputs") or [] if isinstance(item, dict)]
    dataset = event.get("dataset")
    if isinstance(dataset, dict):
        named.append(dataset.get("name"))
    return {name for name in named if isinstance(name, str)}


def _commit(row: dict[str, Any]) -> tuple[str, str, int | None]:
    """``(table, operation, version)`` a feed row announces; a static change names no version."""
    facet = scenario.lance_facet(row)
    return (row["outputs"][0], str(facet.get("operation")), facet.get("version"))


def _openlineage_errors(event: dict[str, Any]) -> list[str]:
    definition = "RunEvent" if "run" in event else "DatasetEvent"
    root = {"$ref": f"#/$defs/{definition}", "$defs": _OPENLINEAGE["$defs"], "$id": _OPENLINEAGE["$id"]}
    return [f"{definition}: {error.message}" for error in Draft202012Validator(root, format_checker=FormatChecker()).iter_errors(event)]


def _signature_facet_errors(event: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(_SIGNATURE_FACET, registry=_REGISTRY, format_checker=FormatChecker())
    return [f"rask_signature: {error.message}" for error in validator.iter_errors(_facets(event))]


def _forged(message: BusMessage, event: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """``message``'s envelope carrying ``event`` under a CloudEvent id of its own, and that id."""
    envelope_id = str(uuid.uuid4())
    return {**message.envelope, "id": envelope_id, "data": event}, envelope_id


def _named(event: dict[str, Any]) -> str:
    """What a control event is about, by name: a table's or namespace's name (`object_name`, its `object_id` names its
    uuid), any other object by its id (`project:<id>`)."""
    return str(event.get("object_name") or event.get("object_id"))


def _grant_messages(captured: Captured, person: Identity) -> dict[str, BusMessage]:
    """The `grant_added` messages naming ``person``, by the name of the object each grant is on."""
    subject = user_object(person.principal)
    return {
        _named(message.event): message
        for message in captured.control
        if message.event.get("action") == "grant_added" and (message.event.get("extra") or {}).get("subject") == subject
    }


@pytest.fixture(scope="module")
def captured(byo_tenant: Tenant, byo_run: ScenarioRun) -> Captured:
    tables = {byo_run.bronze_table, byo_run.silver_table}
    lineage = [message for message in bus.messages_since("LINEAGE", byo_tenant.created_at) if _tables_of(message.event) & tables]
    control = [message for message in bus.messages_since("CATALOG_CONTROL", byo_tenant.created_at) if byo_tenant.project in _named(message.event)]
    return Captured(feed=feed_of(byo_tenant.pipeline, byo_run), lineage=lineage, control=control)


@pytest.fixture(scope="module")
def redelivery(captured: Captured, byo_tenant: Tenant, byo_run: ScenarioRun) -> Redelivery:
    """Silver's insert, published again as the bus stored it: signature and CloudEvent id intact, without the
    `Nats-Msg-Id` the stream would drop it on, so lineage and notifications each receive it a second time. That is
    the at-least-once path every consumer takes when the outbox relay re-publishes or a broker redelivers."""
    pipeline = byo_tenant.pipeline
    insert = [row for row in captured.feed if _commit(row) == (byo_run.silver_table, "insert", 2)]
    assert len(insert) == 1, f"the feed holds {len(insert)} rows for silver's insert"
    identity = event_identity(insert[0]["event"])
    stored = [message for message in captured.lineage if event_identity(message.event) == identity]
    assert len(stored) == 1, f"the bus holds {len(stored)} messages for silver's insert"

    cursor = scenario.feed_start(pipeline)
    seq = bus.publish(LINEAGE_TOPIC, stored[0].envelope, msg_id=None)
    bus.wait_until_acked("LINEAGE", LINEAGE_DURABLES, seq, within=scenario.SETTLE_SECONDS)
    notification = f"{identity}@COMPLETE"
    landed = scenario.read_forward(pipeline, since=cursor, table=byo_run.silver_table)
    return Redelivery(
        notification=notification,
        landed=[int(row["seq"]) for row in landed if event_identity(row["event"]) == identity],
        in_inbox=[row["notification_id"] for row in pipeline.inbox(since=byo_tenant.created_at) if row["notification_id"] == notification],
    )


@pytest.fixture(scope="module")
def forgeries(captured: Captured, byo_tenant: Tenant, byo_run: ScenarioRun) -> Forgeries:
    """Each lane gets one forgery of a real message: the same bytes under an id of its own, so no dedupe can hide
    it, with the `rask_signature` the catalog put on it removed. The silver insert claims the pipeline's authority to
    write silver; the pipeline's bronze grant names the pipeline as its subject."""
    pipeline = byo_tenant.pipeline
    insert = [
        message
        for message in captured.lineage
        if _tables_of(message.event) == {byo_run.silver_table} and _facets(message.event).get("rask_lance", {}).get("operation") == "insert"
    ]
    grant = _grant_messages(captured, pipeline).get(byo_tenant.bronze)
    assert insert and grant, "the capture holds no silver insert or no grant to the pipeline to forge"

    forged_run = str(uuid.uuid4())
    run_event = copy.deepcopy(insert[0].event)
    run_event["run"]["runId"] = forged_run
    del run_event["run"]["facets"]["rask_signature"]
    forged_grant = uuid.uuid4().hex
    control_event = {key: value for key, value in grant.event.items() if key != "rask_signature"} | {"event_id": forged_grant}

    cursor = scenario.feed_start(pipeline)
    lineage_envelope, lineage_id = _forged(insert[0], run_event)
    control_envelope, control_id = _forged(grant, control_event)
    lineage_seq = bus.publish(LINEAGE_TOPIC, lineage_envelope, msg_id=lineage_id)
    control_seq = bus.publish(CONTROL_TOPIC, control_envelope, msg_id=control_id)
    bus.wait_until_acked("LINEAGE", LINEAGE_DURABLES, lineage_seq, within=scenario.SETTLE_SECONDS)
    bus.wait_until_acked("CATALOG_CONTROL", CONTROL_DURABLES, control_seq, within=scenario.SETTLE_SECONDS)
    # A namespace no inbox reads and the tenant's teardown removes with its warehouse.
    marker = f"{byo_tenant.project}-marker"
    body = {"namespace": marker, "properties": {"rask.tier": "bronze"}}
    scenario.ok(byo_tenant.admin.http.post(f"{scenario.CATALOG}/management/v1/warehouses/{byo_tenant.project}/namespaces", json=body))
    kept = _kept_by_the_catalog(byo_tenant.admin, forged=forged_grant, marker=marker)
    named = {f"{forged_run}@COMPLETE", f"{forged_grant}@GRANT_ADDED"}
    return Forgeries(
        recorded=[
            int(row["seq"]) for row in scenario.read_forward(pipeline, since=cursor, table=byo_run.silver_table) if event_identity(row["event"]) == forged_run
        ],
        told=[row["notification_id"] for row in pipeline.inbox(since=byo_tenant.created_at) if row["notification_id"] in named],
        kept=kept,
    )


def test_every_event_is_the_shape_its_contract_writes(captured: Captured) -> None:
    published = [(f"bus {message.stream} #{message.seq}", message.event) for message in captured.lineage]
    served = [(f"feed #{row['seq']}", row["event"]) for row in captured.feed]
    errors = [
        f"{where}: {error}"
        for where, event in published + served
        for error in _openlineage_errors(event) + _signature_facet_errors(event) + rask_facet_errors(event)
    ]
    for message in captured.control:
        try:
            CatalogControlEvent.model_validate_json(json.dumps(message.event), strict=True)
        except ValidationError as exc:
            errors.append(f"bus {message.stream} #{message.seq}: {exc}")

    assert published and served and captured.control, "the capture holds no event to judge"
    assert errors == []


def test_lineage_recorded_each_event_as_the_person_who_called_the_catalogs_door(captured: Captured, byo_tenant: Tenant, byo_run: ScenarioRun) -> None:
    """The pipeline made every commit but one; the validator's approval moved `published` to silver's second version.
    The catalog signs as itself on behalf of the person it authenticated, so its signature names that person."""
    pipeline, validator = byo_tenant.pipeline.principal, byo_tenant.validator.principal
    people = {f"user:{identity.principal}" for identity in (byo_tenant.admin, byo_tenant.pipeline, byo_tenant.validator)}

    def stamp(event: dict[str, Any], project: object) -> dict[str, object]:
        facets = _facets(event)
        signature = facets.get("rask_signature") or {}
        return {
            "author": (facets.get("rask_author") or {}).get("sub"),
            "project": project,
            "signer": signature.get("identity"),
            "for": signature.get("onBehalfOf"),
        }

    def expected(row: dict[str, Any]) -> dict[str, object]:
        who = validator if _commit(row) == (byo_run.silver_table, "publish_table", 2) else pipeline
        return {"author": who, "project": byo_tenant.project, "signer": CATALOG_SIGNER, "for": who}

    recorded = {row["seq"]: stamp(row["event"], scenario.lance_facet(row).get("project")) for row in captured.feed}
    announced = {
        message.seq: (
            message.event.get("actor") in people,
            (message.event.get("rask_signature") or {}).get("identity"),
            (message.event.get("rask_signature") or {}).get("onBehalfOf"),
        )
        for message in captured.control
    }

    assert recorded == {row["seq"]: expected(row) for row in captured.feed}
    assert announced == {message.seq: (True, CATALOG_SIGNER, message.event.get("actor")) for message in captured.control}


def test_each_person_the_scenario_names_is_told_exactly_their_part(captured: Captured, byo_tenant: Tenant, byo_run: ScenarioRun) -> None:
    """The pipeline hears of each commit it made and of its two grants. The admin hears that it was made the
    project's admin, and of the one run it watched for, the approval. The validator hears of its grant only: its
    `validator` grant lets it promote silver, not read it, and delivery tells an author only of what it may see
    (`can_be_notified`, a reader's)."""
    tenant = byo_tenant

    def grant(person: Identity, named: str) -> tuple[str, str, str]:
        message = _grant_messages(captured, person).get(named)
        return (f"{message.event['event_id']}@GRANT_ADDED" if message else "no grant_added event names this", "grant_added", named)

    def run(row: dict[str, Any], reason: str) -> tuple[str, str, str]:
        return (f"{event_identity(row['event'])}@COMPLETE", reason, _commit(row)[0])

    authored = [row for row in captured.feed if (_facets(row["event"]).get("rask_author") or {}).get("sub") == tenant.pipeline.principal]
    approval = [row for row in captured.feed if _commit(row) == (byo_run.silver_table, "publish_table", 2)]
    expected = {
        "admin": sorted([grant(tenant.admin, f"project:{tenant.project}"), *(run(row, "watch") for row in approval)]),
        "pipeline": sorted(
            [
                grant(tenant.pipeline, tenant.bronze),
                grant(tenant.pipeline, tenant.silver),
                *(run(row, "author") for row in authored),
            ]
        ),
        "validator": sorted([grant(tenant.validator, tenant.silver)]),
    }

    deadline = time.monotonic() + scenario.SETTLE_SECONDS
    while True:
        heard = told(tenant, byo_run)
        if all(set(expected[part]) <= set(heard[part]) for part in tenant.people) or time.monotonic() > deadline:
            break
        time.sleep(3)

    assert heard == expected


def test_each_event_is_recorded_once_and_a_redelivery_of_its_bytes_adds_nothing(captured: Captured, byo_run: ScenarioRun, redelivery: Redelivery) -> None:
    keys = Counter((event_identity(row["event"]), row["event_type"]) for row in captured.feed)
    commits = {
        table: [(operation, version) for named, operation, version in map(_commit, captured.feed) if named == table]
        for table in (byo_run.bronze_table, byo_run.silver_table)
    }

    assert [key for key, count in keys.items() if count > 1] == []
    assert commits == {
        byo_run.bronze_table: [("declare_table", None), ("create_table", 1), ("insert", 2)],
        byo_run.silver_table: [("create_table", 1), ("publish_table", 1), ("insert", 2), ("publish_table", 2), ("insert", 3)],
    }
    assert (redelivery.landed, redelivery.in_inbox) == ([], [redelivery.notification])


def test_a_forged_copy_with_its_signature_dropped_is_recorded_and_told_to_nobody(forgeries: Forgeries) -> None:
    assert (forgeries.recorded, forgeries.told, forgeries.kept) == ([], [], [])


@pytest.mark.usefixtures("byo_run", "redelivery", "forgeries")
def test_nothing_the_scenario_caused_parked_and_the_outbox_drained(byo_tenant: Tenant) -> None:
    """The window opens at the tenant's creation and holds the drive, the redelivery and the forgeries, which this
    test depends on. The outbox depth is the estate's: the set the relay drains, each a committed write not yet
    confirmed delivered."""
    parked = [
        (message.seq, message.subject, sorted(_tables_of(message.event)) or _named(message.event))
        for message in bus.messages_since("DLQ", byo_tenant.created_at)
    ]
    depth = settled_outbox_depth(byo_tenant.admin)

    assert (parked, depth) == ([], 0)
