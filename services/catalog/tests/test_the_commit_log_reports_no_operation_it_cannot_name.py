"""The commit log may report a version with no operation; it must not invent one.

`table_history`'s own header says the whole value of the endpoint is "reporting what Lance actually
recorded". It builds each row's operation with `type(op).__name__`, and that yields `BaseOperation` for
an operation pylance models no subclass for — `BaseOperation` being the ABC the twelve modelled
operations inherit (`lance/dataset.py:5905`), not an operation Lance has ever recorded. So the log
answered with a name no Lance release defines, on the one endpoint whose contract is fidelity.

IT IS NOT HYPOTHETICAL. Measured 2026-09-11, one `dataset.optimize.compact_files()` commits TWO
versions — an unmodelled one, then the `Rewrite` — so every compacted table in the estate carries a
history row claiming an operation called "BaseOperation".

THE MODULE ALREADY HAD THE HONEST ANSWER and did not reach for it here. A transaction it cannot read
appends the row with `operation` left null, and says why: "Reporting the version with a null operation
is honest; dropping it would make the history lie by omission." An operation it cannot NAME is the same
question with the same answer — the version stays in the log, and the field the reader cannot be told
is empty rather than filled with the name of an abstract base class.

WHY NOT A PLACEHOLDER STRING. Any spelling invented here ("Unknown", "Other") is a value a client must
now learn, and it collides with the day Lance adds a real operation of that name. Null already means
"this log cannot tell you", is already produced by the sibling branch, and needs no new vocabulary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pytest

from catalog.services import dataplane


@pytest.fixture
def compacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A real compaction, never a hand-written operation name.

    The defect is invisible to any test that supplies its own operation strings: it lives in what Lance
    commits, so the test has to let Lance commit it.
    """
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": [1]}), uri)
    for value in (2, 3, 4):
        lance.write_dataset(pa.table({"id": [value]}), uri, mode="append")
    lance.dataset(uri).optimize.compact_files()
    monkeypatch.setattr(dataplane, "open_dataset", lambda *_args, **_kwargs: lance.dataset(uri))
    return uri


def _history(table_id: list[str] | None = None) -> dict[int, Any]:
    rows = dataplane.table_history(cast(Any, None), cast(Any, {}), table_id or ["db", "t"])
    return {int(row["version"]): row["operation"] for row in rows}


def test_the_modelled_operations_are_still_named(compacted: str) -> None:
    """The half that must not regress: a name Lance did record is reported verbatim."""
    operations = _history()

    assert operations[1] == "Overwrite", "the create"
    assert operations[4] == "Append", "the last write before the compaction"
    assert operations[6] == "Rewrite", "the compaction itself is modelled and must be named"


def test_an_operation_the_binding_cannot_name_is_reported_null(compacted: str) -> None:
    """THE GATE. Before this, a compacted table's log claimed an operation called 'BaseOperation'."""
    operations = _history()

    assert operations[5] is None, (
        "the version a compaction commits before its Rewrite has no operation this binding can name, so "
        "the log must leave the field null — the same answer it already gives for a transaction it cannot "
        "read — rather than reporting the name of the abstract base class"
    )


def test_every_version_still_appears(compacted: str) -> None:
    """Null is not a licence to drop the row, which would be the log lying by omission instead."""
    assert sorted(_history()) == [1, 2, 3, 4, 5, 6], "a version the log cannot describe is still a version that happened"
