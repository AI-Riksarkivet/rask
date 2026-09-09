"""SK-04 — the SERVING layer's three "missing thing" sites, on the estate's one absence vocabulary.

pylance exposes no typed error for a missing table/version, so the registry's ``table_dataset``, the
reader's ``_at_version`` and ``introspect.discover_tables`` classify by message substring. Before the
shared helper they had already drifted: the reader matched only "not found", so an OSError saying
"does not exist" escaped as a raw 500 instead of the 404 the registry returned for the same condition.

That helper was `lancekit.errors.is_not_found`, and it has been folded into
`lancekit.absence.reads_as_absent` — the write and reconcile paths were asking the same question of a
second shared classifier with the opposite width, which is the drift this file gates, one level up.

The writer's commit-conflict markers are deliberately NOT part of this: a lost OCC race is a different
condition (409, re-read and re-send), not a not-found.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import lance
import pytest

from service_kit.exceptions import NotFoundError
from service_kit.lancekit import registry as registry_mod
from service_kit.lancekit.absence import reads_as_absent
from service_kit.lancekit.reader import LocalCatalogTransport


class _RaisingDataset:
    """Stands in for lance.LanceDataset; checkout_version raises what we script."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def checkout_version(self, version: int) -> None:
        raise self._exc


def _transport(exc: BaseException) -> LocalCatalogTransport:
    transport = LocalCatalogTransport.__new__(LocalCatalogTransport)
    # cast: a scripted stand-in — _at_version only calls checkout_version, and a
    # real LanceDataset cannot be made to raise a chosen message on demand.
    transport._ds = cast("lance.LanceDataset", _RaisingDataset(exc))
    return transport


def test_helper_classifies_both_missing_wordings() -> None:
    assert reads_as_absent(OSError("LanceError(IO): Object at location foo does not exist"))
    assert reads_as_absent(ValueError("Table bar was not found"))
    assert not reads_as_absent(OSError("Commit conflict for version 7: concurrent writer"))


def test_reader_translates_does_not_exist_into_not_found() -> None:
    # The wording the object store produces for a missing version file. Before the
    # shared classifier the reader matched only "not found" and this escaped raw.
    transport = _transport(OSError("Object at location data/_versions/9.manifest does not exist"))
    with pytest.raises(NotFoundError):
        transport._at_version(9)


def test_reader_still_translates_not_found() -> None:
    """The message is pylance's OWN, measured — the fixture used to be an invented `"version 9 not found"`.

    That string is not a wording pylance produces, so the test proved only that the classifier matched
    the phrase the test had written. Driven on 11.0.0, `checkout_version` on a version that is not
    there says this, over both a local path and S3.
    """
    transport = _transport(OSError("Dataset at path t/_versions/9.manifest was not found: Error performing HEAD http://s/b/t/_versions/9.manifest"))
    with pytest.raises(NotFoundError):
        transport._at_version(9)


def test_the_reader_does_not_call_a_CORRUPT_dataset_a_missing_one() -> None:
    """A deleted data file is not a 404, and the wide classifier said it was.

    MEASURED on pylance 11.0.0: deleting one object under `t/data/` and reading gives the message
    below — `Not found:` for a file the manifest still references. The dataset EXISTS and is damaged,
    so answering "no such table" reports data loss as a typo and sends nobody to look at the store.
    """
    corrupt = OSError("External error: LanceError(IO): Generic N/A error: Wrapped error: Not found: t/data/9d3f.lance")
    transport = _transport(corrupt)
    with pytest.raises(OSError, match="Not found"):
        transport._at_version(9)


def test_reader_leaves_other_oserrors_alone() -> None:
    transport = _transport(OSError("connection reset by peer"))
    with pytest.raises(OSError, match="connection reset"):
        transport._at_version(9)


def test_registry_translates_missing_table_into_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = SimpleNamespace(
        id="ds",
        path=Path("/nonexistent"),
        storage_options={"endpoint": "http://s3:9000"},  # non-None → the local dir check is skipped
        table_uri=lambda table: f"s3://bucket/ds.lance/{table}.lance",
        sync_table_info=lambda table, version: None,
    )

    def raise_missing(uri: str, storage_options: dict[str, str] | None = None) -> None:
        raise OSError("LanceError(IO): Object at location s3://bucket/ds.lance/t.lance does not exist")

    monkeypatch.setattr(registry_mod.lance, "dataset", raise_missing)
    with pytest.raises(NotFoundError):
        # cast: a duck-typed stand-in — table_dataset only reads the attributes
        # scripted above, and building a real DatasetHandle needs a live dataset.
        registry_mod.table_dataset(cast("registry_mod.DatasetHandle", handle), "t")


def test_the_two_sites_share_one_classifier() -> None:
    """The inline substring matching is gone — every site goes through `lancekit.absence`."""
    src_dir = Path(registry_mod.__file__).parent
    for name in ("registry.py", "reader.py", "introspect.py"):
        source = (src_dir / name).read_text()
        assert '"does not exist" in' not in source and '"not found" in' not in source, (
            f"{name} still classifies not-found inline instead of via lancekit.absence"
        )
        assert "reads_as_absent" in source
