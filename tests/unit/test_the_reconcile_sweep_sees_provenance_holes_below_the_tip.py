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

from lineage.api import reconcile_cron
from lineage.core.config import LineageSettings
from lineage.core.reconcile import read_storage_versions, read_version_operations, reconcile_all
from lineage.schemas import DatasetSummary


class _HoledRepo:
    """A graph holding WROTE edges per ref — main's under ``None``, each branch's under its name.

    Ref-keyed the way the graph is: a branch write's edge carries ``ref``, and ``write_versions(name, ref)``
    answers only that ref's versions, exactly as `cypher.WRITE_VERSIONS` / `BRANCH_WRITE_VERSIONS` do.
    """

    def __init__(self, *, graph_versions: set[int], uri: str, branch_versions: dict[str, set[int]] | None = None) -> None:
        self._graph_versions = graph_versions
        self._branches = branch_versions or {}
        self._uri = uri
        self.backfilled: list[tuple[str, int]] = []
        self.backfilled_on_branches: list[tuple[str, int, str]] = []

    async def list_datasets(self, namespace: str | None = None, tag: str | None = None) -> list[DatasetSummary]:
        return [DatasetSummary(name="db$t")]

    async def source_uri(self, name: str) -> str | None:
        return self._uri

    async def dropped_at(self, name: str) -> str | None:
        return None

    async def latest_write_version(self, name: str) -> int | None:
        return max(self._graph_versions) if self._graph_versions else None

    async def record_observed_drop(self, name: str, uri: str, observed_at: str) -> bool:
        return True

    async def write_versions(self, name: str, ref: str | None = None) -> set[int]:
        return set(self._graph_versions if ref is None else self._branches.get(ref, set()))

    async def backfill_write(self, name: str, version: int, schema: object | None = None, ref: str | None = None) -> None:
        if ref is None:
            self.backfilled.append((name, version))
            self._graph_versions.add(version)
        else:
            self.backfilled_on_branches.append((name, version, ref))
            self._branches.setdefault(ref, set()).add(version)


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


def test_a_lost_event_is_found_and_back_filled_on_main_and_on_each_branch(tmp_path: Path) -> None:
    """THE GATE, through one cron tick over real Lance: a lost event is recovered on whichever ref it was lost.

    Main: graph tip == storage tip, so the version axis says in_sync — and version 2 has no edge, which
    only the hole axis can see.

    Branch ([[LH-282]]): under D3 every table writer writes branches, and a branch keeps its own version
    sequence under `tree/<branch>/` (lance_docs/file_format.md, Branch Dataset Layout) that main's
    `versions()` never lists. Branch `b` is cut at main v3 (its creation manifest is numbered 3 and is the
    parent's state, not a write), written at 4 with its event recorded, then at 5 with its event lost.
    A sweep that reads main's axis alone leaves version 5 of `b` unattributed for good, and no gauge
    counts it.

    Asserted on the tick's REPORT and the back-fill together: a report naming a hole while leaving it
    unrecovered would be a second control that cannot fire, and a back-fill that landed the branch's
    version on main would answer for main's version 5 — a commit that does not exist.
    """
    uri = _four_version_dataset(tmp_path)
    branch = lance.dataset(uri).checkout_version(3).create_branch("b")
    branch = lance.write_dataset(pa.table({"id": [30]}), branch, mode="append")
    lance.write_dataset(pa.table({"id": [31]}), branch, mode="append")
    repo = _HoledRepo(graph_versions={1, 3, 4}, uri=uri, branch_versions={"b": {4}})

    report = reconcile_cron.summarize_sweep(asyncio.run(reconcile_cron._sweep(cast(Any, repo), LineageSettings(), {})))

    assert report.provenance_holes == {"db$t": {"main": [2], "b": [5]}}, "each ref's lost write is reported against that ref"
    assert repo.backfilled == [("db$t", 2)], "main's hole must be RECOVERED on main"
    assert repo.backfilled_on_branches == [("db$t", 5, "b")], "the branch's hole must be RECOVERED on the branch, at the branch's version"


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

    async def read_versions(_uri: str, _ref: str | None) -> list[int] | None:
        return [3, 4]  # versions 1 and 2 reclaimed by a cleanup pass

    statuses = asyncio.run(reconcile_all(cast(Any, repo), read_version, backfill=True, read_versions=read_versions))

    assert statuses[0].versions_without_lineage == []
    assert repo.backfilled == []


# --- The classifier: which holes are REAL ---------------------------------------------------- #


def test_read_version_operations_names_the_transaction_behind_each_version(tmp_path: Path) -> None:
    """The reader, against real Lance: a create is an `Overwrite`, an append is an `Append`.

    Asserted on a real dataset rather than a double, because the whole classification rests on
    `type(op).__name__` matching the names :data:`MAINTENANCE_OPERATIONS` is written against — a pylance
    upgrade that renamed one would make the denylist silently stop matching, and a mocked transaction
    would never notice.
    """
    uri = _four_version_dataset(tmp_path)
    operations = read_version_operations(uri, {}, [1, 2])
    assert operations == {1: "Overwrite", 2: "Append"}
    assert read_version_operations(str(tmp_path / "missing.lance"), {}, [1]) == {1: None}


def test_a_maintenance_version_is_not_a_provenance_hole(tmp_path: Path) -> None:
    """A compaction, an index build and a config change commit a version and emit NO lineage, correctly.

    Measured on the live estate 2026-09-11: without this, 2 of the 10 holes the axis found were a
    `CreateIndex` and a maintenance version on `transcripts_v2$annotations` — 20% of the finding was
    noise, and a finding an operator learns to skim past has the same value as no finding at all.
    """
    uri = _four_version_dataset(tmp_path)
    repo = _HoledRepo(graph_versions={1, 4}, uri=uri)

    async def read_version(_uri: str) -> int | None:
        return 4

    async def read_versions(_uri: str, _ref: str | None) -> list[int] | None:
        return [1, 2, 3, 4]

    async def read_operations(_uri: str, versions: list[int], _ref: str | None) -> dict[int, str | None]:
        return {2: "Rewrite", 3: "Update"}  # 2 compacted, 3 is a real write that lost its event

    statuses = asyncio.run(reconcile_all(cast(Any, repo), read_version, backfill=True, read_versions=read_versions, read_operations=read_operations))

    assert statuses[0].versions_without_lineage == [3]
    assert repo.backfilled == [("db$t", 3)], "the compaction must not be attributed to a writer"
