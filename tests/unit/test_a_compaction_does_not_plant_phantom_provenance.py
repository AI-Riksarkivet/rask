"""The sweep may REPORT a version it cannot name; it must never FABRICATE a run that wrote it.

MEASURED, NOT ENUMERATED — and the distinction is the whole point of this file. One
`dataset.optimize.compact_files()` on a four-version table commits **two** versions, not one:

    v5  BaseOperation   total_rows 4, total_fragments 4   (identical to v4)
    v6  Rewrite         total_rows 4, total_fragments 1   (the actual compaction)

`MAINTENANCE_OPERATIONS` holds `{"Rewrite", "CreateIndex", "UpdateConfig"}`, so v6 is correctly
excluded and **v5 is not**. `_recover_holes` keeps it as a provenance hole and `backfill_write` stamps
a `WROTE` edge saying a run wrote it. Every compaction in the estate plants one phantom.

WHY `BaseOperation` IS NOT AN OPERATION NAME. It is the ABC that every modelled operation inherits
(`lance/dataset.py:5905`); pylance models twelve concrete subclasses, and `type(op).__name__` returns
the ABC's own name when the Rust side commits an operation none of them covers. So the classifier was
not reading an operation called "BaseOperation" — it was reading the word "unknown" and treating it as
a data write. That is the estate's classify-by-spelling shape: deciding a version's nature from the
text of a class name rather than from what the version did.

THE FIX IS A DIRECTION, NOT A LONGER LIST. Adding `"BaseOperation"` to the maintenance set would be
the same mistake once more, and worse: it would silently swallow a genuine future Lance data operation
this binding does not model, losing real provenance to keep a report quiet. Instead the two outcomes
`_recover_holes` had collapsed are separated — a version whose operation cannot be NAMED as a data
operation is still reported, because an operator should see it, and is never back-filled, because
claiming a run wrote it is a fabrication the graph cannot distinguish from a real event afterwards.

AND UNKNOWN-BUT-INERT IS NOT A HOLE AT ALL, which is what keeps the report from going permanently red
on every compacted dataset. `ds.versions()` already carries each version's `total_rows`,
`total_data_files`, `total_deletion_files` and `total_deletion_file_rows`, and
`read_storage_versions` already makes that call — so a version whose counters are identical to its
predecessor's is provably inert at zero extra I/O. v5 is exactly that. Any counter moving keeps the
version reportable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa

from lineage.core.reconcile import INERT_UNKNOWN, read_storage_versions, read_version_operations, reconcile_all
from lineage.schemas import DatasetSummary


class _Repo:
    """A graph holding a WROTE edge for every version that existed before the compaction."""

    def __init__(self, *, graph_versions: set[int], uri: str) -> None:
        self._graph_versions = graph_versions
        self._uri = uri
        self.backfilled: list[int] = []

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
        self.backfilled.append(version)
        self._graph_versions.add(version)


def _compacted_dataset(tmp_path: Path) -> str:
    """Four appends then a real compaction — never a hand-written operation name.

    The defect this pins survived a fix precisely because no test ever compacted anything, so the
    operations the classifier was written against were the ones someone remembered rather than the
    ones Lance emits.
    """
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": [1]}), uri)
    for value in (2, 3, 4):
        lance.write_dataset(pa.table({"id": [value]}), uri, mode="append")
    lance.dataset(uri).optimize.compact_files()
    return uri


def test_a_real_compaction_commits_a_version_no_subclass_models(tmp_path: Path) -> None:
    """The measurement the rest of this file rests on, so a Lance upgrade that changes it says so here."""
    uri = _compacted_dataset(tmp_path)
    versions = read_storage_versions(uri, {})

    assert versions is not None, "the dataset this test just wrote must be readable"
    assert versions == [1, 2, 3, 4, 5, 6], "one compaction commits TWO versions on top of the four writes"
    operations = read_version_operations(uri, {}, versions)
    assert operations[6] == "Rewrite", "the compaction itself is the modelled operation"
    assert operations[5] == INERT_UNKNOWN, (
        "the version a compaction commits before its Rewrite is unnameable AND provably inert — identical "
        "counters to the version below it — so it must read as that, never as an operation literally named "
        "'BaseOperation', which is the ABC's own name and not a name the classifier may branch on"
    )


def test_the_inert_version_a_compaction_commits_is_not_a_provenance_hole(tmp_path: Path) -> None:
    """THE GATE. Before this, every compaction in the estate planted one fabricated `WROTE` edge."""
    uri = _compacted_dataset(tmp_path)
    repo = _Repo(graph_versions={1, 2, 3, 4}, uri=uri)

    async def read_version(_uri: str) -> int | None:
        return 6

    async def read_versions(_uri: str) -> list[int] | None:
        return read_storage_versions(uri, {})

    async def read_operations(_uri: str, versions: list[int]) -> dict[int, str | None]:
        return read_version_operations(uri, {}, versions)

    statuses = asyncio.run(
        reconcile_all(
            cast(Any, repo),
            read_version,
            backfill=True,
            read_versions=read_versions,
            read_operations=read_operations,
        )
    )

    assert statuses[0].versions_without_lineage == [], (
        "neither version a compaction commits is a provenance hole: v6 is a Rewrite and v5 changed no rows, no data files and no deletions"
    )
    assert repo.backfilled == [], "a compaction must plant no provenance — the graph cannot tell a fabricated edge from a real one later"


def test_an_unnamed_version_that_changed_data_is_reported_and_never_fabricated(tmp_path: Path) -> None:
    """The other direction, which is what stops this fix from becoming the bug it replaces.

    A version the binding cannot name but whose counters MOVED is a real candidate for lost provenance.
    It must stay in the report — an operator has to see it — and must still not be back-filled, because
    the sweep cannot honestly claim a run wrote something it could not identify.
    """
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": [1]}), uri)
    for value in (2, 3):
        lance.write_dataset(pa.table({"id": [value]}), uri, mode="append")
    # BELOW the tip on purpose: the graph's tip matches storage's, so the version axis reports in_sync
    # and the tip back-fill never runs. Placed at the tip instead, the tip path claims the version before
    # hole recovery sees it and this gate passes while proving nothing about recovery.
    repo = _Repo(graph_versions={1, 3}, uri=uri)

    async def read_version(_uri: str) -> int | None:
        return 3

    async def read_versions(_uri: str) -> list[int] | None:
        return [1, 2, 3]

    async def read_operations(_uri: str, versions: list[int]) -> dict[int, str | None]:
        return dict.fromkeys(versions)

    statuses = asyncio.run(
        reconcile_all(
            cast(Any, repo),
            read_version,
            backfill=True,
            read_versions=read_versions,
            read_operations=read_operations,
        )
    )

    assert statuses[0].versions_without_lineage == [2], "an unreadable transaction over a real append must stay visible"
    assert repo.backfilled == [], "reporting is honest; back-filling an unidentified version is a fabricated provenance claim"
