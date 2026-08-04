"""The Lander — the ONE component that writes Lance. Invariant I4.

Everything else in the plane produces records or fragments and hands them here. That is not
tidiness: it is what makes the estate's write path auditable. One writer means one place where a
commit is registered with the catalog, one place that stamps the run id into commit metadata, and
one place to look when a version appears that nobody can explain.

`tests/unit/test_ingest_invariants.py` enforces it by grep — `lance.write_dataset`, `merge_insert`
and `lance.fragment.write_fragments` may appear in this module and nowhere else under
`services/ingest`. That gate is proven to fail on a seeded violation, unlike the ratch docstring it
replaces.

THE CREATION TWO-STEP (§0 C10). The obvious design — workers write fragments, the lander commits
them — cannot create a dataset, because the catalog's client-direct fragment door hardcodes
`LanceOperation.Append` (`services/catalog/.../dataplane.py:614`) and rules at `:594-595` that
"CREATE and OVERWRITE stay server-side to centralize it and to owner-govern the destructive reset".
So creation is the catalog's and appends are the lander's:

    1. the catalog creates the dataset EMPTY, server-side, with the creation-time-only flags
       (`enable_stable_row_ids=True`, `data_storage_version="2.2"`) — measured working with a
       blob-v2 column at zero rows;
    2. the lander commits fragments as an Append against that version.

This keeps "no byte transits the catalog" true — the create carries zero rows — instead of false on
every dataset's first run, which is what the single-step design would have quietly done.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol

import lance
import pyarrow as pa
from lance import LanceOperation
from pydantic import BaseModel


if TYPE_CHECKING:
    from collections.abc import Sequence

# Creation-time-only, and silent no-ops if set later (file_format.md:4011-4013 + guide.md:228-229) — which is
# why gate A14 makes the catalog refuse a governed dataset created without them. CDF (D1) and every
# `source_rowid` reference in silver/gold (D2) depend on stable row ids existing from version 1.
CREATION_FLAGS = {"data_storage_version": "2.2", "enable_stable_row_ids": True}


class CommitResult(BaseModel):
    """What the lander reports back to the workflow's finalize activity."""

    dataset_uri: str
    version: int
    #: Rows in the DATASET after the commit. A tier total, not a run's work.
    rows: int
    #: Rows THIS run added. Reported separately because the two diverge the moment a dataset takes a
    #: second run, and conflating them made the second in-cluster lane report 8 units done for a run
    #: that ingested 4 files — a run's progress must describe the run, not the tier it landed in.
    rows_added: int = 0
    fragments_committed: int = 0


class CatalogClient(Protocol):
    """The catalog seam. A Protocol so the lander is testable without a live catalog service."""

    def ensure_dataset(self, project: str, dataset: str, schema: pa.Schema) -> str:
        """Create the dataset EMPTY server-side if absent; return its object-store URI."""
        ...

    def register_version(self, dataset_uri: str, version: int, run_id: str) -> None:
        """Register the committed version, carrying the run id in commit metadata.

        That metadata is how a died-after-commit run is reconciled from storage truth, and how the
        image tag binds a CODE version to a DATA version in lineage.
        """
        ...


class Lander:
    """Commits fragments produced by workers into ONE new dataset version."""

    def __init__(self, catalog: CatalogClient) -> None:
        self._catalog = catalog

    def ensure(self, project: str, dataset: str, schema: pa.Schema) -> str:
        return self._catalog.ensure_dataset(project, dataset, schema)

    def commit_fragments(self, dataset_uri: str, fragments_json: Sequence[str], run_id: str) -> CommitResult:
        """Fragments -> ONE Append commit -> catalog registration.

        Deliberately ONE commit for the whole run (D6). Bronze shows the prior version until this
        returns and the new one all at once after, so there is no observable partially-ingested
        state — which is what lets silver ask "did a publication happen?" instead of "is ingest
        finished?".

        An empty fragment list is a no-op, not an empty commit: a run whose every unit failed should
        leave no version behind to explain.
        """
        if not fragments_json:
            ds = lance.dataset(dataset_uri)
            return CommitResult(dataset_uri=dataset_uri, version=ds.version, rows=ds.count_rows())

        fragments = [lance.fragment.FragmentMetadata.from_json(f) for f in fragments_json]
        base = lance.dataset(dataset_uri)
        committed = lance.LanceDataset.commit(dataset_uri, LanceOperation.Append(fragments), read_version=base.version)
        self._catalog.register_version(dataset_uri, committed.version, run_id)
        # Counted from the FRAGMENTS, not as a before/after difference against the dataset. A
        # difference would be wrong the moment two runs commit concurrently — each would see the
        # other's rows and claim them — and this is exactly the plane where that is normal.
        added = sum(int(getattr(f, "physical_rows", 0) or 0) for f in fragments)
        return CommitResult(
            dataset_uri=dataset_uri,
            version=committed.version,
            rows=committed.count_rows(),
            rows_added=added,
            fragments_committed=len(fragments),
        )


def write_unit_fragments(dataset_uri: str, batch: pa.Table) -> list[str]:
    """A worker's half of the write: fragments on disk, invisible until the lander commits them.

    Lives here rather than in the worker because of I4 — the write verb may appear in exactly one
    module. The worker calls this; it does not call Lance.

    Returns JSON STRINGS because FragmentMetadata has to cross a process boundary (worker ->
    workflow -> lander) and pre-commit fragment IDS COLLIDE — every worker's first fragment is id 0
    (`lance_docs/guide.md:1576-1578`, confirmed on pylance 9.0.0). So fragments are keyed by unit,
    never by fragment id; treating the id as unique would silently drop every worker's work but one.

    `json.dumps` is load-bearing, not decoration: pylance's serialization is ASYMMETRIC —
    `FragmentMetadata.to_json()` returns a **dict** while `FragmentMetadata.from_json()` is typed
    `(json_data: str)`. Pairing them directly raises "the JSON object must be str, bytes or
    bytearray, not dict" at COMMIT time, i.e. after every worker has already done its fetching.

    **The creation flags are passed HERE too, and that is not redundancy.** `write_fragments`
    defaults to `data_storage_version=None` (which resolves to 2.1) and, worse,
    `enable_stable_row_ids=False`. A fragment written against a not-yet-existing dataset therefore
    takes those defaults, and the first in-cluster run died at commit with

        The operation added files with version 2.1. However, the data storage version is 2.2.

    after every fixture had been fetched, validated and written. The stable-row-id half would not
    even have failed loudly: the commit would have succeeded and D1's change-data-feed and every
    `source_rowid` reference in silver would have been quietly built on fragments that carry no
    stable ids — and the flag is creation-time-only, so there is no later point at which it could be
    repaired. Passing the flags explicitly makes both impossible regardless of what exists yet.
    """
    written = lance.fragment.write_fragments(batch, dataset_uri, **CREATION_FLAGS)
    return [json.dumps(f.to_json()) for f in written]


def create_empty(dataset_uri: str, schema: pa.Schema) -> int:
    """Step 1 of the creation two-step — an empty dataset carrying the creation-time flags.

    Server-side in production (the catalog owns CREATE); exposed here so the same code path is
    exercisable in tests and by the §7.11 verification script.
    """
    ds = lance.write_dataset(schema.empty_table(), dataset_uri, mode="create", **CREATION_FLAGS)
    return ds.version


def fragments_to_json(fragments: Sequence[object]) -> str:
    return json.dumps([str(f) for f in fragments])
