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
import logging
from typing import TYPE_CHECKING, Any, Protocol, TypedDict

import lance
import pyarrow as pa
from lance import LanceOperation
from pydantic import BaseModel


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Sequence


class CreationFlags(TypedDict):
    """The creation-time flags, typed PER KEY so `**CREATION_FLAGS` stays checkable.

    A plain dict literal infers as `dict[str, str | bool]`, and splatting that into `write_dataset`
    hands `str | bool` to every keyword it has — which ty reports once per parameter. That was 23 of
    this branch's diagnostics from two call sites, and the noise is the real cost: a genuinely wrong
    argument to the bronze writer would have arrived in the same pile and been read as more of the
    same. A TypedDict keeps the constant DRY and restores per-key types at the splat.
    """

    data_storage_version: str
    enable_stable_row_ids: bool


# Creation-time-only, and silent no-ops if set later (file_format.md:4011-4013 + guide.md:228-229) — which is
# why gate A14 makes the catalog refuse a governed dataset created without them. CDF (D1) and every
# `source_rowid` reference in silver/gold (D2) depend on stable row ids existing from version 1.
CREATION_FLAGS: CreationFlags = {"data_storage_version": "2.2", "enable_stable_row_ids": True}


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
        _ensure_partition_index(committed)
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


#: The scalar-index kind for `partition_key`. BITMAP because the column is LOW-CARDINALITY equality —
#: one distinct value per IIIF volume or S3 folder, queried as `partition_key = 'x'`. Lance stores a
#: compressed Roaring Bitmap per distinct value (`lance_docs/guide.md:3211-3215`), which is the case
#: BTREE would serve worse.
PARTITION_INDEX = "BITMAP"
PARTITION_INDEX_NAME = "partition_key_idx"
#: The anti-join column. BTREE, not BITMAP: `id` is HIGH-cardinality (one distinct value per row),
#: exactly the shape the format routes to BTREE — "essentially a sorted copy of a column"
#: (guide.md:3189), which over int64 is 8 bytes/row. Created at commit like the BITMAP, folded
#: forward by services/maintenance optimize_indices — the create-once/maintain-elsewhere division
#: the format itself draws (file_format.md:1200 fragment_bitmap coverage).
ID_INDEX = "BTREE"
ID_INDEX_NAME = "id_idx"


def _ensure_partition_index(dataset: Any) -> None:  # noqa: ANN401 — LanceDataset
    """Create the `partition_key` index if it is absent. Idempotent, best-effort, never fatal.

    THE COLUMN IS HALF A DESIGN WITHOUT THIS. `runtime.py:97-121` records why partitioning is a COLUMN
    here and not a fragment boundary — Lance has no table partitioning
    (`lance_docs/ns_catalog/partitioning-spec.md:4`, "Lance tables do not natively support
    partitioning, instead promoting clustering"), and key-pure fragments do not survive compaction
    (measured: five fragments, one key each, then `compact_files()` -> ONE). The same measurement says
    what DOES survive: "a BITMAP index on the column made `partition_key = 'x'` answer as an
    index-served ScalarIndexQuery rather than a scan."

    Nothing created it. `services/maintenance` cannot: `optimize_indices()` FOLDS compacted fragments
    into indices that already exist, and a grep for `create_scalar_index` across that whole service
    returns nothing. So a dataset with no index gets nothing from maintenance, forever, and every
    partition query was a full scan.

    HERE rather than at creation, because creation is the CATALOG's (the two-step at this module's
    head: the catalog creates the dataset empty server-side, the lander appends). Ingest's first
    reachable moment is its own commit — and an index over zero rows would be built and immediately
    stale anyway.

    CREATE ONCE, MAINTAIN ELSEWHERE — and that is the format's own division, not a shortcut. An index
    SEGMENT records which fragments it covers in its `fragment_bitmap`
    (`lance_docs/file_format.md:1200`), and the engine filters out row addresses that are not in it
    (`:1239`). So an index built at commit N covers commit N's fragments and no later ones: results
    stay correct as the tier grows, but new fragments are served by scan until something folds them
    in. That something is `services/maintenance`'s `optimize_indices()`, which is exactly what it does
    and all it does. Re-creating the index here on every commit would duplicate that work and make a
    routine append cost more as the tier grows.

    BITMAP is the format's own recommendation for this shape: `lance_docs/file_format.md`'s
    `index/scalar/bitmap.md` — "extremely fast query performance for LOW-CARDINALITY columns", which
    is one distinct value per IIIF volume or S3 folder.

    Never fatal: a run that landed its data must not fail because an optimisation could not be built.
    Same reasoning as I8 for lineage, and the index is recoverable at any later commit.
    """
    try:
        present = {getattr(i, "name", "") for i in dataset.describe_indices()}
        if PARTITION_INDEX_NAME not in present:
            dataset.create_scalar_index("partition_key", index_type=PARTITION_INDEX, name=PARTITION_INDEX_NAME)
        if ID_INDEX_NAME not in present:
            # The anti-join reads `id` on EVERY incremental tick; without this it is a full
            # column scan that grows with the tier. Same never-fatal contract as the BITMAP.
            dataset.create_scalar_index("id", index_type=ID_INDEX, name=ID_INDEX_NAME)
    except Exception:
        logger.warning("could not ensure the scalar indexes (partition_key/id) — queries will scan", exc_info=True)


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
