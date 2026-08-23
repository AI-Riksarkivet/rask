"""The unit-FETCH half of `lance-append` — the half that had no test and did not work.

`test_adapters.py` covers enumeration: it builds the source, counts fragments and checks the keys.
That is why the defect survived. A unit key crosses the queue and is resolved at the far end by a
`Fetcher`, and `lance-append`'s keys (`<uri>#fragment=<id>`) are not scheme-resolvable URIs, so the
default `UriFetcher` read them as `file`-scheme blobs and refused every one as being outside
RASK_INGEST_LOCAL_ROOT. Measured in-cluster 2026-08-23: `units_total: 3, units_done: 0`,
`publish_reason: "nothing to commit"` — a run that enumerates perfectly and lands nothing.

So these tests assert the SEAM, not just the reader: that the kind registers a fetcher at all, that
the default kinds still do not (they must keep falling back), and that the bytes it returns are the
fragment's rows.
"""

from __future__ import annotations

import pyarrow as pa
import pytest
from ingest.adapters import register_builtin_sources
from ingest.sources import fetcher_for


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    """A real three-fragment Lance dataset, with the confinement root pointed at it."""
    lance = pytest.importorskip("lance")
    uri = str(tmp_path / "run-42.lance")
    lance.write_dataset(pa.table({"id": [1, 2, 3], "note": ["alpha", "beta", "gamma"]}), uri, mode="create")
    lance.write_dataset(pa.table({"id": [4, 5], "note": ["delta", "epsilon"]}), uri, mode="append")
    lance.write_dataset(pa.table({"id": [6], "note": ["zeta"]}), uri, mode="append")
    monkeypatch.setenv("RASK_INGEST_LANCE_ROOT", str(tmp_path))
    monkeypatch.delenv("LANCE_REST_ROOT", raising=False)
    register_builtin_sources()
    return uri


def test_lance_append_registers_its_own_fetcher() -> None:
    """The seam itself. Without this the kind enumerates and then fails every unit."""
    register_builtin_sources()
    assert fetcher_for("lance-append") is not None


@pytest.mark.parametrize("kind", ["s3-prefix", "local-dir"])
def test_scheme_resolvable_kinds_keep_the_default_fetcher(kind: str) -> None:
    """The fallback must stay the norm — a kind emitting `s3://`/`file://` keys needs no fetcher.

    Pinned because the easy over-correction is to give every kind one, which would move source
    knowledge back into the fetch path that I1 exists to keep free of it.
    """
    register_builtin_sources()
    assert fetcher_for(kind) is None


def test_unknown_kind_falls_back_rather_than_raising() -> None:
    """An older build's chunk can name a kind this process no longer registers."""
    assert fetcher_for("a-kind-that-was-removed") is None


@pytest.mark.asyncio
async def test_fetch_returns_that_fragments_rows(dataset: str) -> None:
    """The actual bytes: fragment 1 is the second append — `delta`/`epsilon`, not the whole dataset."""
    fetcher = fetcher_for("lance-append")
    assert fetcher is not None
    payload = await fetcher.fetch(f"{dataset}#fragment=1")

    table = pa.ipc.open_stream(pa.BufferReader(payload)).read_all()
    assert table.column("note").to_pylist() == ["delta", "epsilon"]
    assert table.num_rows == 2


@pytest.mark.asyncio
async def test_every_enumerated_key_is_fetchable(dataset: str) -> None:
    """Enumeration and fetch must agree — the exact pairing the live run got wrong.

    Asserting the ROUND TRIP (each key the source emits resolves through the fetcher, and the row
    counts sum to the dataset) is what a per-half test could not: both halves passed alone.
    """
    from ingest.sources import iter_unit_keys

    from service_kit.lakehouse.sources import LanceFragmentSource

    fetcher = fetcher_for("lance-append")
    assert fetcher is not None
    # `iter_unit_keys`, not `iter_objects`: the plane enumerates WITHOUT fetching, and iter_objects
    # would read every fragment here — hiding the very split this test exists to cover.
    keys = list(iter_unit_keys(LanceFragmentSource(dataset)))
    assert len(keys) == 3

    total = 0
    for key in keys:
        table = pa.ipc.open_stream(pa.BufferReader(await fetcher.fetch(key))).read_all()
        total += table.num_rows
    assert total == 6


@pytest.mark.asyncio
async def test_fetch_reapplies_confinement(dataset: str, tmp_path, monkeypatch) -> None:
    """A key legal at accept must not be fetchable after the root moves — fail closed, not read.

    The queue is durable: a unit can be drained by a later process whose confinement differs, so
    `probe()` at accept time cannot stand in for a check here.
    """
    fetcher = fetcher_for("lance-append")
    assert fetcher is not None
    monkeypatch.setenv("RASK_INGEST_LANCE_ROOT", str(tmp_path / "somewhere-else"))
    with pytest.raises(ValueError, match="outside RASK_INGEST_LANCE_ROOT"):
        await fetcher.fetch(f"{dataset}#fragment=0")


@pytest.mark.asyncio
async def test_missing_fragment_is_permanent_not_transient(dataset: str) -> None:
    """Compaction retires fragment ids. Retrying one never helps, so it must not look transient."""
    fetcher = fetcher_for("lance-append")
    assert fetcher is not None
    with pytest.raises(FileNotFoundError):
        await fetcher.fetch(f"{dataset}#fragment=999")


@pytest.mark.asyncio
async def test_malformed_key_is_refused(dataset: str) -> None:
    fetcher = fetcher_for("lance-append")
    assert fetcher is not None
    with pytest.raises(ValueError, match="missing its fragment marker"):
        await fetcher.fetch(dataset)
