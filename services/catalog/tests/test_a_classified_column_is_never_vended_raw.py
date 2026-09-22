"""A column carrying a classification makes its table unvendable as raw object bytes.

[[LH-058]]. `credentials` is a DATA-READ action (`fga_deps.py:88`), so a reader who clears
`can_read_data` receives a 900 s object-store session over the table's whole prefix and reads every
byte directly — past any door that could project columns. Classifying a column changed nothing about
that.

THE ROW'S OPEN CHOICE WAS "REFUSE OR NARROW", AND MEASUREMENT SETTLES IT. A session policy's unit is an
OBJECT, and Lance's field-to-file mapping is write-order dependent: measured on pylance 2026-09-22, two
columns written together share ONE data file (`field ids: [0, 1]`) while a column added later gets its
own. So whether a classified column's bytes are separable is an accident of the table's write history,
and no policy can express "this prefix except that column". Narrowing is not expressible.

The answer is `server_mediated`, which is this door's EXISTING answer for "a direct credential would be
wrong here" (the unsanctioned-base fallback beside it) — and it is the one path where a column rule can
ever be applied, because the Arrow-IPC endpoints project.

WHAT THIS DOES NOT CLAIM: the server-mediated path does not mask either. This stops the raw bytes
leaving under a credential the caller holds, which is the precondition for masking, not masking.

THE STORE IS THE LANCE FORMAT'S OWN, which is the other half of the finding. `update_field_metadata`
round-trips the value, and BOTH the value and the field's id survive a rename (`secret` -> `ssn` kept
id 1 and kept the metadata). Keying column governance on a field id — the thing the reference model
builds deliberately, because a name is not stable — is here a property of the format.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from catalog.core.vending import CLASSIFICATION_KEY, classified_columns, dataset_facts


#: pylance's `alter_columns` is declared `*alterations: Iterable[AlterColumn]` and its implementation
#: does `self._ds.alter_columns(list(alterations))`, where the Rust side requires each element to be a
#: DICT. So the typed form raises `TypeError: 'list' object is not an instance of 'dict'` and the
#: untyped form is the one that works — measured on pylance 2026-09-22. `LanceSchema.field` is likewise
#: absent from the stubs and present at runtime. Both are reached through an explicitly `Any`-typed
#: handle rather than an ignore comment, so the narrowing is visible and carries its reason.
def _lance_dataset(location: str) -> Any:
    return lance.dataset(location)


def _dataset(tmp_path: Path, *, classify: str | None = None) -> str:
    location = str(tmp_path / "t.lance")
    dataset = lance.write_dataset(pa.table({"id": [1, 2], "ssn": ["a", "b"]}), location)
    if classify is not None:
        dataset.update_field_metadata({classify: {CLASSIFICATION_KEY: "restricted"}})
    return location


def test_an_unclassified_table_reports_no_classified_columns(tmp_path: Path) -> None:
    """The control. Without it every assertion below passes on a reader that finds nothing, ever."""
    _version, _bases, classified = dataset_facts(_dataset(tmp_path), {})
    assert classified == ()


def test_a_classified_column_is_reported_off_the_same_manifest_read(tmp_path: Path) -> None:
    """`dataset_facts` is the vend's ONE root-cred open; the classification rides it rather than a second."""
    _version, _bases, classified = dataset_facts(_dataset(tmp_path, classify="ssn"), {})
    assert classified == ("ssn",), "the vend door cannot refuse what the manifest read never reported"


def test_the_classification_survives_a_rename(tmp_path: Path) -> None:
    """The property that makes a NAME-keyed store unnecessary — it is the FIELD that carries it."""
    location = _dataset(tmp_path, classify="ssn")
    dataset = _lance_dataset(location)
    before = dataset.lance_schema.field("ssn").id()
    dataset.alter_columns({"path": "ssn", "name": "taxpayer_id"})

    reopened = _lance_dataset(location)
    field = reopened.lance_schema.field("taxpayer_id")
    assert field.id() == before, "the field id moved under a rename — a classification keyed on it would be lost"
    assert CLASSIFICATION_KEY in (field.metadata or {}), "the classification did not follow the rename"
    assert classified_columns(reopened) == ("taxpayer_id",)


@pytest.mark.parametrize("classify", [None, "ssn"])
def test_the_reader_never_raises_on_a_table_it_cannot_open(tmp_path: Path, classify: str | None) -> None:
    """A vend must not 500 because a schema read failed — an unreadable table reports nothing classified."""
    _dataset(tmp_path, classify=classify)
    assert dataset_facts(str(tmp_path / "absent.lance"), {}) == (0, (), ())
