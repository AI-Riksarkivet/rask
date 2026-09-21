"""A dataset the catalog CAN name must be planned off-pod, even when no producer stamped it.

THE DEFECT, measured on the live estate 2026-09-21. `rask-maintenance` is OOMKilled — 6 restarts,
exit 137, against a 512Mi limit — and the sweep's own per-dataset line says why: of 4,276 compaction
outcomes in the killed container, **2,207 ran `mode='in_pod'`**, which opens the whole dataset inside
the pod. The chart states the consequence at `maintenance-worker.yaml:104`: "this pod's memory ceiling
is a function of the largest table anyone owns rather than of its request rate."

WHY THEY LANDED THERE IS AN IDENTITY DISAGREEMENT, not a governance one. Two doors resolve the same
dataset's id and they do not resolve it the same way:

* `sweep.maintain_one_item` takes the PATH first and the stamp only where the path is silent, and
  vends the write credential under that answer;
* `optimize.compact_one` passed `result.declared_table_id` — the in-schema stamp — and nothing else,
  so a dataset with no stamp was treated as unplannable and fell to the in-pod rewrite.

The second premise is false for most of the population. Of the 151 distinct unstamped datasets the
killed container rewrote in-pod, `table_id_from_location` answers for **123 (81%)** — they sit in the
catalog's own `<uuid8>_<namespace>$<table>` layout. They are not anonymous: the same tick vended them
table-scoped credentials by that id ("write credential SCOPED for advstats7ns$tA", a catalog 200), and
then rewrote their bytes in-process anyway.

WHAT THIS DOES NOT CHANGE, and the distinction is the whole fix: a dataset the catalog genuinely
cannot name still belongs on the in-pod path.
`test_compaction_runs_off_the_pod.py::test_a_dataset_the_catalog_cannot_NAME_stays_on_the_in_pod_rewrite`
pins that and keeps passing — its fixture writes to a bare `t.lance`, which carries no identifier in
either the stamp or the layout. That test's reasoning is sound; only its premise that "no stamp" means
"no identifier" was too wide.

The id is RESOLVED ONCE by the caller and threaded, rather than re-derived here, so the credential the
bytes are signed with and the identifier the plan is addressed to cannot disagree — which is the
objection the code this replaces raised against deriving a second answer, honoured by not deriving one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa

from maintenance.services.optimize import compact_one
from service_kit.lakehouse.table_locations import table_id_from_location


LAID_OUT_BY_THE_CATALOG = "9c5020a1_advstats7ns$tA"
TABLE_ID = "advstats7ns$tA"


def _unstamped_but_laid_out(tmp_path: Path, *, writes: int = 6, rows: int = 10) -> str:
    """A dataset in the catalog's layout carrying NO `lineage.dataset_id` — the live shape.

    Separate writes because one write is one fragment; the schema metadata is left off deliberately,
    which is what makes this the 81% case rather than the already-covered one.
    """
    uri = str(tmp_path / LAID_OUT_BY_THE_CATALOG)
    for i in range(writes):
        table = pa.table({"id": pa.array([i * rows + j for j in range(rows)], pa.int64())})
        lance.write_dataset(table, uri, mode="create" if i == 0 else "append")
    return uri


def test_the_fixture_is_the_case_this_file_claims(tmp_path: Path) -> None:
    """The premise, asserted rather than assumed: no stamp, and yet an id the layout answers.

    Without this the test below could pass for the wrong reason — a fixture that quietly acquired a
    stamp would exercise the path that already worked.
    """
    from maintenance.core.lineage_emit import declared_table_id

    uri = _unstamped_but_laid_out(tmp_path)
    assert declared_table_id(lance.dataset(uri)) is None, "the fixture must carry no producer stamp"
    assert table_id_from_location(uri) == TABLE_ID, "the layout must still name the table"


def test_a_dataset_the_layout_names_is_planned_off_the_pod(tmp_path: Path) -> None:
    """The carried id reaches the rewrite gate, so the bytes leave the pod.

    RED before the fix: `compact_one` consulted the stamp alone, found `None`, skipped the gate
    entirely and compacted in-process — 123 of 151 live datasets took that path every tick.
    """
    uri = _unstamped_but_laid_out(tmp_path)
    asked: list[str] = []

    def _rewrite(uri: str, *, table_id: str, options: Any) -> Any:
        asked.append(table_id)
        raise AssertionError("stop here — proving the gate was ENTERED is the assertion")

    compact_one(
        uri,
        {},
        None,
        target_rows_per_fragment=1024,
        cleanup_enabled=False,
        optimize_indices_enabled=False,
        rewrite=_rewrite,
        table_id=table_id_from_location(uri),
    )

    assert asked == [TABLE_ID], "the rewrite gate must be addressed by the id the layout carries"


def test_the_caller_s_id_is_preferred_over_the_stamp(tmp_path: Path) -> None:
    """A resolved id displaces the stamp, because the credential was vended under the resolved one.

    [[LH-141]] is a catalogue of wrong stamps, and `sweep.maintain_one_item` states the precedence in
    as many words: the stamp must never displace an answer the layout could give. If the gate
    disagreed with the vend, the bytes would be signed for one table and planned against another.
    """
    uri = str(tmp_path / LAID_OUT_BY_THE_CATALOG)
    stamped = {b"lineage.dataset_id": b"someone-elses$table"}
    for i in range(6):
        table = pa.table({"id": pa.array([i * 10 + j for j in range(10)], pa.int64())}).replace_schema_metadata(stamped)
        lance.write_dataset(table, uri, mode="create" if i == 0 else "append")

    asked: list[str] = []

    def _rewrite(uri: str, *, table_id: str, options: Any) -> Any:
        asked.append(table_id)
        raise AssertionError("stop here — the identifier is the assertion")

    compact_one(
        uri,
        {},
        None,
        target_rows_per_fragment=1024,
        cleanup_enabled=False,
        optimize_indices_enabled=False,
        rewrite=_rewrite,
        table_id=TABLE_ID,
    )

    assert asked == [TABLE_ID], "the caller's resolved id wins; a wrong stamp must not redirect the plan"
