"""The credential door is asked about the table the dataset IS, and a stamp only where nothing better answers.

[[LH-141]]. Measured on the live estate 2026-09-16 over a 12-hour window: datasets at composed ``medallion/<tier>``
paths, which no layout parser can name, were signed by the ambient credential with no vend decision at all, silent at
the deployed log level (1,310 SCOPED vend lines, 0 AMBIENT). So the producer's ``lineage.dataset_id`` stamp names the
table where nothing else can.

THE STAMP IS THE FALLBACK, NEVER THE PREFERENCE. The catalog's ``__manifest`` names the table a location IS; the
path's layout says where the bytes are; the stamp is what a producer claimed, and this row exists because stamps are
wrong. A stamp the vend does use is then checked against the catalog's own location for that id
(`test_a_vended_credential_must_cover_the_dataset_it_signs`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import lance
import pyarrow as pa
import pytest

from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials, sweep
from maintenance.services.due import TableIds
from maintenance.services.plan import DatasetPlan, PlanContext
from service_kit.lakehouse.base_refs import normalise
from service_kit.lakehouse.maintenance_due import CloneList


def _stamped(tmp: Path, name: str, *, stamp: str | None, fragments: int = 2) -> str:
    uri = str(tmp / name)
    for i in range(fragments):
        lance.write_dataset(pa.table({"id": pa.array([i], pa.int64())}), uri, mode="overwrite" if i == 0 else "append", enable_stable_row_ids=True)
    if stamp is not None:
        lance.dataset(uri).update_schema_metadata({"lineage.dataset_id": stamp}, replace=True)
    return uri


def _maintain(uri: str, tmp: Path, *, named: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """One dataset's pass in a tick whose ``__manifest`` names it ``named``, under a policy keeping one version, so its
    superseded version is reclaimable and the pass vends."""
    now = datetime.now(UTC)
    monkeypatch.setattr(sweep, "resolve_plan", lambda *_a, **_k: DatasetPlan(older_than=None, retain_versions=1))
    settings = MaintenanceSettings(
        MAINTENANCE_S3_BUCKET="lance-catalog",
        MAINTENANCE_CATALOG_URL="http://catalog:2333",
        MAINTENANCE_CONTROL_ROOT=str(tmp),
        MAINTENANCE_MIN_VERSIONS_TO_EXPIRE=1,
    )
    tick = sweep.SweepPlan(
        uris=[uri],
        clones=CloneList(taken_at=now),
        table_ids=TableIds(by_location={normalise(uri): named} if named else {}),
        context=PlanContext(policies=[], now=now, older_than=timedelta(days=7)),
    )
    sweep.maintain_dataset(uri, sweep=tick, settings=settings)


def _record(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    asked: list[str | None] = []

    def _write_options_for(uri: str, settings: MaintenanceSettings, *, fallback: dict[str, str], declared_table_id: str | None = None) -> dict[str, str]:
        asked.append(declared_table_id)
        return fallback

    monkeypatch.setattr(credentials, "write_options_for", _write_options_for)
    return asked


def test_a_dataset_nothing_else_names_vends_against_the_stamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured population: `s3://<bucket>/medallion/<tier>`, where the parser answers None."""
    asked = _record(monkeypatch)
    uri = _stamped(tmp_path, "medallion-silver", stamp="bronze$events")

    _maintain(uri, tmp_path, named=None, monkeypatch=monkeypatch)

    assert asked == ["bronze$events"], f"the vend was asked about {asked}"


@pytest.mark.parametrize(("name", "named", "expected"), [("flat", "real$table", "real$table"), ("4c49d010_acme-bronze$events", None, "acme-bronze$events")])
def test_the_manifest_and_the_layout_beat_the_stamp(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, named: str | None, expected: str) -> None:
    """The catalog names the table a location is, and the layout says where the bytes are; the stamp says what a
    producer claimed, so it never displaces either."""
    asked = _record(monkeypatch)
    uri = _stamped(tmp_path, name, stamp="a-wrong$stamp")

    _maintain(uri, tmp_path, named=named, monkeypatch=monkeypatch)

    assert asked == [expected]


def test_a_dataset_with_no_stamp_and_no_derivable_id_vends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged behaviour, asserted so the new read cannot quietly invent an id. A producer forced to
    supply something would vend a credential for the WRONG table rather than for none."""
    asked = _record(monkeypatch)
    uri = _stamped(tmp_path, "anonymous", stamp=None)

    _maintain(uri, tmp_path, named=None, monkeypatch=monkeypatch)

    assert asked == [None], "an unidentifiable dataset was given an identity"
