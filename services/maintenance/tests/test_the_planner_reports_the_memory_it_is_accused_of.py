"""Each table pass's line carries the memory readings [[LH-183]] turns on.

[[LH-183]] is a row about the maintenance pod's RSS climbing until it is OOMKilled, and its method is one comparison:
``python_blocks`` against RSS. Flat blocks with climbing RSS means native allocation and no heap fix will touch it;
blocks tracking RSS means Python retention. A pass opens its table's datasets, the opens LH-183 narrowed the source to,
so the readings ride the pass's own line (``maintenance_table_pass``): the comparison needs both series at the SAME
instant, and the row records ~10Mi of spread between two samplers reading seconds apart.
"""

import gc
import logging
from pathlib import Path
from uuid import uuid4

import lance
import pyarrow as pa
import pytest

from lakehouse.lancekit.base_refs import normalise
from lakehouse.records import location_claims
from maintenance.core.config import MaintenanceSettings
from maintenance.services import table_pass
from maintenance.services.naming import TableIds
from maintenance.services.table_pass import TableTask, memory_readings, run_table_pass


#: The readings whose whole purpose is to be compared with each other. Named once: a lane that reports
#: a subset reports nothing, because one series alone cannot tell native from Python.
_MEMORY_KEYS = frozenset({"lance_session_bytes", "lance_session_cap_bytes", "python_blocks", "rss_bytes"})


def test_the_pass_line_reports_every_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A pass whose line carries a subset of the readings cannot answer the row's comparison."""
    control = tmp_path / "control"
    control.mkdir()
    settings = MaintenanceSettings(MAINTENANCE_CONTROL_ROOT=str(control), MAINTENANCE_POLICY_ROOT=str(control))
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"n": pa.array([1, 2], pa.int64())}), uri)
    uuid = str(uuid4())
    claims = location_claims.ClaimStore(control_root=settings.resolved_control_root, storage_options={})
    location_claims.take(claims, normalise(uri), "db$t", segments=["db", "t"], is_gone=lambda _claim: False, uuid=uuid)
    named = TableIds(by_location={normalise(uri): "db$t"}, namespace_uuids={"db": str(uuid4())})
    monkeypatch.setattr(table_pass, "table_ids_of", lambda *_a, **_k: named)

    with caplog.at_level(logging.INFO, logger=table_pass.__name__):
        run_table_pass(TableTask(uuid=uuid, location=uri), settings)

    (line,) = [record for record in caplog.records if record.getMessage() == "maintenance_table_pass"]
    missing = sorted(key for key in _MEMORY_KEYS if not isinstance(getattr(line, key, None), int))
    assert missing == [], f"the pass's line lost a reading: {missing}"


def test_two_readings_move_with_the_heap() -> None:
    """The reading must RESPOND, or it cannot separate retention from native allocation.

    `python_blocks` is `sys.getallocatedblocks()`, a count of blocks rather than bytes: tracking RSS
    means Python retention, flat while RSS climbs means native allocation. `len(gc.get_objects())` answers
    only for what the collector tracks, which changes between versions: on CPython 3.13.12 `gc.is_tracked({"n": 1})`
    is False, so holding 50,000 such dicts moved `gc.get_objects()` by -188 while `getallocatedblocks()` moved by
    +149,728 and returned to baseline on release; on 3.15.2 it is True. A reading that stays
    flat reads as "native", a confident wrong answer to the only question this instrument settles.
    """
    # The allocation below triggers collections, which free cyclic garbage earlier tests left and can outweigh the 50,000
    # dicts between the two readings. Collected first and paused across them, the readings see only what this test holds.
    gc.collect()
    gc.disable()
    try:
        before = memory_readings()["python_blocks"]
        retained = [{"n": i} for i in range(50_000)]
        after = memory_readings()["python_blocks"]
    finally:
        gc.enable()

    assert after > before, f"the reading did not move ({before} -> {after}) while 50,000 dicts were held"
    assert len(retained) == 50_000, "keep the reference alive until after the second reading"
