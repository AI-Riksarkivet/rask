"""A unit's DECLARED table id is what the rewrite is vended against, not one recovered from the path.

`write_options_for` derived the identity from the dataset URI. That covers a minority of the estate,
and the shortfall is the part that matters rather than an edge case. Measured against the live
warehouse, of eleven top-level roots in `s3://lance-catalog/`, `table_id_from_location` answers for
six; the five it cannot read are `bronze`, `ingest`, `medallion`, `media-src` and `models` — and
`medallion/` is the whole cascade, which `docs/DECISIONS.md` names as the highest-churn writer in
the estate.

Those five are NOT unknown to the catalog, which is the finding that makes this worth fixing rather
than accepting: `bronze$events` (at `s3://lance-catalog/medallion/bronze`) and `bronze$pages` (at
`s3://lance-catalog/bronze/pages`) both answer a write-tier vend with 200. The identity exists and is
authorized; only the parser cannot see it in the path. So the producer stamps it on the unit and the
executor prefers it — leaving derivation as the fallback for a unit built before the field existed.

The failure this prevents is the quiet one: derive-only leaves the cascade tier signing with the root
key while every log line, every counter and every test stays green.
"""

from __future__ import annotations

import pytest

from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials
from service_kit.lakehouse.table_locations import table_id_from_location


AMBIENT = {"aws_access_key_id": "rustfsadmin"}
CASCADE_URI = "s3://lance-catalog/medallion/bronze"


def _settings() -> MaintenanceSettings:
    return MaintenanceSettings(MAINTENANCE_S3_BUCKET="lance-catalog", MAINTENANCE_CATALOG_URL="http://catalog:2333")


def test_the_cascade_layout_really_is_underivable() -> None:
    """If this ever starts answering, the rest of this module is testing nothing."""
    assert table_id_from_location(CASCADE_URI) is None


def test_the_declared_identity_is_what_gets_vended(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def _vend(table_id: str, settings: MaintenanceSettings) -> dict[str, str] | None:
        asked.append(table_id)
        return {"aws_access_key_id": "SCOPED"}

    monkeypatch.setattr(credentials, "_vend", _vend)
    options = credentials.write_options_for(CASCADE_URI, _settings(), fallback=AMBIENT, declared_table_id="bronze$events")

    assert asked == ["bronze$events"], "the cascade's rewrite was not vended against its own table"
    assert options == {"aws_access_key_id": "SCOPED"}, "the cascade's rewrite is still signed by the root key"


def test_derivation_still_serves_a_unit_that_declares_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def _vend(table_id: str, settings: MaintenanceSettings) -> dict[str, str] | None:
        asked.append(table_id)
        return {"aws_access_key_id": "SCOPED"}

    monkeypatch.setattr(credentials, "_vend", _vend)
    flat = "s3://lance-catalog/6ecbe11e_transcripts_v2$annotations"
    credentials.write_options_for(flat, _settings(), fallback=AMBIENT, declared_table_id=None)
    assert asked == ["transcripts_v2$annotations"]


def test_a_declared_identity_is_not_second_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong stamp must surface as a failed vend on a real table, not be silently repaired by
    derivation — otherwise a producer's bug hides behind a sweep that looks fine."""
    asked: list[str] = []

    def _vend(table_id: str, settings: MaintenanceSettings) -> dict[str, str] | None:
        asked.append(table_id)
        return None

    monkeypatch.setattr(credentials, "_vend", _vend)
    flat = "s3://lance-catalog/6ecbe11e_transcripts_v2$annotations"
    options = credentials.write_options_for(flat, _settings(), fallback=AMBIENT, declared_table_id="other$table")

    assert asked == ["other$table"], "the declared id was discarded in favour of the path"
    assert options == AMBIENT


def test_no_catalog_configured_still_means_the_ambient_credential() -> None:
    bare = MaintenanceSettings(MAINTENANCE_S3_BUCKET="lance-catalog")
    assert credentials.write_options_for(CASCADE_URI, bare, fallback=AMBIENT, declared_table_id="bronze$events") == AMBIENT
