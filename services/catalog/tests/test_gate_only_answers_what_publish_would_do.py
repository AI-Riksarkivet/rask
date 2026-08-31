"""`gate` must refuse everything `publish` refuses — including the provenance contract.

The gate's own docstring states the invariant: *"a gate that answered differently from the publish
would be worse than no gate, because a caller would trust it. That equality includes the REFUSAL: an
unrunnable key column is a 400 here too, or a caller could ask the question, be told the gate would
pass, and then be refused by the act."*

The tier-contract refusal broke exactly that, and it was MY change that broke it:
`refuse_a_tier_without_provenance` was added to `publish` and not to `gate`. Measured by driving both
doors against one dataset carrying `stage` but neither `lineage` nor `source_rowid` — `gate` answered
200 with every assertion passing and no mention of a problem, and `publish` of the SAME version
answered 400.

Why that shape is the damaging one: the cascade's promotion review calls `gate` precisely so it can
decide BEFORE the tag moves. A gate that under-reports sends a reviewer an approval for work the act
will refuse, and the reviewer has no way to see it coming.

So the rule this pins is not "call the same functions" but "answer the same question": every refusal
`publish` can raise, `gate` raises too, on the same version.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import connect
from lance_namespace.errors import InvalidInputError
from lance_namespace_urllib3_client.exceptions import ApiException  # noqa: F401  (import shape kept for parity)

from catalog.services.dataplane import create_table, open_dataset
from catalog.services.publication import gate


lance = pytest.importorskip("lance")

TABLE_ID = ["tier"]

#: `stage` present, `lineage` and `source_rowid` absent — a table that CLAIMS to be governed and is not.
PARTIAL = pa.schema([pa.field("id", pa.int64()), pa.field("stage", pa.string())])
CONFORMING = pa.schema(
    [
        pa.field("id", pa.int64()),
        pa.field("stage", pa.string()),
        pa.field("lineage", pa.string()),
        pa.field("source_rowid", pa.uint64()),
    ]
)


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _make(tmp_path: Path, schema: pa.Schema, **cols: object):  # noqa: ANN201
    ns = connect("dir", {"root": str(tmp_path / "d")})
    create_table(ns, {}, TABLE_ID, _ipc(pa.table(dict(cols), schema=schema)), mode="create")
    return open_dataset(ns, {}, TABLE_ID).uri


def test_gate_refuses_a_nonconforming_tier_exactly_as_publish_does(tmp_path: Path) -> None:
    """The asymmetry: gate said 'would pass', publish said 400."""
    uri = _make(
        tmp_path,
        PARTIAL,
        id=pa.array([1, 2], pa.int64()),
        stage=pa.array(["silver", "silver"]),
    )

    # `InvalidInputError` — the spec's typed error, which `install_problem_handlers` renders as a 400
    # problem+json carrying code 4. Matching the CONCRETE type is the point: a service_kit exception
    # here would escape the spec envelope, which the catalog has an AST gate against.
    with pytest.raises(InvalidInputError, match="conforming tier"):
        gate(uri, key_column="id", version=1)


def test_gate_still_passes_a_conforming_tier(tmp_path: Path) -> None:
    """The guard against the fix becoming 'refuse everything' — the reviewer must still get a verdict."""
    uri = _make(
        tmp_path,
        CONFORMING,
        id=pa.array([1, 2], pa.int64()),
        stage=pa.array(["silver", "silver"]),
        lineage=pa.array(['{"run_id":"r"}'] * 2),
        source_rowid=pa.array([7, 8], pa.uint64()),
    )

    result = gate(uri, key_column="id", version=1)

    assert result.published is False, "gate never publishes — it answers"
    assert result.reason is None, f"a conforming tier must pass cleanly: {result.reason}"
