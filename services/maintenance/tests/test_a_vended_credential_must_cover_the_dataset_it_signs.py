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
carries `location` — the catalog's own answer for where that table lives — and the client read only
`credentials.storage_options`. So the check costs no extra call: if the catalog's location does not
cover the URI being maintained, the two disagree and the unit stops.

CONTAINMENT, NEVER EQUALITY, and this is the half that would break the estate if it were got wrong. A
branch dataset is swept at `<root>/tree/<branch>` while the catalog's location for its table is the
root; the live sweep holds several (`s3://tracka-wh/425bbde2_tracka$rd2_28877454/tree/dev`). Equality
would refuse every one of them — a guard that fires on the healthy majority and is then turned off.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from pydantic import SecretStr

from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials


pytestmark = pytest.mark.usefixtures("catalog_identity_token")

CATALOG = "http://catalog:2333"
SCOPED = StorageOptions(key_pair=S3KeyPair(access_key_id="SCOPED", secret_access_key=SecretStr("scoped-secret")))


def _settings() -> MaintenanceSettings:
    return MaintenanceSettings(MAINTENANCE_S3_BUCKET="lance-catalog", MAINTENANCE_CATALOG_URL=CATALOG)


@pytest.fixture
def door() -> Iterator[respx.MockRouter]:
    """The catalog's side of the wire: a request no route matches fails the test."""
    with respx.mock() as router:
        yield router


def _vends(door: respx.MockRouter, table_id: str, location: str | None) -> None:
    """The maintain-tier door answering ``table_id``'s vend with the scoped pair, at ``location``."""
    flat = {"aws_access_key_id": "SCOPED", "aws_secret_access_key": "scoped-secret"}
    answer = {"mode": "direct", "credentials": {"storage_options": flat}, **({"location": location} if location is not None else {})}
    door.post(f"{CATALOG}/management/v1/table/{table_id}/credentials", params={"tier": "maintain"}).mock(return_value=httpx.Response(200, json=answer))


@pytest.mark.parametrize(
    ("answered", "held"),
    [
        # The case equality would break: the sweep holds a branch, the catalog answers with the root.
        pytest.param("s3://tracka-wh/425bbde2_tracka$rd2_28877454", "s3://tracka-wh/425bbde2_tracka$rd2_28877454/tree/dev", id="a-branch-under-the-root"),
        # The catalog answers the URL `describe_table` does, percent-encoding the name; the sweep holds the directory
        # the store keeps ([[LH-319]]).
        pytest.param(
            "s3://tracka-wh/425bbde2_tracka$r%C3%A4ksm%C3%B6rg%C3%A5s", "s3://tracka-wh/425bbde2_tracka$räksmörgås", id="the-root-as-the-catalog-spells-it"
        ),
    ],
)
def test_a_dataset_under_the_table_root_is_covered(door: respx.MockRouter, answered: str, held: str) -> None:
    _vends(door, "tracka$rd2_28877454", answered)

    options = credentials.write_options_for(held, _settings(), table_id="tracka$rd2_28877454")

    assert options == SCOPED, f"{held} was refused a credential vended for its own table root {answered}"


def test_a_vend_that_names_no_location_proceeds_and_says_so(door: respx.MockRouter, caplog: pytest.LogCaptureFixture) -> None:
    """A missing location means the catalog did not tell us, NOT that there is a crossing.

    Refusing on absence would stop maintaining the whole estate the day the response shape changed —
    the same asymmetry `_may_write_anything` states: an uncertain answer must never be the one that
    silently stops maintenance. So it proceeds, loudly, and this test is what makes that a decision
    rather than an oversight.
    """
    _vends(door, "bronze$events", None)

    with caplog.at_level("WARNING"):
        options = credentials.write_options_for("s3://bind86-wh/medallion/silver", _settings(), table_id="bronze$events")

    assert options == SCOPED
    assert any("location" in record.getMessage() for record in caplog.records), "an unverifiable vend passed silently"
