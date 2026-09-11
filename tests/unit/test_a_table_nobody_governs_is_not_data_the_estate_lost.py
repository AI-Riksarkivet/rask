"""`storage_loss` means a governed table's data is gone. A table nobody holds a tuple on is not that.

MEASURED ON THE LIVE ESTATE 2026-09-11, and the whole sweep's loss population is this shape. The
reconcile tick's `storage_loss` line names exactly three datasets — `aud1ns$sub3$tt`,
`e2e-ns$t178b2dda`, `probe$nonexistent` — and asking OpenFGA about each one directly returns ZERO
tuples, against a control (`acme-bronze$events`) that returns two. All three carry absolute `s3://`
URIs, so they belong in the loss path rather than the relative-URI class beside it: the bytes really
are gone.

WHAT IS WRONG IS THE NAME. The class's own words are "a bad restore, a wipe... the data is gone; only
a human can answer for it" — a page for a person. A table carrying no authorization tuple cannot be
read, maintained, dropped or re-created by anyone, including whoever made it; its bytes being absent
is not an incident anybody can act on. Splitting `graph_ahead` out of this line fixed 29 of the 32 it
once carried; these three are a SECOND miscategorisation of the same kind, and the remaining 100%.

WHY THE CHECK IS AFFORDABLE, which is what decides where it goes. OpenFGA refuses a `Read` carrying a
`tuple_key` whose object id is empty, so the naive shape is one call per dataset — 356 a tick, the
kind of price that gets an axis switched off. Omitting `tuple_key` ENTIRELY is a different call and
pages the whole store: measured, 51 pages and 5027 tuples in 0.1 s. So the governed set is read once
per sweep and passed in, and an ungoverned dataset costs no storage I/O at all because the check sits
ahead of the read.

AND IT IS REPORTED, NOT SKIPPED. The `dropped` stamp beside it `continue`s silently, which is right
for a deliberate drop. This is not that: an ungoverned table is either residue nobody cleaned up or a
live table that lost its grants, and both are things an operator should be able to see. Folding it
into `storage_loss` to keep it visible would be the mistake this file exists to undo.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from lineage.api import reconcile_cron
from lineage.core.reconcile import reconcile_all
from lineage.schemas import DatasetSummary, ReconcileState, ReconcileStatus


class _Repo:
    """A graph holding one dataset at version 3 with an absolute URI storage cannot find."""

    def __init__(self, name: str) -> None:
        self._name = name

    async def list_datasets(self, namespace: str | None = None, tag: str | None = None) -> list[DatasetSummary]:
        return [DatasetSummary(name=self._name)]

    async def source_uri(self, name: str) -> str | None:
        return f"s3://a-bucket/{name}"

    async def dropped_at(self, name: str) -> str | None:
        return None

    async def latest_write_version(self, name: str) -> int | None:
        return 3

    async def write_versions(self, name: str) -> set[int]:
        return {1, 2, 3}

    async def backfill_write(self, name: str, version: int, schema: object | None = None) -> None:
        raise AssertionError("a dataset absent from storage is not a lost write and must never be back-filled")


async def _absent(_uri: str) -> int | None:
    """Storage says the dataset is not there — the condition that reaches MISSING_ON_STORAGE."""
    return None


def _sweep(*, governed: set[str] | None) -> list[ReconcileStatus]:
    return asyncio.run(reconcile_all(cast(Any, _Repo("probe$nonexistent")), _absent, backfill=True, governed=governed))


def test_a_dataset_with_no_tuples_is_not_reported_as_storage_loss() -> None:
    """THE GATE. Every dataset the live sweep calls loss is one of these."""
    statuses = _sweep(governed=set())

    assert statuses[0].status is ReconcileState.UNGOVERNED, "a table nobody holds a tuple on is not a governed table whose data was lost — it is its own state"


def test_a_governed_dataset_absent_from_storage_is_STILL_loss() -> None:
    """The control, and the reason this is not a guard that cannot fire.

    The whole value of the check is that real loss still pages. A change that quieted the warning for
    everything would pass the gate above and destroy the axis.
    """
    statuses = _sweep(governed={"probe$nonexistent"})

    assert statuses[0].status is ReconcileState.MISSING_ON_STORAGE, "a governed table whose bytes are gone is exactly what this line is for"


def test_a_deployment_that_cannot_read_tuples_classifies_exactly_as_before() -> None:
    """Absent evidence must not become a verdict.

    `governed=None` means nobody asked OpenFGA — FGA off, or a store this sweep could not reach. A
    dataset must then be classified on what IS known, because silently reclassifying every dataset as
    ungoverned on an unreachable store would erase the loss axis at the moment it matters most.
    """
    statuses = _sweep(governed=None)

    assert statuses[0].status is ReconcileState.MISSING_ON_STORAGE


def test_the_report_gives_it_its_own_line_and_keeps_it_out_of_loss() -> None:
    """Visible, not folded. `storage_loss` stays the operator's page for data a person must answer for."""
    report = reconcile_cron.summarize_sweep(
        [
            ReconcileStatus(dataset="residue", in_sync=False, status=ReconcileState.UNGOVERNED),
            ReconcileStatus(dataset="gone", in_sync=False, status=ReconcileState.MISSING_ON_STORAGE),
        ]
    )

    assert report.ungoverned == ["residue"], "an ungoverned dataset is its own finding"
    assert report.storage_loss == ["gone"], "and it must not be counted as data the estate lost"


def test_a_store_that_cannot_be_enumerated_degrades_to_unknown_not_to_empty() -> None:
    """THE CONTROL THAT KEEPS THIS SAFE, and the one an empty-set default would destroy.

    `governed_objects` fails CLOSED by raising rather than returning what it managed to read. If the
    caller turned that into an empty set, every dataset in the estate would classify UNGOVERNED and the
    loss axis would go silent — at exactly the moment the authorization store is in trouble. "We could
    not ask" and "nobody governs anything" are opposite claims, and only the first is true here.
    """

    class _Boom:
        pass

    async def _raise(*_args: object, **_kwargs: object) -> set[str]:
        raise RuntimeError("openfga unreachable")

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=_Boom())))
    settings = SimpleNamespace(fga_enabled=True, fga_object_type="table")

    with mock.patch.object(reconcile_cron.fga, "governed_objects", _raise):
        assert asyncio.run(reconcile_cron.governed_tables(cast(Any, request), cast(Any, settings))) is None


def test_fga_switched_off_asks_nothing_and_says_so() -> None:
    """An estate running without authorization has no governed set to compare against, and must not be
    told that every one of its tables is ungoverned."""
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=object())))
    settings = SimpleNamespace(fga_enabled=False, fga_object_type="table")

    assert asyncio.run(reconcile_cron.governed_tables(cast(Any, request), cast(Any, settings))) is None


def test_an_ungoverned_dataset_costs_no_storage_read() -> None:
    """The check sits AHEAD of the read, and that ordering is load-bearing twice over.

    Correctness: every axis below the read reasons about a table someone owns, so running them against
    one nobody can reach produces findings no person can act on — an ungoverned table reported
    `unreadable` is the same miscategorisation one class over. Cost: residue is the population that
    grows, and it now pays no object-store round-trips at all. Measured 2026-09-11, 11 of the estate's
    swept datasets take this branch.
    """
    reads: list[str] = []

    async def _counting_read(uri: str) -> int | None:
        reads.append(uri)
        return None

    statuses = asyncio.run(reconcile_all(cast(Any, _Repo("probe$nonexistent")), _counting_read, backfill=True, governed=set()))

    assert statuses[0].status is ReconcileState.UNGOVERNED
    assert reads == [], "a table nobody governs is not worth an object-store round-trip, and its answer would change nothing"


def test_the_denominator_does_not_shrink() -> None:
    """`checked` is what every other count is read against, so an axis that quietly stopped counting a
    dataset would make a smaller finding next tick read as progress nobody made — the exact failure the
    truncation warning elsewhere in this sweep exists to prevent. An ungoverned dataset is reported, not
    dropped, so it stays in the denominator."""
    report = reconcile_cron.summarize_sweep(
        [
            ReconcileStatus(dataset="residue", in_sync=False, status=ReconcileState.UNGOVERNED),
            ReconcileStatus(dataset="fine", in_sync=True, status=ReconcileState.IN_SYNC),
        ]
    )

    assert report.checked == 2
