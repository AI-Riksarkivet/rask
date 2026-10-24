"""SK-04 — the reader's "missing thing" site, on the estate's one absence vocabulary.

pylance exposes no typed error for a missing table/version, so the reader's ``_at_version`` classifies by
message substring, as the client kit's registry and introspection do. Before the shared helper they had
already drifted: the reader matched only "not found", so an OSError saying "does not exist" escaped as a
raw 500 instead of the 404 the registry returned for the same condition.

That helper was `lancekit.errors.is_not_found`, and it has been folded into
`lancekit.absence.reads_as_absent` — the write and reconcile paths were asking the same question of a
second shared classifier with the opposite width, which is the drift this file gates, one level up.

The writer's commit-conflict markers are deliberately NOT part of this: a lost OCC race is a different
condition (409, re-read and re-send), not a not-found.
"""

from __future__ import annotations

from typing import cast

import lance
import pytest

from service_kit.exceptions import NotFoundError
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
