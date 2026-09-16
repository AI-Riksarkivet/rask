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

WHERE THE VALUE COMES FROM IS A DIFFERENT QUESTION, and it went unanswered for longer than this file
did: the parameter landed here and its only producer could not fill it — `plan_sweep` set
`item.table_id` from `table_id_from_uri`, which is exactly the parser this module is about. The sweep
reads the dataset's own stamp now ([[LH-141]]), and what it hands over is checked against the catalog's
location for that id rather than trusted.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials
from service_kit.lakehouse.table_locations import table_id_from_location


AMBIENT = {"aws_access_key_id": "minioadmin"}
SCOPED = {"aws_access_key_id": "SCOPED"}
CASCADE_URI = "s3://lance-catalog/medallion/bronze"
FLAT_URI = "s3://lance-catalog/6ecbe11e_transcripts_v2$annotations"


def _settings() -> MaintenanceSettings:
    return MaintenanceSettings(MAINTENANCE_S3_BUCKET="lance-catalog", MAINTENANCE_CATALOG_URL="http://catalog:2333")


def _vending(location: str | None, asked: list[str]) -> Callable[[str, MaintenanceSettings], credentials.Vended | None]:
    """A vend double that answers with BOTH halves the real one does.

    The location is not decoration: `write_options_for` refuses a credential that does not cover the
    dataset being maintained ([[LH-141]]), so a double returning bare options would pass by never
    reaching the check its caller now runs — which is the shape of double this suite has been bitten by
    before.
    """

    def _vend(table_id: str, settings: MaintenanceSettings) -> credentials.Vended | None:
        asked.append(table_id)
        return None if location is None else credentials.Vended(options=dict(SCOPED), location=location)

    return _vend


def test_the_cascade_layout_really_is_underivable() -> None:
    """If this ever starts answering, the rest of this module is testing nothing."""
    assert table_id_from_location(CASCADE_URI) is None


def test_the_declared_identity_is_what_gets_vended(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr(credentials, "_vend", _vending(CASCADE_URI, asked))

    options = credentials.write_options_for(CASCADE_URI, _settings(), fallback=AMBIENT, declared_table_id="bronze$events")

    assert asked == ["bronze$events"], "the cascade's rewrite was not vended against its own table"
    assert options == SCOPED, "the cascade's rewrite is still signed by the root key"


def test_derivation_still_serves_a_unit_that_declares_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr(credentials, "_vend", _vending(FLAT_URI, asked))

    credentials.write_options_for(FLAT_URI, _settings(), fallback=AMBIENT, declared_table_id=None)

    assert asked == ["transcripts_v2$annotations"]


def test_a_declared_identity_is_not_second_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong stamp must surface as a failed vend on a real table, not be silently repaired by
    derivation — otherwise a producer's bug hides behind a sweep that looks fine."""
    asked: list[str] = []
    monkeypatch.setattr(credentials, "_vend", _vending(None, asked))

    options = credentials.write_options_for(FLAT_URI, _settings(), fallback=AMBIENT, declared_table_id="other$table")

    assert asked == ["other$table"], "the declared id was discarded in favour of the path"
    assert options == AMBIENT


def test_no_catalog_configured_still_means_the_ambient_credential() -> None:
    bare = MaintenanceSettings(MAINTENANCE_S3_BUCKET="lance-catalog")
    assert credentials.write_options_for(CASCADE_URI, bare, fallback=AMBIENT, declared_table_id="bronze$events") == AMBIENT
