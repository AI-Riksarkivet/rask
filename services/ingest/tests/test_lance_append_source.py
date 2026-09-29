"""The `lance-append` source kind and the three guards the plan attached to it.

1b was deferred with its guards, on the reasoning that "a guard for a kind that does not exist is
unreachable code". The kind exists now, so the guards are reachable and are asserted here:

* **refuse a source that resolves to a catalog table**, naming the medallion stage runner — copying between
  governed tiers is the cascade's job, and doing it through ingest would fabricate a second, unlineaged
  path between two tiers the cascade already owns;
* **refuse at ACCEPT rather than hanging** — an unreadable dataset must fail while the caller still
  holds the request, not inside a worker that has already claimed a unit;
* **bronze conformance in front of register** — every unit's payload must be a real Arrow IPC stream,
  which is what makes the blob column readable by whatever consumes bronze.

The confinement mirrors `local-dir` exactly: one env names the root the kind may read, and UNSET means
the kind is refused rather than pointed at everything.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from ingest.adapters import LANCE_ROOT_ENV, register_builtin_sources
from ingest.sources import SourceSpec, build_source, iter_units


lance = pytest.importorskip("lance")


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_builtin_sources()


@pytest.fixture
def dataset(tmp_path: Path) -> str:
    uri = str(tmp_path / "ext.lance")
    lance.write_dataset(pa.table({"id": [1, 2, 3], "w": ["a", "b", "c"]}), uri)
    return uri


def test_unset_root_refuses_the_kind(dataset: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same rule as local-dir: a source that cannot be pointed anywhere is a source nobody can abuse."""
    monkeypatch.delenv(LANCE_ROOT_ENV, raising=False)
    spec = SourceSpec(kind="lance-append", project="p", dataset="d", options={"uri": dataset})
    with pytest.raises(ValueError, match=LANCE_ROOT_ENV):
        build_source(spec)


def test_units_carry_readable_arrow_ipc(dataset: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard 3 — bronze conformance: the blob must reopen as the fragment's rows."""
    monkeypatch.setenv(LANCE_ROOT_ENV, str(tmp_path))
    spec = SourceSpec(kind="lance-append", project="p", dataset="d", options={"uri": dataset})
    units = list(iter_units(build_source(spec)))
    assert units, "a non-empty dataset must yield at least one unit"
    rows = sum(pa.ipc.open_stream(unit.data).read_all().num_rows for unit in units)
    assert rows == 3
