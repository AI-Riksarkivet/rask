"""#60 — report what `optimize_indices()` CANNOT fix, instead of reporting that it ran.

The sweep counts `indices_optimized` and moves on. That number says the call was made; it says
nothing about whether the indices are actually serving queries afterwards, and four states survive
the call untouched:

  * **rows left unindexed.** `optimize_indices` folds new fragments into existing indices, but a
    dataset can come out the other side still carrying unindexed rows — those queries fall back to a
    flat scan. Nothing reported it, so a tier silently degrades to full scans while the sweep logs
    success every tick.
  * **delta proliferation.** Each optimize can append a DELTA index rather than merging; `num_indices`
    climbs and every query fans out across all of them. This is the FTS partition-count case, and it
    is measurable directly.
  * **params drift.** An index built with one tokenizer/params set is not rebuilt by `optimize_indices`
    — it only extends what is there. A dataset whose index was created under different params keeps
    serving under the OLD ones, invisibly.
  * **a dropped index.** A cast (or any rebuild that changes a column's type) can leave the column
    with no index at all, and an optimize over a dataset with no index is a successful no-op.

MEASURED, not assumed. pylance 9.0.0's `stats.index_stats(name)` exposes `num_indexed_rows`,
`num_unindexed_rows`, `num_indexed_fragments`, `num_unindexed_fragments`, `num_indices` and per-delta
`indices[].params`; `describe_indices()` gives `index_type`, `field_names` and `details`. Everything
asserted here was read off a real dataset first.

This module REPORTS. It does not rebuild an index — that is a data-rewriting decision with a cost
profile (a full FTS rebuild over a large tier) that belongs to a policy and an operator, not to a
cron tick that was asked to compact.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest

from maintenance.services import index_health
from maintenance.services.index_health import inspect_indices


def _indexed(tmp_path: Path, *, rows: int = 200) -> str:
    """A dataset with one index of each shape the estate actually uses."""
    uri = str(tmp_path / "t.lance")
    table = pa.table(
        {
            "id": pa.array(range(rows), pa.int64()),
            "cat": pa.array(["a", "b"] * (rows // 2)),
            "txt": pa.array([f"hello world {i}" for i in range(rows)]),
        }
    )
    lance.write_dataset(table, uri)
    ds = lance.dataset(uri)
    ds.create_scalar_index("cat", index_type="BITMAP", name="cat_bitmap")
    ds.create_scalar_index("id", index_type="BTREE", name="id_btree")
    ds.create_scalar_index("txt", index_type="INVERTED", name="txt_fts")
    return uri


def _append(uri: str, *, start: int, rows: int) -> None:
    lance.write_dataset(
        pa.table(
            {
                "id": pa.array(range(start, start + rows), pa.int64()),
                "cat": pa.array(["a", "b"] * (rows // 2)),
                "txt": pa.array([f"more {i}" for i in range(rows)]),
            }
        ),
        uri,
        mode="append",
    )


def test_a_FULLY_INDEXED_dataset_reports_no_findings(tmp_path: Path) -> None:
    """The control, and it has to hold or every other assertion is noise.

    A report that fires on a healthy dataset trains operators to ignore it, which is worse than not
    reporting at all.
    """
    findings = inspect_indices(lance.dataset(_indexed(tmp_path)))

    assert findings == [], f"a healthy dataset reported findings: {findings}"


def test_UNINDEXED_ROWS_are_reported_with_the_column_that_has_them(tmp_path: Path) -> None:
    """The state `indices_optimized` cannot distinguish from health.

    Queries against these rows fall back to a flat scan. The sweep's counter says the optimize ran;
    only this says it did not finish the job.
    """
    uri = _indexed(tmp_path)
    _append(uri, start=200, rows=60)

    findings = inspect_indices(lance.dataset(uri))

    assert findings, "60 unindexed rows were reported as healthy"
    by_name = {f.name: f for f in findings}
    assert by_name["id_btree"].unindexed_rows == 60
    assert by_name["id_btree"].column == "id"
    assert "unindexed" in by_name["id_btree"].note


def test_a_column_with_NO_INDEX_AT_ALL_is_reported(tmp_path: Path) -> None:
    """The dropped-index case. `optimize_indices` over a dataset with no index is a successful no-op,
    so the pass is green and the column is served by a full scan forever."""
    uri = str(tmp_path / "bare.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(10), pa.int64())}), uri)

    findings = inspect_indices(lance.dataset(uri), expected_columns=["id"])

    assert any(f.column == "id" and "no index" in f.note for f in findings), findings


def test_an_EXPECTED_column_that_IS_indexed_produces_nothing(tmp_path: Path) -> None:
    """The other half of the dropped-index check — it must not fire when the index is present."""
    findings = inspect_indices(lance.dataset(_indexed(tmp_path)), expected_columns=["id", "cat", "txt"])

    assert findings == [], f"an indexed column was reported as missing: {findings}"


def test_the_finding_carries_the_INDEX_TYPE_so_a_change_is_visible(tmp_path: Path) -> None:
    """Params/type drift is not something `optimize_indices` repairs — it extends what exists.

    Reporting the type on every finding is what makes a silent change from BITMAP to BTREE (or an FTS
    rebuilt under different tokenizer params) something an operator can SEE, which is the most this
    can honestly do without rebuilding.
    """
    uri = _indexed(tmp_path)
    _append(uri, start=200, rows=60)

    findings = {f.name: f.index_type for f in inspect_indices(lance.dataset(uri))}

    assert findings["cat_bitmap"] == "Bitmap"
    assert findings["id_btree"] == "BTree"
    assert findings["txt_fts"] == "Inverted"


def test_this_pylance_MERGES_deltas_rather_than_appending_them(tmp_path: Path) -> None:
    """The FTS partition-count case, MEASURED — and on pylance 9.0.0 it does not happen.

    The concern is real in principle: an optimize that APPENDS a delta index leaves every query
    fanning out across all of them, so a dataset gets slower with each maintenance tick while
    `indices_optimized` counts up. Measured over four append+optimize cycles on an inverted index:

        append 0: num_indices=1 unindexed=40 -> optimize -> num_indices=1 unindexed=0 per_delta=[240]
        append 1: num_indices=1 unindexed=40 -> optimize -> num_indices=1 unindexed=0 per_delta=[280]
        append 2: ...                                                              per_delta=[320]
        append 3: ...                                                              per_delta=[360]

    `num_indices` never leaves 1 and the single delta absorbs the rows — this pylance MERGES.

    So the check in `inspect_indices` is a guard against a behaviour change, not a live finding, and
    this test says so out loud rather than pretending to reproduce something it cannot. It is
    genuinely load-bearing in that direction: if a pylance upgrade starts appending deltas, this fails
    and the guard's threshold becomes a real number somebody has to choose.
    """
    uri = _indexed(tmp_path)
    for chunk in range(4):
        _append(uri, start=200 + chunk * 40, rows=40)
        lance.dataset(uri).optimize.optimize_indices()

    stats = lance.dataset(uri).stats.index_stats("txt_fts")

    assert stats["num_indices"] == 1, (
        f"this pylance now keeps {stats['num_indices']} delta indices after repeated optimizes. Delta "
        f"proliferation is live — pick a real DEFAULT_MAX_DELTAS and make the check assert findings."
    )
    assert inspect_indices(lance.dataset(uri), max_deltas=1) == [], "a merged index reported a finding"


def test_a_dataset_whose_stats_cannot_be_READ_is_reported_not_skipped(tmp_path: Path) -> None:
    """ "We could not check" must never render as "nothing wrong" — the rule the orphan scan follows.

    Driven by asking about an index name the dataset does not have, which is the shape a renamed or
    concurrently-dropped index produces.
    """
    uri = _indexed(tmp_path)

    findings = inspect_indices(lance.dataset(uri), expected_columns=["does_not_exist"])

    assert any(f.column == "does_not_exist" for f in findings)


def test_a_JSON_index_is_reported_UNKNOWN_without_ever_calling_stats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`index_stats` on a JSON index panics in Lance (`scalar/json.rs:95`, a `todo!()`), and a pyo3
    panic writes a Rust backtrace to stderr every single sweep. `describe_indices` already reports the
    type, so the call is skipped — but the index must still read UNKNOWN, never healthy, because it is
    genuinely unmonitored.

    The NOT-CALLED half is the whole point: without it the panic returns and this test still passes.
    """
    uri = _indexed(tmp_path)

    class _JsonIndex:
        name = "lineage_run_id_idx"
        field_names = ("lineage",)
        index_type = "Json"

    monkeypatch.setattr(lance.LanceDataset, "describe_indices", lambda self: [_JsonIndex()], raising=False)

    called: list[str] = []
    monkeypatch.setattr(index_health, "_stats", lambda ds, name: called.append(name))

    findings = inspect_indices(lance.dataset(uri))

    assert called == [], f"stats must not be asked of a JSON index — that is the panic: {called}"
    json_findings = [f for f in findings if f.name == "lineage_run_id_idx"]
    assert len(json_findings) == 1, findings
    assert json_findings[0].index_type == "Json"
    assert "UNKNOWN" in json_findings[0].note
    assert "JSON index" in json_findings[0].note
