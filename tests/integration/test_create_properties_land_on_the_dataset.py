"""A property set at create must be readable OFF the table, not only echoed back (LH-025).

`create_table` passed `properties` into `declare_table` and echoed them in its response, and nothing
wrote them onto the dataset. So the spec's "stamped at create" was true of the namespace manifest and
of the reply, and false of the Lance file — a client that created a table with `{"owner": "…"}` and
then asked the table for its metadata got nothing back, with no error to say why.

DRIVEN THROUGH THE REAL WRITE, not a mock: the `dir` backend plus a real `lance.write_dataset`, then
the properties are read back through `read_schema_metadata` — the same door a client uses. A test that
asserted on the response body would have passed the whole time the defect existed, because the echo
was never the broken part.
"""

from __future__ import annotations

import pyarrow as pa
import pytest
from lance_namespace import LanceNamespace, connect

from catalog.services import dataplane


@pytest.fixture
def real_ns(tmp_path: object) -> LanceNamespace:
    return connect("dir", {"root": str(tmp_path)})


def _arrow_bytes() -> bytes:
    table = pa.table({"id": pa.array([1, 2], pa.int64()), "payload": pa.array(["a", "b"])})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def test_a_property_set_at_create_is_readable_off_the_table(real_ns: LanceNamespace) -> None:
    """The whole row: create with properties, then ask the TABLE what it carries."""
    dataplane.create_table(real_ns, {}, ["props_at_create"], _arrow_bytes(), properties={"owner": "team-a", "tier": "gold"})

    stored = dataplane.read_schema_metadata(real_ns, {}, ["props_at_create"])

    assert stored.get("owner") == "team-a", f"the property never reached the dataset: {stored}"
    assert stored.get("tier") == "gold", f"the property never reached the dataset: {stored}"


def test_creating_without_properties_writes_no_metadata(real_ns: LanceNamespace) -> None:
    """A create that names nothing must not invent a key — the boundary the merge could get wrong."""
    dataplane.create_table(real_ns, {}, ["no_props"], _arrow_bytes())

    assert dataplane.read_schema_metadata(real_ns, {}, ["no_props"]) == {}


def test_the_create_stamp_does_not_evict_the_internal_lineage_keys(real_ns: LanceNamespace) -> None:
    """`replace=True` would drop the `lineage.*` coordinates that make the file self-describing.

    Asserted through the RAW dataset rather than `read_schema_metadata`, which filters those keys out
    by design — so reading through the filtered door could never see them disappear.
    """
    import lance

    location = dataplane.create_table(real_ns, {}, ["keeps_lineage"], _arrow_bytes(), properties={"owner": "team-a"}).location
    assert location

    lance.dataset(location).update_schema_metadata({"lineage.dataset_id": "acme$gold"})
    dataplane.update_schema_metadata(real_ns, {}, ["keeps_lineage"], {"tier": "silver"})

    raw = lance.dataset(location).schema.metadata or {}
    decoded = {k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v for k, v in raw.items()}
    assert decoded.get("lineage.dataset_id") == "acme$gold", f"an internal coordinate was evicted: {decoded}"
    assert decoded.get("owner") == "team-a", "the create stamp did not survive a later property write"
