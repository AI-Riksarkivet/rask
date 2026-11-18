"""A reclaim whose dry run truncates its list marks a superset of what its cleanup removes (ADR 0243, ``reclaim_marks.victims``).

Past :data:`lakehouse.reclaim_marks._MOST_CANDIDATES` candidate files Lance's dry run stops naming them, and the mark is
taken as every version of the ref below its head that nothing pins. A tag pins a version of its OWN branch: one on
``(b, 3)`` keeps branch ``b``'s version 3, while main's untagged version 3 is one main's cleanup deletes, so it must be
marked. The bound is lowered here so a five-version table truncates.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import lance
import pyarrow as pa
import pytest

from lakehouse import reclaim_marks


def test_a_tag_on_another_branchs_version_of_the_same_number_leaves_mains_marked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uri = str(tmp_path / "t")
    for n in range(5):
        lance.write_dataset(pa.table({"id": [n]}), uri, mode="create" if n == 0 else "overwrite")
    main = lance.dataset(uri)
    main.create_branch("b", 2)
    for n in range(2):
        lance.write_dataset(pa.table({"id": [10 + n]}), lance.dataset(uri).checkout_version(("b", None)), mode="overwrite")
    lance.dataset(uri).tags.create("t", ("b", 3))
    monkeypatch.setattr(reclaim_marks, "_MOST_CANDIDATES", 1)

    marked = reclaim_marks.victims(lance.dataset(uri), older_than=timedelta(0), retain_versions=None, ref=None)

    assert 3 in marked and 5 not in marked, marked
