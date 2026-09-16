"""The credential door and the compaction-plan door must be asked about the SAME table.

[[LH-141]]. The sweep carries two table identities per dataset and uses a different one at each door:

* the CREDENTIAL door gets ``item.table_id``, which ``plan_sweep`` fills from
  ``table_id_from_uri(uri)`` — ``None`` for every composed ``medallion/<tier>`` path;
* the COMPACTION-PLAN door gets ``result.declared_table_id``, which ``compact_one`` reads off the
  dataset's own ``lineage.dataset_id`` stamp.

So a stamp the vend never sees reaches the catalog. Measured on the live estate 2026-09-16 over a
12-hour window: **30 calls to ``table/bronze$events/compaction_plan`` and ZERO to
``table/bronze$events/credentials``** — and in one tick, 3 of the 59 datasets that wrote were signed by
the ambient credential with no vend decision at all, all three composed paths.

IT IS SILENT AT THE DEPLOYED LOG LEVEL, which is why it survived. ``write_options_for`` returns the
ambient fallback at ``table_id is None`` on a ``logger.debug`` line and makes no HTTP call, so there is
no AMBIENT line either: measured in the same window, 1,310 SCOPED vend lines, **0 AMBIENT**, and 0
``maintenance_vend_skipped_unresolvable_location``. Every observable said the vending was total.

THE STAMP IS THE FALLBACK, NEVER THE PREFERENCE. The path is derived from where the bytes actually
are; the stamp is what a producer claimed, and this row exists because stamps are wrong. So a flat
layout still wins, and the stamp only fills the gap the parser cannot read — where it is then checked
against the catalog's own location for that id (`test_a_vended_credential_must_cover_the_dataset_it_signs`).
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest

from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials, sweep
from service_kit.lakehouse.work_items import DatasetPlan, DatasetWorkItem


def _stamped(tmp: Path, name: str, *, stamp: str | None, fragments: int = 2) -> str:
    uri = str(tmp / name)
    for i in range(fragments):
        lance.write_dataset(pa.table({"id": pa.array([i], pa.int64())}), uri, mode="overwrite" if i == 0 else "append", enable_stable_row_ids=True)
    if stamp is not None:
        lance.dataset(uri).update_schema_metadata({"lineage.dataset_id": stamp}, replace=True)
    return uri


def _item(uri: str, *, table_id: str | None) -> DatasetWorkItem:
    return DatasetWorkItem(uri=uri, plan=DatasetPlan(cleanup_enabled=False, optimize_indices_enabled=False), table_id=table_id)


def _settings() -> MaintenanceSettings:
    return MaintenanceSettings(MAINTENANCE_S3_BUCKET="lance-catalog", MAINTENANCE_CATALOG_URL="http://catalog:2333")


def _record(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    asked: list[str | None] = []

    def _write_options_for(uri: str, settings: MaintenanceSettings, *, fallback: dict[str, str], declared_table_id: str | None = None) -> dict[str, str]:
        asked.append(declared_table_id)
        return fallback

    monkeypatch.setattr(credentials, "write_options_for", _write_options_for)
    return asked


def test_a_unit_with_no_derivable_id_vends_against_the_stamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured population: `s3://<bucket>/medallion/<tier>`, where the parser answers None."""
    asked = _record(monkeypatch)
    uri = _stamped(tmp_path, "medallion-silver", stamp="bronze$events")

    sweep.maintain_one_item(_item(uri, table_id=None), settings=_settings(), options={})

    assert asked == ["bronze$events"], f"the vend was asked about {asked}, so the credential door and the plan door disagree about this dataset"


def test_a_derivable_id_still_beats_the_stamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The path says where the bytes are; the stamp says what a producer claimed. This row is a
    catalogue of wrong stamps, so the stamp must never displace an answer the path could give."""
    asked = _record(monkeypatch)
    uri = _stamped(tmp_path, "flat", stamp="a-wrong$stamp")

    sweep.maintain_one_item(_item(uri, table_id="real$table"), settings=_settings(), options={})

    assert asked == ["real$table"]


def test_a_unit_that_will_not_write_still_vends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading the stamp must not cost the estate the saving that probe exists for — measured at 280
    STS records a minute, against a store that became unable to restart at 107,485 of them."""
    asked = _record(monkeypatch)
    uri = _stamped(tmp_path, "quiet", stamp="bronze$events", fragments=1)

    sweep.maintain_one_item(_item(uri, table_id=None), settings=_settings(), options={})

    assert asked == [], "a dataset with nothing to rewrite minted a credential anyway"


def test_a_dataset_with_no_stamp_and_no_derivable_id_vends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged behaviour, asserted so the new read cannot quietly invent an id. A producer forced to
    supply something would vend a credential for the WRONG table rather than for none."""
    asked = _record(monkeypatch)
    uri = _stamped(tmp_path, "anonymous", stamp=None)

    sweep.maintain_one_item(_item(uri, table_id=None), settings=_settings(), options={})

    assert asked == [None], "an unidentifiable dataset was given an identity"
