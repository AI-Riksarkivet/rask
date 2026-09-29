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
def real_ns(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> LanceNamespace:
    """The `dir` backend, rooted where this test can write.

    `monkeypatch.setenv` rather than `os.environ`, so teardown restores the environment and these tests
    stay order-independent — the property that was actually missing when they were written: they passed
    under the full suite and failed whenever `tests/integration` ran alone.

    THE CREDENTIALS THIS FIXTURE USED TO SET ARE GONE, and the reason is the point: `dataplane`'s
    create/read reach `shared_lance_session()`, which used to build the catalog's whole `Settings` —
    where the S3 pair is REQUIRED — to read two defaulted cache integers. Opening a LOCAL dataset
    therefore demanded credentials it never used. `LanceSessionCaps` now carries just those two, so this
    fixture needs no credential and the suite is never taught that one arrives through the environment.
    """
    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", str(tmp_path))

    from catalog.core.config import get_settings

    get_settings.cache_clear()  # a Settings cached by an earlier test would carry another root
    return connect("dir", {"root": str(tmp_path)})


def _table() -> pa.Table:
    return pa.table({"id": pa.array([1, 2], pa.int64()), "payload": pa.array(["a", "b"])})


def test_a_property_set_at_create_is_readable_off_the_table(real_ns: LanceNamespace) -> None:
    """The whole row: create with properties, then ask the TABLE what it carries."""
    dataplane.create_table(real_ns, {}, ["props_at_create"], _table(), properties={"owner": "team-a", "tier": "gold"}, registry=None)

    stored = dataplane.read_schema_metadata(real_ns, {}, ["props_at_create"])

    assert stored.get("owner") == "team-a", f"the property never reached the dataset: {stored}"
    assert stored.get("tier") == "gold", f"the property never reached the dataset: {stored}"


def test_creating_without_properties_writes_no_metadata(real_ns: LanceNamespace) -> None:
    """A create that names nothing must not invent a key — the boundary the merge could get wrong."""
    dataplane.create_table(real_ns, {}, ["no_props"], _table(), registry=None)

    assert dataplane.read_schema_metadata(real_ns, {}, ["no_props"]) == {}
