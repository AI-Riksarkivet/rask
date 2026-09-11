"""A stage's WROTE edge must name the version that changed the ROWS, not the index built after it.

`measure_stage` and `transform_stage` rebuild the lineage JSON scalar index and only THEN call
:func:`measure`, which reads ``ds.version`` — so the version that reaches the OpenLineage edge is the
trailing ``CreateIndex`` commit, one past the data write it is supposed to describe.

MEASURED ON THE LIVE ESTATE 2026-09-11, and it is not a sampling artefact — it is every one:

* **253 of 253** stage-authored producer edges sit on a ``CreateIndex`` version (162 `embed_features`,
  77 `aggregate_gold`, 14 `derive_media`).
* Four datasets have NO producer edge on any retained data version: `acme-silver$features` 102/102,
  `silver$features` 59/59, `silver-media$features` 19/19, `acme-gold$catalog` 24/24.
* Direct proof: `acme-silver$features` **v275** (an `Update`) holds rows whose
  ``lineage.run_id = cdd9111d-…``, while the graph's `WROTE` edge for that same run id is on **v276**
  (`CreateIndex`). v275's only edge is the `author='reconcile'` back-fill.

WHAT IT COSTS. The catalog prescribes a who/when/what join — `versions.py:79-84` joins `/producers`
``dataset_version`` against `/history` on the version number — and that join therefore attributes every
stage write to `reconcile` and names an index build as its producer. The `published` tag and the next
tier's ``from_version``/``to_version`` name index versions too, so a downstream consumer pinning "the
version I derived from" pins the index commit rather than the data.

WHY THE VERSION AND NOT THE INDEX BUILD IS THE THING TO MOVE: a `CreateIndex` changes no row, so it is
maintenance, and `lineage/core/reconcile.py` already classifies it as such — meaning once the edge names
the data version, the index version is correctly NOT reported as a provenance hole. The two halves
compose; removing the rebuild instead would be a separate decision about index durability, and its
stated rationale ("the Ray lane still overwrites") is stale now that both lanes merge.

Only ``version`` moves. Row count, byte size and schema are all unchanged by an index build, so they
stay measured after it, from one open.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa

from medallion.services import compute


def _staged(tmp_path: Path) -> tuple[str, str]:
    """An upstream and a downstream dataset, the downstream carrying a `lineage` column and two versions."""
    from_uri = str(tmp_path / "up.lance")
    to_uri = str(tmp_path / "down.lance")
    lance.write_dataset(pa.table({"id": [1, 2], "payload": ["a", "b"]}), from_uri)
    # `pa.json_()`, not `pa.string()`: a JSON scalar index can only be built on a Binary/LargeBinary
    # field, so a string column would make `_index_lineage` raise instead of committing the version this
    # gate exists to distinguish. That is the same distinction LH-012 closed on the write path.
    schema = pa.schema([pa.field("id", pa.int64()), pa.field("payload", pa.string()), pa.field("lineage", pa.json_())])
    lance.write_dataset(pa.table({"id": [1], "payload": ["a"], "lineage": ['{"run_id": "r1"}']}, schema=schema), to_uri)
    lance.write_dataset(pa.table({"id": [2], "payload": ["b"], "lineage": ['{"run_id": "r1"}']}, schema=schema), to_uri, mode="append")
    return from_uri, to_uri


def test_measure_stage_reports_the_version_the_data_landed_at(tmp_path: Path) -> None:
    """THE GATE. The index rebuild commits a version; the edge must still name the write before it."""
    from_uri, to_uri = _staged(tmp_path)
    data_version = int(lance.dataset(to_uri).version)

    result = compute.measure_stage(from_uri, to_uri, {})

    after = int(lance.dataset(to_uri).version)
    assert after > data_version, "this fixture must actually build an index, or the gate proves nothing"
    assert result.version == data_version, f"the WROTE edge named version {result.version} — the index build — instead of the data commit {data_version}"


def test_the_measurements_other_than_version_are_still_taken_after_the_index(tmp_path: Path) -> None:
    """Only the VERSION moves. An index build changes no row and no column, so the rest is unaffected
    and is still read from a single open — moving them too would buy nothing and cost a second read."""
    from_uri, to_uri = _staged(tmp_path)

    result = compute.measure_stage(from_uri, to_uri, {})

    assert result.row_count == 2
    assert {field["name"] for field in result.fields} == {"id", "payload", "lineage"}


def test_a_target_with_no_lineage_column_is_measured_at_its_own_version(tmp_path: Path) -> None:
    """No lineage column means no index rebuild, so nothing is one-past anything — the version the
    caller sees is the version on disk, exactly as before."""
    from_uri = str(tmp_path / "up.lance")
    to_uri = str(tmp_path / "down.lance")
    lance.write_dataset(pa.table({"id": [1]}), from_uri)
    lance.write_dataset(pa.table({"id": [1]}), to_uri)

    result = compute.measure_stage(from_uri, to_uri, {})

    assert result.version == int(lance.dataset(to_uri).version)
