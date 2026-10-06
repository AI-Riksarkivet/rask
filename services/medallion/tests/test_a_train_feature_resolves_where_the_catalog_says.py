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

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from medallion.core.config import MedallionSettings
from medallion.services import ray_submit, train


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


def _features_the_job_reads(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, Any]]]:
    """The Ray Jobs API behind the Ray adapter: each submission's feature list, as the job will read it."""
    seen: list[list[dict[str, Any]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(json.loads(request.content)["runtime_env"]["env_vars"]["RASK_PARAM_FEATURES"]))
        return httpx.Response(200)

    client = httpx.AsyncClient(base_url="http://ray-head:8265", transport=httpx.MockTransport(handle))

    async def _client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(ray_submit, "ray_client", _client)
    return seen


def _local(root: Path) -> dict[str, str]:
    """A consumer plans each run into a control root and reads its registry's version first (CP-029): both local."""
    return {"MEDALLION_BRONZE_URI": f"{root}/medallion/bronze", "MEDALLION_CONTROL_ROOT": f"{root}/control"}


def test_an_UNREGISTERED_dataset_keeps_the_composed_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`None` is an answer, not a failure — an external producer's unregistered dataset is supported."""
    monkeypatch.setattr(train.catalog_register, "describe_table_location", lambda **_kw: None)
    settings = _settings(MEDALLION_CATALOG_URL="http://catalog:2333")

    assert train.feature_uri_for(settings, "silver$features") == "s3://lake/medallion/silver"


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


def test_the_ray_JOB_is_given_the_location_the_door_RESOLVED(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The hop the legs above cannot see: validation asks the catalog, the submission composed a path.

    `feature_uri_for` is correct and was called in exactly one place — `_resolve_version`, which opens
    the dataset locally to pin a version. The URI the Ray job actually reads is built separately, and it
    was `stage_uri_for`. So the door validated against the governed location and then trained against
    `<base>/medallion/<stage>`, a path the catalog never vended.

    MEASURED ON THE DEPLOYED ESTATE 2026-09-24, through `tests/e2e-py/test_governed_union_e2e.py`: the
    run lands attributed and FAILS with
    `train: Dataset at path medallion/silver/_versions/5.manifest was not found` — and `medallion/silver`
    holds ZERO keys on that estate while 27 paths matching `silver` exist under catalog-managed names.
    A gate on the innermost call proves nothing about the hop that does the work.
    """
    monkeypatch.setattr(train.catalog_register, "describe_table_location", lambda **_kw: "s3://tenant-wh/90fabc")
    seen = _features_the_job_reads(monkeypatch)
    event = {"data": {"token": "t1", "model": "churn", "features": [{"dataset": "silver$features", "version": 7}]}}

    local = _settings(MEDALLION_CATALOG_URL="http://catalog:2333", **_local(tmp_path))
    outcome = asyncio.run(train.handle_train_trigger(local, event, dapr=object()))

    assert outcome == {"status": "SUCCESS"}
    assert seen == [[{"dataset": "silver$features", "version": 7, "uri": "s3://tenant-wh/90fabc"}]]
