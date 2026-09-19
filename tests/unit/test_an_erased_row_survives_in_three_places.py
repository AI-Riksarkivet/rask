"""An erasure request removes a row from MAIN and from nowhere else ([[LH-073]]).

This is a CHARACTERISATION test: it asserts the defect, not the fix. The estate's own
`test_a_rotated_secret_reaches_the_pods_that_hold_it` does the same thing for the same reason — a
premise a fix depends on is worth holding still, because the fix is judged against it and because a
premise nobody re-measures is one that quietly stops being true.

WHAT IT DEMONSTRATES, measured against pylance 11.0.0. `delete_from_table` (`data.py:451`) is a
predicate delete plus a DELETE lineage event and propagates nowhere. So after "erase this subject":

    main after delete   ['bob', 'carol', 'dan']
    branch 'work'       ['alice', 'bob', 'carol', 'dan']
    tag 'pinned'        ['alice', 'bob', 'carol', 'dan']
    version 1           ['alice', 'bob', 'carol', 'dan']

All three survivors are LIVE, QUERYABLE SURFACES on the deployed estate, not archival residue: a
branch is reachable through `?branch=`, a tag and an old version through `checkout_version`, and the
catalog serves all of them. So the subject's data is one request away from anyone who can read the
table, after the estate has reported the erasure done.

WHY THIS IS THE WORSE HALF OF A PAIR. rask HAS the column-level lineage that answers "which systems
touched this record" — `ColumnLineageDatasetFacet`, emitted by the catalog and the medallion. It can
therefore produce the list an auditor asks for and then act on one quarter of it. Being able to prove
what you would have to erase, and not erasing it, is a worse position than having neither.

WHEN ERASURE PROPAGATES, THIS TEST INVERTS. That is intended: each assertion below names the surface
it is about, so the fix flips them one at a time and the remaining reds are the remaining work.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest


_SUBJECT = "alice"


@pytest.fixture
def erased(tmp_path: Path) -> lance.LanceDataset:
    """A table with a branch, a tag and history, after the subject is deleted from main."""
    uri = str(tmp_path / "subjects")
    dataset = lance.write_dataset(pa.table({"id": pa.array([1, 2, 3, 4]), "pii": pa.array([_SUBJECT, "bob", "carol", "dan"])}), uri)
    dataset.create_branch("work")
    dataset.tags.create("pinned", dataset.version)
    dataset.delete("id = 1")  # the erasure request, as the catalog's door performs it
    return lance.dataset(uri)


def _pii(handle: lance.LanceDataset) -> list[str]:
    return list(handle.to_table().to_pydict()["pii"])


def test_the_subject_is_gone_from_main(erased: lance.LanceDataset) -> None:
    """The one surface the door does reach — and the reason the operation reports success."""
    assert _SUBJECT not in _pii(erased)


def test_the_subject_SURVIVES_on_a_branch(erased: lance.LanceDataset) -> None:
    """A branch is a shallow clone that resolves through the parent's fragments, and a predicate delete
    on main writes a deletion file the branch's own manifest does not reference. Reachable through
    `?branch=` on every read door the catalog serves."""
    assert _SUBJECT in _pii(erased.checkout_version(("work", None)))


def test_the_subject_SURVIVES_under_a_tag(erased: lance.LanceDataset) -> None:
    """A tag PINS a version against reclamation, so the one mechanism that would eventually remove the
    bytes is the one a tag exists to prevent."""
    assert _SUBJECT in _pii(erased.checkout_version("pinned"))


def test_the_subject_SURVIVES_in_history(erased: lance.LanceDataset) -> None:
    """Time travel is a feature of the format, so every version before the delete still answers with
    the row. Retention eventually reclaims these — a tag or a branch stops it."""
    assert _SUBJECT in _pii(lance.dataset(erased.uri, version=1))


def test_three_of_four_surfaces_retain_the_subject(erased: lance.LanceDataset) -> None:
    """The count, stated once, because it is the sentence the row is about: an erasure request reaches
    one of the four places the estate serves this table from."""
    surfaces = {
        "main": _pii(erased),
        "branch:work": _pii(erased.checkout_version(("work", None))),
        "tag:pinned": _pii(erased.checkout_version("pinned")),
        "version:1": _pii(lance.dataset(erased.uri, version=1)),
    }
    retaining = sorted(name for name, rows in surfaces.items() if _SUBJECT in rows)

    assert retaining == ["branch:work", "tag:pinned", "version:1"], retaining
