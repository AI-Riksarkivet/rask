"""A registry field that is present and EMPTY is not a missing field.

Measured on the live estate 2026-09-20: the drift report carries ten `IncompleteScan` entries reading
`lance-catalog/_trash/namespace-<id>.json is missing one of ['id', 'location']`, on every tick,
permanently.

NONE OF THOSE RECORDS IS MALFORMED. `namespaces.py` writes a namespace trash record with
`location=""` on purpose — a namespace is not a directory and owns no bytes, so there is no location
to record. The record is exactly what the catalog meant to write.

THE READER CONFLATES ABSENT WITH EMPTY. `_list_json_records` accepts a record when
``all(record.get(key) for key in required)`` — a TRUTHINESS test — so `""` fails it the same way a
missing key does. Two different facts, one answer.

AND THE CONSEQUENCE IS A WHOLE PASS, not a cosmetic note. `purge.py::report_is_clean` returns
"the drift report is INCOMPLETE … a partial scan cannot certify the estate" for ANY incomplete entry,
and the trash purge is gated on that verdict. So ten correctly-written records disable the reclaimer
permanently — and they do it while every drift category reads 0, which is precisely when an operator
would believe the estate is clean.

PRESENCE IS THE TEST, and it is the narrower one rather than the looser: a record genuinely missing
`location` is still skipped, because the key is absent rather than empty. What changes is only that a
field the writer deliberately left blank stops being reported as absent. `_orphaned_trash` already
handles an empty location correctly one layer down — "A location that names no bucket at all is NOT
reported. It is malformed rather than orphaned" — so nothing downstream needs to learn anything new.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from maintenance.services.reconcile import _list_json_records


def _write(tmp_path: Path, name: str, payload: dict[str, object]) -> None:
    (tmp_path / "_trash").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_trash" / name).write_text(json.dumps(payload), encoding="utf-8")


def _read(tmp_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """Exactly the arguments `_registry_source` passes for the trash registry.

    Mirroring the real call site is the point: a helper that passed only `required` would exercise a
    configuration the estate never uses, and would have reported an identity-less record as accepted
    while production rejected it.
    """
    return _list_json_records(f"file://{tmp_path}", {}, prefix="_trash", required=("id", "location"), non_empty=("id",))


@pytest.mark.parametrize(
    ("payload", "ids", "skip_words"),
    [
        # THE DEFECT. `namespaces.py` writes `location=""` because a namespace owns no bytes; the reader
        # called that missing, and ten of them blocked the trash purge forever.
        pytest.param({"id": "acme$silver", "location": "", "kind": "namespace"}, ["acme$silver"], (), id="empty-location-is-read"),
        # The line this must not cross. Presence is a narrower test than truthiness, not a looser one: a
        # record with no `location` key at all is still incomplete and still reported.
        pytest.param({"id": "ns$t", "kind": "table"}, [], ("missing", "location"), id="absent-location-is-skipped"),
        # `id` is the record's identity and an empty one names nothing, and it is named as EMPTY rather than
        # as absent: they are different defects at the writer. That `id` and `location` are both required
        # while only one may be blank is why this cannot be fixed by dropping `location` from the tuple.
        pytest.param({"id": "", "location": "s3://wh/x", "kind": "table"}, [], ("empty",), id="empty-id-is-skipped"),
    ],
)
def test_a_record_is_judged_by_presence_and_an_empty_identity_is_still_refused(
    tmp_path: Path, payload: dict[str, object], ids: list[str], skip_words: tuple[str, ...]
) -> None:
    _write(tmp_path, "record-1.json", payload)

    records, skipped = _read(tmp_path)

    assert [r["id"] for r in records] == ids
    assert len(skipped) == (1 if skip_words else 0), skipped
    assert all(word in skipped[0] for word in skip_words), skipped
