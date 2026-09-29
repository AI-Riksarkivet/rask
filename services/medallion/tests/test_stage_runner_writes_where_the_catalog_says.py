"""The stage runner writes to the location the catalog vends, not to one it composed.

This is rule I2 finally applied to the write side. The stage runner used to build
`{root}/medallion/{tier}` — a layout the catalog has never produced — write there, and then tell the
catalog that was the table's home. Measured live: the catalog's own binding said
`s3://bind86-wh/medallion/silver`, so the publish that followed opened the catalog's answer, found no
dataset and 500'd. The bytes were real and governed as living somewhere they did not.

It is a REORDERING, not a swap. Asking has to happen BEFORE the write, where registering happened
after it — so the vended URI is what `transform_stage` writes to, and there is nothing left to
register afterwards because the create already did it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pytest

from medallion.core.config import MedallionSettings
from medallion.services import inprocess_executor, transform


VENDED = "the-catalog-said-here"


class _Dapr:
    def __init__(self) -> None:
        self.topics: list[str] = []

    async def publish_event(self, **kwargs: Any) -> None:
        self.topics.append(kwargs["topic_name"])


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    lance.write_dataset(pa.table({"id": [1, 2, 3]}), str(tmp_path / "bronze.lance"))
    return tmp_path


def _settings(tmp_path: Path, **over: Any) -> MedallionSettings:
    base: dict[str, Any] = {
        "MEDALLION_FROM_NAMESPACE": "bronze",
        "MEDALLION_FROM_DATASET": "bronze$events",
        "MEDALLION_TO_NAMESPACE": "silver",
        "MEDALLION_TO_DATASET": "silver$features",
        "MEDALLION_PUB_TOPIC": "medallion.silver",
        "MEDALLION_COMPUTE_ENABLED": "true",
        "MEDALLION_CATALOG_URL": "http://catalog.test",
        "MEDALLION_FROM_URI": str(tmp_path / "bronze.lance"),
        "MEDALLION_TO_URI": str(tmp_path / "composed.lance"),
    }
    return MedallionSettings(**{**base, **over})


def _event() -> dict[str, Any]:
    return {"data": {"token": "tok", "dataset": "bronze$events", "namespace": "bronze"}}


@pytest.fixture
def wrote_to(monkeypatch: pytest.MonkeyPatch, upstream: Path) -> list[str]:
    """Capture the URI the compute step actually writes to."""
    written: list[str] = []
    real = inprocess_executor.transform_stage

    def _spy(from_uri: str, to_uri: str, *a: Any, **k: Any) -> Any:
        written.append(to_uri)
        return real(from_uri, str(upstream / "actual.lance"), *a, **k)

    monkeypatch.setattr(inprocess_executor, "transform_stage", _spy)
    monkeypatch.setattr(
        transform.catalog_register,
        "ensure_stage_output",
        lambda **k: str(upstream / VENDED),
    )
    # The stage runner asks the catalog TWICE: where to WRITE (above) and where its UPSTREAM lives. `None` is
    # the catalog's "I govern no such table", which keeps this suite on its composed upstream — the
    # subject here is the WRITE location, and a vended upstream would change what is read, not where.
    monkeypatch.setattr(transform.catalog_register, "describe_table_location", lambda **_: None)
    return written


class TestTheVendedLocationWins:
    def test_the_stage_runner_writes_where_the_catalog_says(self, wrote_to: list[str], upstream: Path) -> None:
        asyncio.run(transform.handle_stage(cast("Any", _Dapr()), _settings(upstream), _event()))

        assert wrote_to, "the compute step never ran"
        assert wrote_to[0] == str(upstream / VENDED), f"the stage runner wrote to a composed path ({wrote_to[0]!r}) instead of the vended one"
