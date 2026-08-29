"""SK-04 — ONE not-found message classifier for the two Lance "missing thing" sites.

pylance exposes no typed error for a missing table/version, so both the registry's
``table_dataset`` and the reader's ``_at_version`` classify by message substring.
Before the shared helper they had already drifted: the reader matched only
"not found", so an OSError saying "does not exist" (the wording object stores
actually produce for a missing path) escaped as a raw 500 instead of the 404 the
registry path returns for the very same condition.

The writer's commit-conflict markers are deliberately NOT part of this: a lost
OCC race is a different condition (409, re-read and re-send), not a not-found.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import lance
import pytest

from service_kit.exceptions import NotFoundError
from service_kit.lancekit import registry as registry_mod
from service_kit.lancekit.errors import is_not_found
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
    assert is_not_found(OSError("LanceError(IO): Object at location foo does not exist"))
    assert is_not_found(ValueError("Table bar was not found"))
    assert not is_not_found(OSError("Commit conflict for version 7: concurrent writer"))


def test_reader_translates_does_not_exist_into_not_found() -> None:
    # The wording the object store produces for a missing version file. Before the
    # shared classifier the reader matched only "not found" and this escaped raw.
    transport = _transport(OSError("Object at location data/_versions/9.manifest does not exist"))
    with pytest.raises(NotFoundError):
        transport._at_version(9)


def test_reader_still_translates_not_found() -> None:
    transport = _transport(OSError("version 9 not found"))
    with pytest.raises(NotFoundError):
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
    """The inline substring matching is gone — both sites go through errors.is_not_found."""
    src_dir = Path(registry_mod.__file__).parent
    for name in ("registry.py", "reader.py"):
        source = (src_dir / name).read_text()
        assert '"does not exist" in' not in source and '"not found" in' not in source, (
            f"{name} still classifies not-found inline instead of via lancekit.errors"
        )
        assert "is_not_found" in source
