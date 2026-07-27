"""#78 format honesty — the create path rejects a client that tries to select a non-Lance file format
instead of silently echoing the ignored property back."""

from __future__ import annotations

import pytest
from catalog.api.v1.endpoints.data import _reject_unsupported_format
from lance_namespace import InvalidInputError


@pytest.mark.parametrize(
    "props",
    [
        {"write.format.default": "parquet"},
        {"write.format.default": "ORC"},
        {"data_source_format": "avro"},
        {"data_source_format": "DELTA"},
    ],
)
def test_rejects_non_lance_format(props: dict[str, str]) -> None:
    with pytest.raises(InvalidInputError):
        _reject_unsupported_format(props)


@pytest.mark.parametrize(
    "props",
    [
        None,
        {},
        {"some.other.prop": "value"},
        {"write.format.default": "lance"},  # explicitly Lance is fine
        {"write.format.default": "LANCE"},
    ],
)
def test_allows_lance_or_absent(props: object) -> None:
    _reject_unsupported_format(props)  # no raise
