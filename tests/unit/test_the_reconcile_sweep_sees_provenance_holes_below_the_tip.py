"""A lost lineage event is recovered only if it was the LATEST write — and that is the hole.

`reconcile` compares two MAXIMA: the newest version the graph holds a `WROTE` edge for against the
newest version on disk. A write whose event was lost and which another write then superseded leaves
`storage_version == graph_version`, so the sweep reports `in_sync` and the back-fill never runs. The
provenance of that version is gone permanently — nothing else in the estate looks for it.

MEASURED ON THE LIVE ESTATE 2026-09-11, which is why this is a test and not a worry:

* `bronze$events` answered `{"in_sync": true, "graph_version": 87, "storage_version": 87}` while its
  retained versions **76 (`Overwrite`), 80, 82 and 83 (`Update`)** carried no lineage event at all.
* `transcripts_v2$annotations` answered `{"in_sync": true, "graph_version": 7, "storage_version": 7}`
  with versions 1, 2 and 4 un-provenanced.

Eight of twenty-nine retained data-operation versions across the estate held no provenance, under a
reconciler reporting perfect health. That is the estate's signature anti-pattern in its purest form: a
control that exists, is argued for at length in its own docstring ("flag drift — a write that bypassed
lineage"), and cannot fire on the case it was built for, because a maximum cannot see a hole beneath it.

THE HOLE IS REACHABLE WITHOUT ANY CATALOG DOOR, which is what makes it a provenance property rather than
an endpoint bug. A write-tier vended STS credential grants `PutObject` on `<table-prefix>/*`, and
`core/vending.py:148-155` states that this "also covers ``_versions/``" — so the credential holder can
commit a whole Lance version client-side, manifest included. Driven 2026-09-11 against the deployed
catalog: a vended write credential appended version 2 to a governed table; `/history` reported
`{"version": 2, "operation": "Append"}` and the lineage graph held one event, for version 1.

WHAT IS DELIBERATELY NOT ASSERTED HERE: that `in_sync` turns False. Every other axis this module grew —
`stale`, `dangling_blob_columns`, `missing_declared_columns` — reports on its own field and leaves
`in_sync` version-based, because they answer different questions and collapsing them loses the one an
operator acted on. This axis follows that precedent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa

from lineage.core.reconcile import read_storage_versions, reconcile_all
from lineage.schemas import DatasetSummary


class _HoledRepo:
    """A graph that recorded versions 1 and 3 of a four-version dataset — version 2's event was lost."""

    def __init__(self, *, graph_versions: set[int], uri: str) -> None:
        self._graph_versions = graph_versions
        self._uri = uri
        self.backfilled: list[tuple[str, int]] = []

    async def list_datasets(self, namespace: str | None = None, tag: str | None = None) -> list[DatasetSummary]:
        return [DatasetSummary(name="db$t")]

    async def source_uri(self, name: str) -> str | None:
        return self._uri

    async def dropped_at(self, name: str) -> str | None:
        return None

    async def latest_write_version(self, name: str) -> int | None:
        return max(self._graph_versions) if self._graph_versions else None

    async def write_versions(self, name: str) -> set[int]:
        return set(self._graph_versions)

    async def backfill_write(self, name: str, version: int, schema: object | None = None) -> None:
        self.backfilled.append((name, version))
        self._graph_versions.add(version)


def _four_version_dataset(tmp_path: Path) -> str:
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": [1]}), uri)
    for value in (2, 3, 4):
        lance.write_dataset(pa.table({"id": [value]}), uri, mode="append")
    return uri


def test_read_storage_versions_returns_every_retained_version(tmp_path: Path) -> None:
    """The reader the axis needs: the version SET on disk, not just its maximum.

    ONE listing of the manifest directory, which is what `read_latest_write_age_hours` already pays on
    the freshness axis — so an estate running both reads the same directory once per axis, never once
    per version. A missing dataset reads as `None`, the same absence the version axis already classifies.
    """
    uri = _four_version_dataset(tmp_path)
    assert read_storage_versions(uri, {}) == [1, 2, 3, 4]
    assert read_storage_versions(str(tmp_path / "missing.lance"), {}) is None


def test_a_lost_event_below_the_tip_is_found_and_back_filled(tmp_path: Path) -> None:
    """THE GATE. Graph tip == storage tip, so the version axis says in_sync — and version 2 has no edge.

    Before this axis existed the sweep returned `in_sync=True` with an empty finding set and called it a
    healthy dataset. The assertion that matters is `backfilled`: a report naming the hole while leaving
    it unrecovered would be a second control that cannot fire.
    """
    uri = _four_version_dataset(tmp_path)
    repo = _HoledRepo(graph_versions={1, 3, 4}, uri=uri)

    async def read_version(_uri: str) -> int | None:
        return 4

    async def read_versions(_uri: str) -> list[int] | None:
        return [1, 2, 3, 4]

    statuses = asyncio.run(reconcile_all(cast(Any, repo), read_version, backfill=True, read_versions=read_versions))

    assert len(statuses) == 1
    status = statuses[0]
    assert status.graph_version == 4
    assert status.storage_version == 4
    assert status.in_sync is True, "the version axis is unchanged — the hole is its own axis, like stale"
    assert status.versions_without_lineage == [2], "version 2 exists on disk and the graph has no WROTE edge for it"
    assert repo.backfilled == [("db$t", 2)], "the hole must be RECOVERED, not merely reported"


def test_a_reclaimed_version_the_graph_still_remembers_is_not_a_hole(tmp_path: Path) -> None:
    """The comparison is ONE-DIRECTIONAL, and it has to be: `cleanup_old_versions` reclaims manifests.

    After a reclamation the graph legitimately holds versions storage no longer has. Reporting those as
    holes would make every maintained dataset in the estate permanently red, which is the failure mode
    that gets an axis switched off — so only `on_disk - in_graph` is a finding.
    """
    uri = _four_version_dataset(tmp_path)
    repo = _HoledRepo(graph_versions={1, 2, 3, 4}, uri=uri)

    async def read_version(_uri: str) -> int | None:
        return 4

    async def read_versions(_uri: str) -> list[int] | None:
        return [3, 4]  # versions 1 and 2 reclaimed by a cleanup pass

    statuses = asyncio.run(reconcile_all(cast(Any, repo), read_version, backfill=True, read_versions=read_versions))

    assert statuses[0].versions_without_lineage == []
    assert repo.backfilled == []


def test_the_axis_is_off_when_no_reader_is_injected(tmp_path: Path) -> None:
    """Off by default, like every other optional axis here — a deployment that does not wire the reader
    pays no extra listing and reports no phantom holes."""
    uri = _four_version_dataset(tmp_path)
    repo = _HoledRepo(graph_versions={1, 3, 4}, uri=uri)

    async def read_version(_uri: str) -> int | None:
        return 4

    statuses = asyncio.run(reconcile_all(cast(Any, repo), read_version, backfill=True))

    assert statuses[0].versions_without_lineage == []
    assert repo.backfilled == []


def test_an_unreadable_dataset_is_never_probed_for_holes(tmp_path: Path) -> None:
    """A dataset that could not be OPENED has no version set to compare, so the hole probe must not run.

    The same rule the freshness and declared-column axes follow: an unreadable dataset is already the
    version check's finding, and a second reading of the same absence is how one outage becomes three
    unrelated-looking alarms.
    """
    repo = _HoledRepo(graph_versions={1, 3, 4}, uri="s3://b/gone")
    probed: list[str] = []

    async def read_version(_uri: str) -> int | None:
        return None  # absent on storage

    async def read_versions(uri: str) -> list[int] | None:
        probed.append(uri)
        return None

    statuses = asyncio.run(reconcile_all(cast(Any, repo), read_version, backfill=True, read_versions=read_versions))

    assert probed == []
    assert statuses[0].versions_without_lineage == []
    assert repo.backfilled == []
