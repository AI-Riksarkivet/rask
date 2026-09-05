"""ONE Dapr secret store, named once and consumed once (docs/DECISIONS.md "The Python estate audit" DUP-09 + DUP-17).

Two duplications share one surface here, so they are pinned together:

* **DUP-17** — the estate runs exactly ONE Dapr secret-store component (`lance-secrets`, provisioned by
  `chart/templates/dapr-component.yaml`), and seven different env vars named it, each defaulting to
  the same literal. The chart sets NONE of them, so all seven ride the default and an operator who
  repoints the store must find and set all seven or leave part of the estate reading a component that
  no longer exists. `RASK_SECRET_STORE` was already the estate-wide name two services read; this pins
  that ONE name repointing every consumer, with each per-service alias kept as an override.
* **DUP-09** — the fetch-and-splice that consumes that store was written four times: three
  `apply_dapr_secrets` copies (lineage, medallion, maintenance) plus an inline block in the catalog's
  own lifespan. `service_kit.governed.secrets.apply_dapr_secrets` is the one implementation; nothing
  else may assign the S3 secret from a store bundle.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr


REPO = Path(__file__).resolve().parents[2]


def _settings_builders() -> list[tuple[str, Any, str]]:
    """(label, factory, attribute) for every settings object that names the one secret store."""
    from catalog.core.config import Settings as CatalogSettings
    from ingest.config import IngestSettings
    from lineage.core.config import LineageSettings
    from maintenance.core.config import MaintenanceSettings
    from medallion.core.config import MedallionSettings
    from service_kit.media.config import Settings as MediaSettings
    from viewer.core.config import ViewerSettings

    return [
        ("lineage", LineageSettings, "dapr_secret_store"),
        ("medallion", MedallionSettings, "dapr_secret_store"),
        ("catalog", CatalogSettings, "dapr_secret_store"),
        ("maintenance", MaintenanceSettings, "dapr_secret_store"),
        ("media.s3", MediaSettings, "s3_secret_store"),
        ("media.publish", MediaSettings, "publish_secret_store"),
        ("viewer", ViewerSettings, "secret_store"),
        ("ingest", IngestSettings, "secret_store"),
    ]


@pytest.mark.parametrize("label", [row[0] for row in _settings_builders()])
def test_one_env_var_repoints_every_consumer_of_the_one_secret_store(label: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """DUP-17: `RASK_SECRET_STORE` is the estate-wide name — setting it alone moves every consumer.

    RED before the collapse for six of the eight rows (only viewer and ingest read the shared name):
    each of the others answered the hardcoded default `lance-secrets` while the operator believed the
    estate had been repointed.
    """
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "x")  # catalog's one required field
    monkeypatch.setenv("RASK_SECRET_STORE", "prod-secrets")
    factory, attribute = next((f, a) for lbl, f, a in _settings_builders() if lbl == label)
    assert getattr(factory(), attribute) == "prod-secrets"


@pytest.mark.parametrize("label", [row[0] for row in _settings_builders()])
def test_the_per_service_alias_still_overrides_the_estate_wide_name(label: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The collapse must not REMOVE the per-service lever — a single service may still be repointed."""
    per_service = {
        "lineage": "LINEAGE_DAPR_SECRET_STORE",
        "medallion": "MEDALLION_DAPR_SECRET_STORE",
        "catalog": "LANCE_DAPR_SECRET_STORE",
        "maintenance": "MAINTENANCE_DAPR_SECRET_STORE",
        "media.s3": "MEDIA_S3_SECRET_STORE",
        "media.publish": "MEDIA_PUBLISH_SECRET_STORE",
        "viewer": "RASK_SECRET_STORE",
        "ingest": "RASK_SECRET_STORE",
    }[label]
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("RASK_SECRET_STORE", "estate-wide")
    monkeypatch.setenv(per_service, "just-this-one")
    factory, attribute = next((f, a) for lbl, f, a in _settings_builders() if lbl == label)
    assert getattr(factory(), attribute) == "just-this-one"


def test_the_store_to_settings_splice_has_exactly_one_implementation() -> None:
    """DUP-09: only `service_kit.governed.secrets` may assign the S3 secret from a store bundle.

    RED before the collapse: four modules carried the same `settings.s3_secret_access_key =
    SecretStr(bundle[...])` line — `lineage/core/config.py`, `medallion/core/config.py`,
    `maintenance/core/config.py` and, inline in a lifespan, `catalog/main.py`.
    """
    splice = re.compile(r"s3_secret_access_key\s*=\s*SecretStr\(\s*bundle")
    offenders = sorted(
        str(path.relative_to(REPO))
        for root in ("services", "packages")
        for path in (REPO / root).rglob("*.py")
        if "/tests/" not in str(path) and splice.search(path.read_text())
    )
    assert offenders == ["packages/service-kit/src/service_kit/governed/secrets.py"], offenders


def test_the_catalog_lifespan_consumes_the_store_through_the_shared_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """DUP-09's fourth copy is the one inline in a lifespan — the catalog's. It must call the seam.

    RED before the collapse: `catalog/main.py` fetched and spliced the bundle itself, so this
    monkeypatch was never reached and `seen` stayed empty.
    """
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "x")  # `catalog.main` builds settings at import
    import catalog.main as catalog_main
    import service_kit.governed.secrets as shared
    from catalog.core.config import Settings as CatalogSettings

    # The name it holds IS the shared seam, not a same-named local copy.
    assert catalog_main.apply_dapr_secrets is shared.apply_dapr_secrets

    seen: list[object] = []

    def _record(settings: object) -> dict[str, str]:
        seen.append(settings)
        return {"rustfs-secret-key": "from-store"}

    monkeypatch.setattr(catalog_main, "apply_dapr_secrets", _record)
    settings = CatalogSettings.model_validate({"s3_access_key_id": "x", "secrets_from_dapr": True, "s3_secret_access_key": SecretStr("")})
    catalog_main.consume_dapr_secrets(settings)
    assert seen == [settings]
