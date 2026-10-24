"""The registry's ``table_dataset`` reads a missing table as the estate's one absence, a 404.

pylance exposes no typed error for a missing table, so the registry classifies by message substring through
``service_kit.lancekit.absence.reads_as_absent``, the vocabulary every plane shares.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from client_kit.lancekit import registry as registry_mod
from service_kit.exceptions import NotFoundError


def test_registry_translates_missing_table_into_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = SimpleNamespace(
        id="ds",
        path=Path("/nonexistent"),
        storage_options={"endpoint": "http://s3:9000"},  # non-None → the local dir check is skipped
        table_uri=lambda table: f"s3://bucket/ds.lance/{table}.lance",
        sync_table_info=lambda table, version: None,
    )

    def raise_missing(uri: str, storage_options: dict[str, str] | None = None, *, session: object | None = None) -> None:
        # The double mirrors `lance.dataset`'s real signature, `session` included. It did not, so when
        # `table_dataset` began threading a session ([[LH-096]]) this stand-in raised TypeError and the
        # test failed for a reason that had nothing to do with the classifier it exists to pin.
        raise OSError("LanceError(IO): Object at location s3://bucket/ds.lance/t.lance does not exist")

    monkeypatch.setattr(registry_mod.lance, "dataset", raise_missing)
    with pytest.raises(NotFoundError):
        # cast: a duck-typed stand-in — table_dataset only reads the attributes
        # scripted above, and building a real DatasetHandle needs a live dataset.
        registry_mod.table_dataset(cast("registry_mod.DatasetHandle", handle), "t")
