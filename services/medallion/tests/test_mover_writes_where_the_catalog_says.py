"""The mover writes to the location the catalog vends, not to one it composed.

This is rule I2 finally applied to the write side. The mover used to build
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
from medallion.services import transform


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
    real = transform.transform_stage

    def _spy(from_uri: str, to_uri: str, *a: Any, **k: Any) -> Any:
        written.append(to_uri)
        return real(from_uri, str(upstream / "actual.lance"), *a, **k)

    monkeypatch.setattr(transform, "transform_stage", _spy)
    monkeypatch.setattr(
        transform.catalog_register,
        "ensure_stage_output",
        lambda **k: str(upstream / VENDED),
    )
    return written


class TestTheVendedLocationWins:
    def test_the_mover_writes_where_the_catalog_says(self, wrote_to: list[str], upstream: Path) -> None:
        asyncio.run(transform.handle_stage(cast("Any", _Dapr()), _settings(upstream), _event()))

        assert wrote_to, "the compute step never ran"
        assert wrote_to[0] == str(upstream / VENDED), f"the mover wrote to a composed path ({wrote_to[0]!r}) instead of the vended one"

    def test_the_composed_URI_is_not_used_when_the_catalog_governs_the_lane(self, wrote_to: list[str], upstream: Path) -> None:
        """`MEDALLION_TO_URI` is the single-tenant fallback for an ungoverned deployment. With a
        catalog present it must not decide where governed data lands."""
        asyncio.run(transform.handle_stage(cast("Any", _Dapr()), _settings(upstream), _event()))

        assert "composed.lance" not in wrote_to[0]


class TestWithoutACatalog:
    def test_an_ungoverned_deployment_still_uses_its_configured_URI(self, monkeypatch: pytest.MonkeyPatch, upstream: Path) -> None:
        """No catalog URL is the dev/local shape — it already warns loudly that the write is
        ungoverned, and must keep working rather than failing to resolve a location."""
        written: list[str] = []
        real = transform.transform_stage
        monkeypatch.setattr(
            transform,
            "transform_stage",
            lambda f, t, *a, **k: (written.append(t), real(f, str(upstream / "out.lance"), *a, **k))[1],
        )

        asyncio.run(transform.handle_stage(cast("Any", _Dapr()), _settings(upstream, MEDALLION_CATALOG_URL=""), _event()))

        assert written and "composed.lance" in written[0]


class TestThereIsNothingLeftToRegister:
    """`ensure_stage_output` CREATES the table, which registers it. Registering again afterwards was
    not merely redundant — under per-tenant routing it reintroduced the P0 it was part of: the old
    `register_stage_output` resolved the location against a single hardwired `MEDALLION_CATALOG_ROOT`,
    while a tenant's vended location lives in that tenant's warehouse (`s3://acme-bucket/<hash>_silver$features`),
    so the call raised AFTER the Lance write had committed — ungoverned bytes plus a poison retry no
    redelivery could clear.

    THE ASSERTION IS NOW STRUCTURAL. That door has been deleted, so the mover cannot make the call
    even by mistake, and a test that stubbed it would be stubbing nothing. What is left worth pinning
    is the property the deletion bought: resolving the location is the LAST catalog call before the
    write, and no second call re-states where the table lives.
    """

    def test_the_mover_makes_no_catalog_call_that_RE_STATES_the_location(self, monkeypatch: pytest.MonkeyPatch, upstream: Path) -> None:
        from medallion.services import catalog_register

        assert not hasattr(catalog_register, "register_stage_output"), (
            "the telling-after-the-fact door is back; a mover that both asks and tells has two answers for where its table lives"
        )

        asked: list[dict[str, Any]] = []
        monkeypatch.setattr(transform.catalog_register, "ensure_stage_output", lambda **k: asked.append(k) or str(upstream / "vended.lance"))

        asyncio.run(transform.handle_stage(cast("Any", _Dapr()), _settings(upstream), _event()))

        assert len(asked) == 1, f"the location was resolved {len(asked)} times for one write"
