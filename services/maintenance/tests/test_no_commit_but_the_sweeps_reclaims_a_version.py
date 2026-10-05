"""Version reclamation has one owner, the sweep, and no writer's commit deletes a version ([[LH-245]]).

Lance's automatic cleanup runs INSIDE the commit of whoever writes, every N commits, from the
`lance.auto_cleanup.*` manifest keys (`lance_docs/guide.md:3857-3923`). Measured on pylance 12.0.0: with
`interval=1, older_than=0s`, one ordinary append took a table from versions 1..7 to 7..8. That deletion
passes none of the sweep's gates (a legal hold, a protected base), runs under the writer's identity,
and is recorded nowhere, so a tick removes every key before any gate can return.

Driven on real pylance through the worker's unit, `maintain_one_item`, on the two refusal paths. The sweep
is the backstop for a table nobody commits on; a table that is committed on is disarmed by the committing
door first (`tests/integration/test_an_ordinary_append_never_deletes_a_version.py`).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Literal

import lance
import pyarrow as pa
import pytest

from maintenance.core.config import MaintenanceSettings
from maintenance.services.sweep import DatasetPlan, DatasetWorkItem, maintain_one_item


#: The keys an armed table carries. `retain_versions` included, because pylance's own
#: `disable_auto_cleanup` deletes only the other two and leaves it standing (measured on 12.0.0).
_ARMED: dict[str, str | None] = {"lance.auto_cleanup.interval": "1", "lance.auto_cleanup.older_than": "0s", "lance.auto_cleanup.retain_versions": "1"}


def _armed_table(tmp_path: Path) -> str:
    uri = str(tmp_path / "t.lance")
    table = pa.table({"id": pa.array([1, 2], pa.int64())})
    lance.write_dataset(table, uri, data_storage_version="2.2", enable_stable_row_ids=True)
    lance.write_dataset(table, uri, mode="append")
    lance.dataset(uri).update_config(_ARMED)
    return uri


def _auto_cleanup_keys(uri: str) -> dict[str, str]:
    return {k: v for k, v in lance.dataset(uri).config().items() if k.startswith("lance.auto_cleanup.")}


@pytest.mark.parametrize("governed_by", ["legal_hold", "protected_base"])
def test_a_held_or_protected_table_carries_no_auto_cleanup_keys_after_a_tick(tmp_path: Path, governed_by: Literal["legal_hold", "protected_base"]) -> None:
    """Both gates return before reclamation, which is exactly why the keys must go before either does."""
    uri = _armed_table(tmp_path)
    if governed_by == "legal_hold":
        item = DatasetWorkItem(uri=uri, plan=DatasetPlan(older_than=timedelta(0), cleanup_enabled=False))
    else:
        item = DatasetWorkItem(uri=uri, plan=DatasetPlan(older_than=timedelta(0)), protected_by=uri)

    result = maintain_one_item(item, settings=MaintenanceSettings.model_validate({"s3_bucket": "unused"}), options={})

    if governed_by == "protected_base":
        assert result.refused_by == "protected_base", "the fixture stopped being the protected case"
    else:
        assert result.old_versions_removed == 0, "the hold was not honoured"
    assert _auto_cleanup_keys(uri) == {}, f"the commit path is still armed on a {governed_by} table"
    assert result.auto_cleanup_disarmed is True
