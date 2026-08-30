"""Unit tests for the #17/#92 model-artifact listing (``registry.list_artifacts`` + the describe wire-up).

The trainer lays plain-path artifacts out at ``<artifacts_root>/<model>/<token>/…`` — OUTSIDE any Lance
dataset — and the registry rows point at them. These tests pin the listing (relative paths, sizes, mtime,
``[]`` for an absent tree), the Settings derivation that must mirror medallion's ``artifact_base_for``
byte-for-byte (s3 → the bucket root's ``models/`` tree; local → a ``model-artifacts`` sibling), and the
describe endpoint carrying the list.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import lance
import pyarrow as pa

from catalog.api.v1.endpoints import models as ep
from catalog.core.config import Settings
from catalog.services import models as registry


def _tree(root: Path) -> Path:
    """One model's artifact tree: two publish tokens, three objects."""
    for token, name, payload in (
        ("tok1", "weights.json", b"wwwww"),
        ("tok1", "config.json", b"cc"),
        ("tok2", "weights.json", b"w2"),
    ):
        target = root / token
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_bytes(payload)
    return root


def test_list_artifacts_relative_paths_sizes_and_mtime(tmp_path: Path) -> None:
    tree = _tree(tmp_path / "demo")
    artifacts = registry.list_artifacts(str(tree), {})
    assert [a["path"] for a in artifacts] == ["tok1/config.json", "tok1/weights.json", "tok2/weights.json"]
    assert [a["size_bytes"] for a in artifacts] == [2, 5, 2]
    for a in artifacts:  # ISO-8601 mtime, parseable — or None when the filesystem reports none
        assert a["updated_at"] is None or isinstance(datetime.fromisoformat(a["updated_at"]), datetime)


def test_absent_tree_is_empty_never_an_error(tmp_path: Path) -> None:
    assert registry.list_artifacts(str(tmp_path / "nowhere"), {}) == []


def _settings(**overrides: Any) -> Settings:
    # model_validate (not Settings(...)) so field-name keys validate cleanly — the fields carry
    # LANCE_* aliases (the test_fga_model_contract pattern).
    return Settings.model_validate({"s3_access_key_id": "x", "s3_secret_access_key": "x", **overrides})


def test_artifacts_root_mirrors_medallion_layout_on_s3() -> None:
    # medallion's artifact_base_for: artifacts live at the BUCKET root's models/ tree, never inside the
    # registry dataset — the catalog must read exactly where the trainer writes.
    s = _settings(root="s3://lake/warehouse")
    assert s.models_root == "s3://lake/warehouse/medallion/models"
    assert s.model_artifacts_root == "s3://lake/models"
    assert s.model_artifacts_uri("churn") == "s3://lake/models/churn"


def test_artifacts_root_mirrors_medallion_layout_locally(tmp_path: Path) -> None:
    s = _settings(root=str(tmp_path))
    assert s.model_artifacts_root == f"{tmp_path}/medallion/model-artifacts"


def test_artifacts_root_override_wins() -> None:
    s = _settings(root="s3://lake", model_artifacts_base="s3://zoned/artifacts/")
    assert s.model_artifacts_root == "s3://zoned/artifacts"
    assert s.model_artifacts_uri("churn") == "s3://zoned/artifacts/churn"


def _publish_registry(uri: str) -> None:
    """A 1-version registry the shape the trainer publishes (mirrors test_model_promotion)."""
    meta = {"config": {}, "features": ["silver$features"], "metrics": {"rows_seen": 4}, "token": "tok1"}
    table = pa.table({"artifact": pa.array(["weights"], pa.string()), "meta": pa.array([json.dumps(meta)], pa.string())})
    lance.write_dataset(table, uri, mode="overwrite")


def test_describe_endpoint_carries_the_artifact_list(tmp_path: Path) -> None:
    registry_uri = str(tmp_path / "registry" / "demo")
    _publish_registry(registry_uri)
    artifacts_root = _tree(tmp_path / "artifacts" / "demo")
    settings: Any = SimpleNamespace(
        fga_enabled=False,
        delimiter="$",
        model_uri=lambda m: str(tmp_path / "registry" / m),
        model_artifacts_uri=lambda m: str(tmp_path / "artifacts" / m),
    )
    response = asyncio.run(ep.describe_model(model="demo", settings=settings, so={}, token=None, client=None))
    assert response.latest_version == 1
    assert [a.path for a in response.artifacts] == [
        "tok1/config.json",
        "tok1/weights.json",
        "tok2/weights.json",
    ]
    assert all(a.size_bytes > 0 for a in response.artifacts)
    assert artifacts_root.exists()


def test_describe_endpoint_reports_no_artifacts_as_empty(tmp_path: Path) -> None:
    registry_uri = str(tmp_path / "registry" / "demo")
    _publish_registry(registry_uri)
    settings: Any = SimpleNamespace(
        fga_enabled=False,
        delimiter="$",
        model_uri=lambda m: str(tmp_path / "registry" / m),
        model_artifacts_uri=lambda m: str(tmp_path / "artifacts" / m),  # never created
    )
    response = asyncio.run(ep.describe_model(model="demo", settings=settings, so={}, token=None, client=None))
    assert response.artifacts == []
