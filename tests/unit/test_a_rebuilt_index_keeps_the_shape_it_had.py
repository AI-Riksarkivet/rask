"""An index rebuilt by the estate comes back with the parameterisation it was built with.

[[LH-105]]. A repair door that silently re-tunes what it repairs is worse than no door: the operator
asks for the index back and gets a different one, the table keeps answering queries, and the only
symptom is recall or latency nobody can attribute. So the property under test is FIDELITY, not
"an index exists afterwards".

WHY THE READBACK NEEDS TWO CALLS, which is the whole reason this module exists. Measured on pylance
11.0.0 — the version the deployed catalog runs — `describe_indices()` carries `.name`,
`.index_type`, `.field_names`, `.type_url` and `.details`, and for a vector index `.details` is
`{"metric_type": ..., "compression": {"num_bits": ..., "num_sub_vectors": ...}, "runtime_hints": {...}}`.
**`num_partitions` is not in it.** It appears only as `indices[0].num_partitions` in
`index_statistics(name)`. A rebuild reading only the non-deprecated call therefore re-partitions the
index to pylance's default without erroring, which is exactly the silent re-tune above.

THE KIND IS READ, NOT INFERRED. `IndexWorkItem.kind` documents that a worker "guessing between them
would build a different index than the caller asked for" — true of a worker, and the reason this
resolution happens here instead: `.type_url` is `/lance.index.pb.VectorIndexDetails` for a vector
index and `/lance.table.<Kind>IndexDetails` for every scalar one, so the discriminator is observed
off the live index rather than derived from a name list that a new index type would silently fall
out of.

T6 — the bug class is "a rebuild loses a parameter", so every parameter that CAN be lost is asserted,
not only the one that was found missing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import lance
import numpy as np
import pyarrow as pa
import pytest

from catalog.services import index_specs
from service_kit.lakehouse.work_items import SCALAR_INDEX_TYPES


VECTOR_DIMENSION = 8
ROWS = 512


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One dataset carrying a deliberately NON-DEFAULT index of each kind.

    Non-default on purpose: an index built with pylance's own defaults would round-trip through a
    readback that dropped every parameter, and this suite would pass while proving nothing.
    """
    location: Path = tmp_path_factory.mktemp("reindex") / "ds"
    rng = np.random.default_rng(seed=17)
    table = pa.table(
        {
            "id": pa.array(range(ROWS)),
            "vec": pa.FixedSizeListArray.from_arrays(pa.array(rng.random(ROWS * VECTOR_DIMENSION, dtype="float32")), VECTOR_DIMENSION),
            "label": pa.array([str(i % 7) for i in range(ROWS)]),
            "body": pa.array([f"the quick brown fox {i}" for i in range(ROWS)]),
        }
    )
    ds = lance.write_dataset(table, str(location))
    ds.create_index("vec", index_type="IVF_PQ", name="vec_idx", num_partitions=4, num_sub_vectors=2, metric="cosine")
    ds.create_scalar_index("label", index_type="BITMAP", name="label_idx")
    ds.create_scalar_index("body", index_type="INVERTED", name="body_idx", base_tokenizer="whitespace", with_position=False)
    return ds


def test_a_vector_index_keeps_its_partition_count(dataset: Any) -> None:
    """`num_partitions` is the field `describe_indices()` does not carry, so it is the one a rebuild loses."""
    spec = index_specs.describe_index_for_rebuild(dataset, "vec_idx")

    assert spec.params.get("num_partitions") == 4, f"the rebuild would re-partition this index; read back {spec.params}"


def test_a_vector_index_keeps_its_sub_vector_count(dataset: Any) -> None:
    """`num_sub_vectors` sits under `.details.compression`, not at the top level where a reader expects it."""
    spec = index_specs.describe_index_for_rebuild(dataset, "vec_idx")

    assert spec.params.get("num_sub_vectors") == 2, f"the rebuild would re-quantize this index; read back {spec.params}"


def test_a_vector_index_keeps_its_distance_metric(dataset: Any) -> None:
    """A metric silently reverting to pylance's `L2` default makes every stored distance mean something else."""
    spec = index_specs.describe_index_for_rebuild(dataset, "vec_idx")

    assert str(spec.params.get("metric", "")).lower() == "cosine", f"read back {spec.params}"


def test_a_vector_index_is_recognised_as_a_vector_index(dataset: Any) -> None:
    """Built through the wrong pylance call, a rebuilt index answers queries wrongly rather than not at all."""
    spec = index_specs.describe_index_for_rebuild(dataset, "vec_idx")

    assert (spec.kind, spec.index_type, spec.column) == ("vector", "IVF_PQ", "vec")


def test_a_full_text_index_keeps_its_tokenizer(dataset: Any) -> None:
    """The inverted index carries the richest `.details`, and a default tokenizer changes what matches."""
    spec = index_specs.describe_index_for_rebuild(dataset, "body_idx")

    assert spec.params.get("base_tokenizer") == "whitespace", f"read back {spec.params}"
    assert spec.params.get("with_position") is False, f"read back {spec.params}"


def test_a_scalar_index_is_recognised_as_a_scalar_index(dataset: Any) -> None:
    """`.type_url` is the discriminator; a BITMAP carries no tuning, so the kind is all there is to lose."""
    spec = index_specs.describe_index_for_rebuild(dataset, "label_idx")

    assert (spec.kind, spec.index_type, spec.column) == ("scalar", "BITMAP", "label")


def test_an_unknown_index_name_is_refused_rather_than_rebuilt_as_something_else(dataset: Any) -> None:
    """A miss must not become a create: the caller asked to REPAIR a named index, and inventing one
    from a default parameterisation is the silent re-tune this module exists to prevent."""
    with pytest.raises(index_specs.IndexNotFoundForRebuildError, match="no_such_idx"):
        index_specs.describe_index_for_rebuild(dataset, "no_such_idx")


def test_the_readback_survives_an_actual_rebuild(dataset: Any) -> None:
    """The end-to-end property, and the only one that proves the parameters are USABLE rather than merely read.

    Feeding the readback straight into pylance is also what pins the two spellings that could have
    needed normalising: `.details` reports `metric_type` upper-case (`COSINE`) and
    `describe_indices()` reports a scalar `index_type` mixed-case (`BTree`), and both were measured
    accepted verbatim on 11.0.0.
    """
    spec = index_specs.describe_index_for_rebuild(dataset, "vec_idx")

    dataset.create_index(spec.column, index_type=spec.index_type, name=spec.name, replace=True, **spec.params)

    rebuilt = json.loads(dataset._ds.index_statistics("vec_idx"))["indices"][0]
    assert rebuilt["num_partitions"] == 4
    assert rebuilt["sub_index"]["num_sub_vectors"] == 2
    assert str(rebuilt["metric_type"]).lower() == "cosine"


#: Every scalar type pylance will build on an ordinary column, with the column it needs. Enumerated
#: rather than sampled because the defect this pins is per-SPELLING: `LABEL_LIST` reads back as
#: `LabelList`, which upper-cases to `LABELLIST` — a value `SCALAR_INDEX_TYPES` does not contain, so
#: the worker would refuse the rebuild as an unknown index type. Six of the seven survive `.upper()`
#: and would have made a sampled test pass.
SCALAR_KINDS_AND_COLUMNS = [
    ("BTREE", "label"),
    ("BITMAP", "label"),
    ("LABEL_LIST", "tags"),
    ("INVERTED", "body"),
    ("NGRAM", "body"),
    ("ZONEMAP", "label"),
    ("BLOOMFILTER", "label"),
]


@pytest.mark.parametrize(("index_type", "column"), SCALAR_KINDS_AND_COLUMNS)
def test_every_scalar_type_reads_back_in_the_vocabulary_the_worker_accepts(tmp_path: Path, index_type: str, column: str) -> None:
    """The readback's spelling must be one `build_index` will dispatch on, or the repair dies at the worker.

    Asserted against `SCALAR_INDEX_TYPES` itself — the frozenset the worker checks — rather than a
    literal, so the two cannot drift: this is the same object both sides import.
    """
    ds = _dataset_with_every_column(tmp_path)
    ds.create_scalar_index(column, index_type=index_type, name="idx")

    spec = index_specs.describe_index_for_rebuild(ds, "idx")

    assert spec.index_type in SCALAR_INDEX_TYPES, f"pylance reports {index_type} as {spec.index_type!r}, which the worker would refuse as unknown"
    assert spec.index_type == index_type


@pytest.mark.parametrize(("index_type", "column"), SCALAR_KINDS_AND_COLUMNS)
def test_every_scalar_type_actually_rebuilds_from_its_own_readback(tmp_path: Path, index_type: str, column: str) -> None:
    """Fidelity end to end per kind: the readback is fed straight back to pylance and must be accepted.

    A spelling can be in the vocabulary and still be rejected by pylance, and a parameter can read
    back and still not be a keyword `create_scalar_index` answers to — neither shows up until
    something calls it.
    """
    ds = _dataset_with_every_column(tmp_path)
    ds.create_scalar_index(column, index_type=index_type, name="idx")
    spec = index_specs.describe_index_for_rebuild(ds, "idx")

    ds.create_scalar_index(spec.column, index_type=spec.index_type, name=spec.name, replace=True, **spec.params)

    assert index_specs.describe_index_for_rebuild(ds, "idx").index_type == index_type


def _dataset_with_every_column(tmp_path: Path) -> Any:
    """A dataset carrying one column of each shape the scalar index types need."""
    rows = 256
    table = pa.table(
        {
            "label": pa.array([str(i % 7) for i in range(rows)]),
            "tags": pa.array([[str(i % 3), str(i % 5)] for i in range(rows)], type=pa.list_(pa.string())),
            "body": pa.array([f"the quick brown fox {i}" for i in range(rows)]),
        }
    )
    return lance.write_dataset(table, str(tmp_path / "ds"))
