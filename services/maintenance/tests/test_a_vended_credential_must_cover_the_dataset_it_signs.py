"""A write credential vended for table X must cover the dataset the sweep is actually holding.

[[LH-141]]. Measured on the live estate 2026-09-16, one tick: 59 datasets wrote, 56 signed by a vended
table-scoped credential and 3 signed by the AMBIENT one with no vend decision at all — and all three
are the composed `medallion/<tier>` paths whose id `table_id_from_location` cannot derive:

    s3://bind86-wh/medallion/silver     stamped bronze$events    indices_optimized=1
    s3://lance-catalog/medallion/silver stamped silver$features  indices_optimized=1
    s3://lance-catalog/medallion/gold   stamped bronze$events    indices_optimized=1

Two datasets in two different buckets both claim to be `bronze$events`, and the catalog governs that
id at a THIRD location, `s3://lance-catalog/medallion/bronze`. The sweep sends that id to
`/compaction_plan` every tick and the door answers 200 — it was asked a question about a table, not
about the dataset the caller is holding, so the 200 is correct and the crossing is invisible to it.

THE DISCRIMINATOR IS ALREADY IN THE VEND RESPONSE AND WAS BEING DISCARDED. `CredentialResponse`
carries `location` — the catalog's own answer for where that table lives — and `_vend` read only
`credentials.storage_options`. So the check costs no extra call: if the catalog's location does not
cover the URI being maintained, the two disagree and the unit stops.

CONTAINMENT, NEVER EQUALITY, and this is the half that would break the estate if it were got wrong. A
branch dataset is swept at `<root>/tree/<branch>` while the catalog's location for its table is the
root; the live sweep holds several (`s3://tracka-wh/425bbde2_tracka$rd2_28877454/tree/dev`). Equality
would refuse every one of them — a guard that fires on the healthy majority and is then turned off.
"""

from __future__ import annotations

import pytest

from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials
from maintenance.services.compaction_executor import GovernedElsewhere


AMBIENT = {"aws_access_key_id": "minioadmin"}
SCOPED = {"aws_access_key_id": "SCOPED"}


def _settings() -> MaintenanceSettings:
    return MaintenanceSettings(MAINTENANCE_S3_BUCKET="lance-catalog", MAINTENANCE_CATALOG_URL="http://catalog:2333")


def _vends(location: str | None) -> object:
    def _vend(table_id: str, settings: MaintenanceSettings) -> credentials.Vended:
        return credentials.Vended(options=dict(SCOPED), location=location)

    return _vend


def test_the_live_crossing_stops_the_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured case: a silver dataset in a tenant warehouse stamped with a bronze table's id.

    `GovernedElsewhere`, not the door's own `MaintenanceDenied`: the catalog vended, and the table it names
    is healthy, so the sweep must not count this under that table's id."""
    monkeypatch.setattr(credentials, "_vend", _vends("s3://lance-catalog/medallion/bronze"))

    with pytest.raises(GovernedElsewhere) as refusal:
        credentials.write_options_for("s3://bind86-wh/medallion/silver", _settings(), fallback=AMBIENT, declared_table_id="bronze$events")

    message = str(refusal.value)
    assert "s3://bind86-wh/medallion/silver" in message, "the refusal does not say which dataset was being maintained"
    assert "s3://lance-catalog/medallion/bronze" in message, "the refusal does not say where the catalog says that table lives"
    assert "bronze$events" in message, "the refusal does not name the id the two disagree about"


def test_a_credential_for_this_very_dataset_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "_vend", _vends("s3://lance-catalog/medallion/bronze"))

    options = credentials.write_options_for("s3://lance-catalog/medallion/bronze", _settings(), fallback=AMBIENT, declared_table_id="bronze$events")

    assert options == SCOPED


def test_a_branch_under_the_table_root_is_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case equality would break: the sweep holds a branch, the catalog answers with the root."""
    root = "s3://tracka-wh/425bbde2_tracka$rd2_28877454"
    monkeypatch.setattr(credentials, "_vend", _vends(root))

    options = credentials.write_options_for(f"{root}/tree/dev", _settings(), fallback=AMBIENT, declared_table_id="tracka$rd2_28877454")

    assert options == SCOPED, "a branch dataset was refused a credential vended for its own table root"


def test_two_spellings_of_one_path_are_not_a_crossing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`base_refs.normalise` is the estate's one comparator for this, and re-implementing it at a call
    site is what its own docstring calls the failure mode indistinguishable from having no guard."""
    monkeypatch.setattr(credentials, "_vend", _vends("/lance-catalog/medallion/bronze/"))

    options = credentials.write_options_for("s3://lance-catalog/medallion/bronze", _settings(), fallback=AMBIENT, declared_table_id="bronze$events")

    assert options == SCOPED


def test_a_vend_that_names_no_location_proceeds_and_says_so(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A missing location means the catalog did not tell us, NOT that there is a crossing.

    Refusing on absence would stop maintaining the whole estate the day the response shape changed —
    the same asymmetry `_may_write_anything` states: an uncertain answer must never be the one that
    silently stops maintenance. So it proceeds, loudly, and this test is what makes that a decision
    rather than an oversight.
    """
    monkeypatch.setattr(credentials, "_vend", _vends(None))

    with caplog.at_level("WARNING"):
        options = credentials.write_options_for("s3://bind86-wh/medallion/silver", _settings(), fallback=AMBIENT, declared_table_id="bronze$events")

    assert options == SCOPED
    assert any("location" in record.getMessage() for record in caplog.records), "an unverifiable vend passed silently"
