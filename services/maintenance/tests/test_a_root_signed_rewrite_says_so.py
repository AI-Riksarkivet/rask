"""A rewrite that falls back to the root key must SAY it did — including when vending is switched off.

`credentials.write_options_for`'s own docstring promises "Every failure degrades to the ambient
credential and says so". Three of its four exits keep that promise (unreachable, unavailable,
unparseable, plus the unresolvable-location debug). The FOURTH — an empty `catalog_url`, i.e. vending
not configured at all — returned the fallback in silence.

MEASURED ON THE LIVE ESTATE 2026-09-09: `maintenance.core.config.get_settings().catalog_url` is `''`
in the running pod (`maintenance.vendWriteCredentials` is the chart default, false), so every
compaction in the estate was signing with the root object-store key, and the whole `lance.audit`
`vend_credentials` stream stopped 15 hours earlier with nothing anywhere reporting a change. The
silent branch is the one that covers a WHOLE-SERVICE misconfiguration, so it was silent in exactly the
case worth hearing about, while the per-table branches were loud about single datasets.

The estate's own rule for this shape is written one file over, about `docs_enabled`: "A security
default that every deployment must remember to turn off is one nobody turns off." A hardening that
every deployment must remember to turn ON is the same defect facing the other way.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from maintenance.services import credentials


if TYPE_CHECKING:
    from maintenance.core.config import MaintenanceSettings


class _Settings:
    """Only what `write_options_for` reads on this path — a real one needs a whole environment.

    CAST rather than constructed: the function's contract here is "an empty `catalog_url` short-circuits
    before anything else is touched", so a double that carries only that field is the honest subject. A
    real `MaintenanceSettings` would prove the same thing while also proving the environment loads.
    """

    catalog_url = ""
    catalog_service_identity = "service-maintenance"


def _settings() -> MaintenanceSettings:
    return cast("MaintenanceSettings", _Settings())


_FALLBACK = {"aws_access_key_id": "root", "aws_secret_access_key": "root"}


def test_vending_switched_OFF_is_announced_not_assumed(caplog) -> None:
    """The operator must be able to learn, from the log, that this rewrite was root-signed."""
    with caplog.at_level(logging.INFO, logger="maintenance.services.credentials"):
        options = credentials.write_options_for("s3://lance-catalog/medallion/bronze", _settings(), fallback=_FALLBACK, declared_table_id="bronze$events")

    assert options == _FALLBACK, "with no catalog URL the rewrite must still run, on the ambient credential"
    said = " ".join(record.getMessage() for record in caplog.records)
    assert said, "the root-signed rewrite was silent — an operator cannot tell a hardened estate from an unhardened one"
    assert "root key" in said, f"the message must name what signed the rewrite; got {said!r}"
    assert "bronze$events" in said, f"the message must name the table it applies to; got {said!r}"


def test_it_says_so_ONCE_per_process_not_once_per_dataset(caplog) -> None:
    """A per-dataset line here would be one per dataset per tick forever — the § Q17-26 failure again.

    The condition is a whole-service one and does not change between datasets, so it is reported at the
    volume of the CONFIGURATION rather than the volume of the sweep.
    """
    credentials._reset_vending_notice()
    with caplog.at_level(logging.INFO, logger="maintenance.services.credentials"):
        for table in ("a$one", "b$two", "c$three"):
            credentials.write_options_for(f"s3://b/{table}", _settings(), fallback=_FALLBACK, declared_table_id=table)

    notices = [r for r in caplog.records if "root key" in r.getMessage() and "NOT CONFIGURED" in r.getMessage()]
    assert len(notices) == 1, f"expected one configuration notice for three datasets, got {len(notices)}"
