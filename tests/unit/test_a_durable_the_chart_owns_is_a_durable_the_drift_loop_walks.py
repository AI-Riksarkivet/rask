"""A chart-rendered durable whose redelivery config the drift loop expects must be on a stream it walks.

THE FAILURE THIS PREVENTS IS SILENT AND WAS MEASURED ONCE ALREADY. A Dapr jetstream subscription can
only BIND an existing durable whose config matches its request, so after a values change that alters
consumer config the pre-existing durable makes every subscribe fail forever with "consumer name already
in use" while pods stay Ready — a dead subscription with a healthy deployment (hit live 2026-07-13, the
whole cascade idle after a roll). `nats-stream-job.yaml`'s reconcile loop exists to repair exactly that,
by deleting a durable whose max_deliver/backoff differ from the values the pubsub components are
templated with.

IT ONLY REPAIRS THE STREAMS IT WALKS, and that list was shorter than the set of durables the chart
owns. Measured 2026-09-14: the chart renders `medallion-producer-control-durable`
(`dapr-component.yaml:88`) and `notifications-control-durable` (`:139`) on CATALOG_CONTROL, both live
with Active Interest on the deployed estate — while the loop walked `LINEAGE MEDALLION TRAINING DLQ`
only. The stream was excluded on the written ground that "Consumers are EPHEMERAL (no durableName)",
which is true of the catalog's own consumer and false of those two, and `dapr-component.yaml:103`
meanwhile promised the repair ("nats-stream-job reconcile compares (maxDeliver/backOff) and repairs")
that could not happen. Two files asserting opposite things, with the live stream agreeing with neither.

THE RULE IS ABOUT CONFIG, NOT ABOUT LANES. A durable templated from the fleet's
`dapr.resiliency.enabled` conditional is one the loop's EXP comparison understands, so it must be
reconciled. A durable whose backoff is sized by its own WORK is one the comparison would find mismatched
every single run — and deleting a work queue out from under in-flight units is the documented reason
INGEST is excluded. So this asserts both directions: fleet-config durables are walked, bespoke-config
ones are not. Adding a stream to the loop because it "also has durables" is the defect, not the fix.
"""

from __future__ import annotations

import re

import pytest
import yaml

from tests.unit.test_invariants import FAST_LOADER, _helm_template


#: The fleet redelivery values the drift loop templates into EXP_MAXD/EXP_BOFF, per resiliency setting.
_FLEET = {True: ("3", "720s,720s"), False: ("5", "30s,60s,120s,300s")}


def _job_stream_list(rendered: str) -> list[str]:
    """The streams the durable-drift loop walks, read out of the rendered Job script."""
    match = re.search(r"for st in ([A-Z_ ]+); do", rendered)
    assert match, "the drift loop's `for st in …` line is not in the rendered Job — this test is reading the wrong thing"
    return match.group(1).split()


def _durables(rendered: str) -> dict[str, tuple[str | None, str | None]]:
    """``{durableName: (maxDeliver, backOff)}`` for every pubsub component the chart renders."""
    out: dict[str, tuple[str | None, str | None]] = {}
    for doc in yaml.load_all(rendered, Loader=FAST_LOADER):
        if not doc or doc.get("kind") != "Component" or doc.get("spec", {}).get("type") != "pubsub.jetstream":
            continue
        meta = {entry["name"]: str(entry.get("value", "")) for entry in doc["spec"].get("metadata", []) if isinstance(entry, dict) and "name" in entry}
        if "durableName" in meta:
            out[meta["durableName"]] = (meta.get("maxDeliver"), meta.get("backOff"))
    return out


@pytest.mark.parametrize("resiliency", [True, False])
def test_a_durable_carrying_FLEET_redelivery_values_is_on_a_stream_the_loop_walks(resiliency: bool) -> None:
    """The half that was broken. A durable the loop's comparison understands must be reachable by it."""
    rendered = _helm_template("dapr.enabled=true", f"dapr.resiliency.enabled={str(resiliency).lower()}")
    streams = _job_stream_list(rendered)
    fleet = _FLEET[resiliency]

    control = {name: cfg for name, cfg in _durables(rendered).items() if name.endswith("-control-durable")}
    assert control, "the chart renders no *-control-durable — this test's subject is gone, not passing"
    for name, cfg in control.items():
        assert cfg == fleet, (
            f"{name} renders maxDeliver/backOff {cfg}, not the fleet {fleet}. Either it grew its own schedule — in "
            "which case it belongs OUT of the loop like the maintenance lanes — or the templating drifted."
        )
    assert "CATALOG_CONTROL" in streams, (
        f"the chart owns {sorted(control)} on CATALOG_CONTROL with fleet redelivery config, and the drift loop walks "
        f"{streams}. A values change to the resiliency flip would leave those durables mismatched and every subscribe "
        "failing with 'consumer name already in use' while the pods stay Ready."
    )


@pytest.mark.parametrize("resiliency", [True, False])
def test_a_durable_whose_backoff_is_sized_by_its_own_WORK_is_deliberately_NOT_walked(resiliency: bool) -> None:
    """The negative twin, and it is not symmetry for its own sake.

    `maintenance-work-durable` renders `720s,720s,720s,720s` with resiliency OFF against the fleet's
    `30s,60s,120s,300s`. Walking its stream would find a mismatch on EVERY run and delete the work queue
    out from under in-flight units — the documented reason INGEST is excluded. Without this assertion the
    obvious "fix" for the test above is to add every stream to the list, which is a worse defect than the
    one it closes.
    """
    rendered = _helm_template("dapr.enabled=true", f"dapr.resiliency.enabled={str(resiliency).lower()}", "maintenance.workTopic=maintenance.work.unit")
    streams = _job_stream_list(rendered)
    durables = _durables(rendered)

    work = durables.get("maintenance-work-durable")
    assert work is not None, "maintenance-work-durable did not render — the fixture no longer sets up its subject"
    if work != _FLEET[resiliency]:
        assert "MAINTENANCE_WORK" not in streams, (
            f"maintenance-work-durable renders {work}, which the loop's EXP {_FLEET[resiliency]} would call drift — "
            "walking MAINTENANCE_WORK would delete it every run, taking in-flight maintenance units with it"
        )
    assert "INGEST" not in streams, "the ingest work queue is a raw nats-py pull consumer; its config can never match EXP"


def _job_expected_durables(rendered: str) -> list[str]:
    """The durable set the orphan pass spares, read out of the rendered Job script."""
    match = re.search(r'EXP_DURABLES="([^"]*)"', rendered)
    assert match, "the orphan pass's `EXP_DURABLES=` line is not in the rendered Job — [[LH-127]]"
    return match.group(1).split()


@pytest.mark.parametrize("resiliency", [True, False])
def test_the_orphan_pass_spares_EVERY_durable_the_chart_renders(resiliency: bool) -> None:
    """[[LH-127]]. The orphan pass deletes a durable nothing here created; the set it compares against
    therefore has to BE the set the chart creates, exactly.

    Keying on app-ids instead — the form the row originally asked for — is destructive, and measured so
    on 2026-09-16: three chart-owned durables are not named `<app-id>-durable`
    (`lineage-dlq-durable`, `medallion-producer-control-durable`, `notifications-control-durable`), so
    stripping the suffix yields names that appear in no app-id set and the pass deletes the dead-letter
    consumer along with both control lanes. That is the 2026-07-13 dead-subscription failure produced by
    the loop built to prevent it.

    This asserts the two derivations AGREE. A component gaining a durable whose name the helper does not
    emit fails here, rather than at the next release when the pass removes it.
    """
    rendered = _helm_template("dapr.enabled=true", f"dapr.resiliency.enabled={str(resiliency).lower()}")

    assert set(_job_expected_durables(rendered)) == set(_durables(rendered))


def test_the_orphan_pass_spares_a_durable_that_appears_only_under_a_values_flag() -> None:
    """`maintenance-work-durable` renders only when `workTopic` is set, and it is the one durable the
    drift pass must never touch. The orphan pass must not touch it either — for the opposite reason: it
    IS chart-owned, so it is not an orphan, whatever stream it sits on."""
    rendered = _helm_template("dapr.enabled=true", "dapr.resiliency.enabled=false", "maintenance.workTopic=maintenance.work.unit")

    spared = set(_job_expected_durables(rendered))
    assert "maintenance-work-durable" in spared
    assert spared == set(_durables(rendered))


def test_the_orphan_pass_actually_deletes_something_and_is_guarded_by_the_set() -> None:
    """A spare-set with no consumer of it is decoration, and an unguarded delete is the destructive form.

    Anchored on `EXP_DURABLES` rather than on the word "orphan": a first version of this matched
    `MAINTENANCE_ORPHAN_SCAN_ENABLED` from an unrelated template and then any later `consumer rm`, so it
    passed against a chart with no orphan pass at all.
    """
    rendered = _helm_template("dapr.enabled=true", "dapr.resiliency.enabled=false")
    guarded = re.search(r"EXP_DURABLES(.|\n)*?consumer rm", rendered)

    assert guarded, "no `consumer rm` is reached from the EXP_DURABLES guard — the orphan pass removes nothing [[LH-127]]"
    assert "ORPHAN DURABLE" in rendered, "the orphan pass logs no distinguishable line, so an operator cannot tell it from the drift pass"
