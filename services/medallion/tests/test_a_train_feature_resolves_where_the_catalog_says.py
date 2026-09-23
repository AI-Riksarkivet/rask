"""The train door asks the catalog where a feature table lives instead of composing a path.

`_resolve_version` opened `stage_uri_for(settings, dataset)` — `silver$features` -> `<base>/medallion/
silver` — and `stage_uri_for`'s own docstring called that a "demo-tier convention", noting that a
catalog-registered feature table "would resolve through describe instead". Against a governed estate
that convention is simply wrong: the catalog vends an opaque per-table location, so the composed path
names nothing and every training submission is refused.

MEASURED ON THE DEPLOYED ESTATE 2026-09-23: `POST /train` answers
`422 cannot resolve feature dataset 'silver$features'` while the catalog describes that same table at
`s3://bind86-wh/90f…`. The table exists, is governed, and the door cannot find it.

THIS IS THE `ensure_stage_output` LESSON ON THE READ SIDE — "the stage runner asks instead of telling"
— and the seam already exists: `describe_table_location` is the read-side question, documented to
answer `None` (not raise) for a table the catalog does not govern, precisely so a caller can fall back
to its composed path. The composed path stays for the no-catalog demo shape and for an unregistered
dataset; it stops being the FIRST answer.
"""

from __future__ import annotations

from typing import Any

import pytest

from medallion.core.config import MedallionSettings
from medallion.services import train


def _settings(**overrides: Any) -> MedallionSettings:
    values: dict[str, Any] = {
        "MEDALLION_RAY_ENABLED": "true",
        "MEDALLION_COMPUTE_ENABLED": "true",
        "MEDALLION_S3_ENDPOINT": "http://rustfs:9000",
        "MEDALLION_S3_SECRET_ACCESS_KEY": "k",
        "MEDALLION_BRONZE_URI": "s3://lake/medallion/bronze",
    }
    values.update(overrides)
    return MedallionSettings.model_validate(values)


def test_a_governed_feature_table_resolves_to_the_catalogs_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect: the door opened a composed path while the catalog vended somewhere else."""
    asked: list[str] = []

    def _describe(*, table_id: str, **_kw: Any) -> str:
        asked.append(table_id)
        return "s3://tenant-wh/90fabc_silver$features"

    monkeypatch.setattr(train.catalog_register, "describe_table_location", _describe)
    settings = _settings(MEDALLION_CATALOG_URL="http://catalog:2333")

    assert train.feature_uri_for(settings, "silver$features") == "s3://tenant-wh/90fabc_silver$features"
    assert asked == ["silver$features"], "the door did not ask the catalog"


def test_an_UNREGISTERED_dataset_keeps_the_composed_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`None` is an answer, not a failure — an external producer's unregistered dataset is supported."""
    monkeypatch.setattr(train.catalog_register, "describe_table_location", lambda **_kw: None)
    settings = _settings(MEDALLION_CATALOG_URL="http://catalog:2333")

    assert train.feature_uri_for(settings, "silver$features") == "s3://lake/medallion/silver"


def test_with_NO_catalog_the_door_never_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The demo shape has no catalog to ask, and must not pay a request to discover that.

    RECORDED, NOT RAISED. A tripwire that raises cannot fire here: the outage fallback catches every
    exception on purpose, so an `AssertionError` from the double is swallowed and the test passes
    whether or not the call was made. Caught by mutation — removing the no-catalog guard left this
    green.
    """
    calls: list[str] = []

    def _record(*, table_id: str, **_kw: Any) -> str:
        calls.append(table_id)
        return "s3://should-not-be-used/x"

    monkeypatch.setattr(train.catalog_register, "describe_table_location", _record)

    assert train.feature_uri_for(_settings(), "silver$features") == "s3://lake/medallion/silver"
    assert calls == [], "asked a catalog that is not configured"


def test_a_CATALOG_OUTAGE_falls_back_rather_than_refusing_the_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable catalog must not turn every training submission into a 422.

    The composed path is what this door used unconditionally until now, so falling back to it during an
    outage is strictly no worse than the behaviour it replaces — while raising would make a transient
    catalog fault look like an invalid request.
    """

    def _boom(**_kw: Any) -> str:
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr(train.catalog_register, "describe_table_location", _boom)
    settings = _settings(MEDALLION_CATALOG_URL="http://catalog:2333")

    assert train.feature_uri_for(settings, "silver$features") == "s3://lake/medallion/silver"
