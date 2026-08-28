"""A transient read failure must NOT be laundered into 'does not exist' (SK-05).

`discover_tables` walks the `*.lance` stems the store listed and calls `table_info` on each.
It caught EVERY exception and dropped the table from the returned dict — so a flaky S3 read
(connection reset, timeout) made a table that genuinely exists vanish. Downstream that absence is
indistinguishable from a missing table: `validate_descriptor` reports `row_table ... does not
exist`, `load_dataset_descriptor` raises `ValueError`, and `registry.get()` fails the WHOLE dataset
permanently — a transient blip turned into a hard, non-retriable descriptor failure.

A genuine not-a-dataset stem (a `.lance` dir with no manifest) is real absence and is still dropped,
so the descriptor cross-check names the missing table. But an I/O error must surface, loud and
retriable, rather than mutate into a permanent 'does not exist'.
"""

from __future__ import annotations

import pytest

from service_kit.lancekit import introspect


def test_transient_read_error_propagates_not_swallowed(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    (tmp_path / "row_table.lance").mkdir()

    def _boom(_uri: object, _storage: object = None) -> introspect.TableInfo:
        raise OSError("connection reset by peer")

    monkeypatch.setattr(introspect, "table_info", _boom)

    with pytest.raises(OSError, match="connection reset"):
        introspect.discover_tables(tmp_path)


def test_a_genuinely_absent_table_is_still_dropped(tmp_path) -> None:
    """A `.lance` stem the listing saw but Lance cannot open as a dataset (no manifest) is real
    absence: drop it so the descriptor cross-check reports it by name, don't fail the walk."""
    (tmp_path / "half_written.lance").mkdir()

    tables = introspect.discover_tables(tmp_path)

    assert tables == {}
