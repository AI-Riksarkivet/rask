"""A store outage on the model registry must answer 503, not 404 (CAT-CORE-06).

`services/models.py::_open` collapsed EVERY `OSError` to `TableNotFoundError`/`TableVersionNotFoundError`
(404) — so a store outage (connection refused, S3 timeout) surfaced as "model registry not found". That
sends whoever is on call to the wrong system and contradicts the estate's own direction (f28db891, "commit
idempotency guard assumed innocence when the store went dark"). A genuinely missing dataset
(`FileNotFoundError`, a bare pylance `ValueError`) is still a 404; a non-`FileNotFound` `OSError` is a 503.
"""

from __future__ import annotations

import lance
import pytest
from lance_namespace import ServiceUnavailableError, TableNotFoundError, TableVersionNotFoundError

from catalog.services import models


def test_a_store_outage_is_a_503_not_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> lance.LanceDataset:
        raise OSError("connection refused")

    monkeypatch.setattr(lance, "dataset", _boom)

    with pytest.raises(ServiceUnavailableError):
        models.describe("s3://b/models/asr", {})
    with pytest.raises(ServiceUnavailableError):
        models.metrics_at("s3://b/models/asr", {}, version=3)


def test_a_missing_registry_is_still_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(*_a: object, **_k: object) -> lance.LanceDataset:
        raise FileNotFoundError("no such dataset")

    monkeypatch.setattr(lance, "dataset", _missing)

    with pytest.raises(TableNotFoundError):
        models.describe("s3://b/models/asr", {})


def test_a_bad_version_is_still_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad(*_a: object, **_k: object) -> lance.LanceDataset:
        raise ValueError("version 99 not found")

    monkeypatch.setattr(lance, "dataset", _bad)

    with pytest.raises(TableVersionNotFoundError):
        models.metrics_at("s3://b/models/asr", {}, version=99)
