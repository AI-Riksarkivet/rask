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
from collections.abc import Iterator
from typing import TYPE_CHECKING, cast

import pytest

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
    #: The identity this deployment actually runs as. `rask-maintenance` on the live estate — a SCOPED
    #: user, not the tenant root — which is the whole reason the message must read it rather than
    #: assert a rank.
    s3_access_key_id = "rask-maintenance"


def _settings() -> MaintenanceSettings:
    return cast("MaintenanceSettings", _Settings())


_FALLBACK = {"aws_access_key_id": "root", "aws_secret_access_key": "root"}


@pytest.fixture(autouse=True)
def _fresh_notice() -> Iterator[None]:
    """The notice is once per PROCESS, so every test here needs it un-fired to observe it.

    Without this the module only passes when nothing earlier in the process fired the notice, and
    something does: `tests/unit/test_base_refs_guard.py` drives a REAL SWEEP TICK, which goes through
    `write_options_for` against the same unconfigured vending. Bisected 2026-09-09 — running that one
    file ahead of this one is enough to red it. The declared `testpaths` order puts this service before
    `tests/unit`, which is the only reason it held; a suite that passes on the order it happens to be
    invoked in is not pinned. Autouse rather than a call in each test, so a test added later cannot
    forget it.
    """
    credentials._reset_vending_notice()
    yield
    credentials._reset_vending_notice()


def test_vending_switched_OFF_is_announced_not_assumed(caplog) -> None:
    """The operator must be able to learn, from the log, that this rewrite was root-signed."""
    with caplog.at_level(logging.INFO, logger="maintenance.services.credentials"):
        options = credentials.write_options_for("s3://lance-catalog/medallion/bronze", _settings(), fallback=_FALLBACK, declared_table_id="bronze$events")

    assert options == _FALLBACK, "with no catalog URL the rewrite must still run, on the ambient credential"
    said = " ".join(record.getMessage() for record in caplog.records)
    assert said, "the root-signed rewrite was silent — an operator cannot tell a hardened estate from an unhardened one"
    assert "rask-maintenance" in said, f"the message must NAME the credential that signed it, not rank it; got {said!r}"
    assert "root key" not in said, f"the rank was asserted instead of read — the estate left that posture; got {said!r}"
    assert "bronze$events" in said, f"the message must name the table it applies to; got {said!r}"


def test_it_says_so_ONCE_per_process_not_once_per_dataset(caplog) -> None:
    """A per-dataset line here would be one per dataset per tick forever — the § Q17-26 failure again.

    The condition is a whole-service one and does not change between datasets, so it is reported at the
    volume of the CONFIGURATION rather than the volume of the sweep.
    """
    with caplog.at_level(logging.INFO, logger="maintenance.services.credentials"):
        for table in ("a$one", "b$two", "c$three"):
            credentials.write_options_for(f"s3://b/{table}", _settings(), fallback=_FALLBACK, declared_table_id=table)

    notices = [r for r in caplog.records if "process credential" in r.getMessage() and "NOT CONFIGURED" in r.getMessage()]
    assert len(notices) == 1, f"expected one configuration notice for three datasets, got {len(notices)}"
