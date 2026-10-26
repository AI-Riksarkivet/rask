"""A release frees only the claim it read ([[LH-458]]).

A rename hands its location's claim from the source id to the destination under the claim's ETag
(``take`` with ``previous``), and a drop or deregister of the source releases the source's claim. When
the hand-over lands between the release's read and its write, a release that removes whatever then
stands at the key removes the destination's fresh claim: the destination resolves to the location with
no claim, and a third id takes it and registers over the same bytes, so a destructive drop of either id
deletes what the other still resolves to (the loss :mod:`service_kit.lakehouse.location_claims` exists
to prevent).

Driven on a real local control root. The hand-over runs inside the release's own read of the record,
so it lands after that read and before the release writes, on every run.

A take for a table its door just brought into being records the uuid the door minted ([[LH-519]]), even over a
claim the table's own name still holds: a release lost to a crash leaves that claim behind, and the table it named
has ended, since the door's backend call found the name free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from service_kit.lakehouse import location_claims, records
from service_kit.lakehouse.location_claims import ClaimStore, LocationClaim, LocationHeldError
from service_kit.lakehouse.objectfs import StorageOptions


LOCATION = "s3://lance-catalog/aa11bb22_ns$x"
ENDED = "11111111-1111-4111-8111-111111111111"
MINTED = "22222222-2222-4222-8222-222222222222"


def _resolves(_claim: LocationClaim) -> bool:
    """``is_gone`` for holders that all still resolve to the location."""
    return False


def test_a_release_leaves_the_claim_a_hand_over_wrote_after_its_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ClaimStore(control_root=str(tmp_path))
    location_claims.take(store, LOCATION, "ns$x", ["ns", "x"], is_gone=_resolves)
    read = records.read_json_at
    handed_over: list[str] = []

    def _read_then_hand_over(root_uri: str, storage_options: StorageOptions, key: str) -> records.StoreRead:
        found = read(root_uri, storage_options, key)
        if not handed_over:
            handed_over.append(key)
            location_claims.take(store, LOCATION, "ns$y", ["ns", "y"], previous="ns$x", is_gone=_resolves)
        return found

    monkeypatch.setattr(records, "read_json_at", _read_then_hand_over)

    released = location_claims.release(store, LOCATION, "ns$x")

    assert handed_over == [location_claims.claim_key(LOCATION)], "the hand-over never ran between the release's read and its write"
    assert released is False
    current = location_claims.holder(store, LOCATION)
    assert current is not None and (current.table, current.segments) == ("ns$y", ["ns", "y"])
    with pytest.raises(LocationHeldError):
        location_claims.take(store, LOCATION, "ns$z", ["ns", "z"], is_gone=_resolves)


def test_a_release_by_the_holder_frees_the_location_for_another_table(tmp_path: Path) -> None:
    store = ClaimStore(control_root=str(tmp_path))
    location_claims.take(store, LOCATION, "ns$x", ["ns", "x"], is_gone=_resolves)

    released = location_claims.release(store, LOCATION, "ns$x")

    assert released is True
    assert location_claims.holder(store, LOCATION) is None
    location_claims.take(store, LOCATION, "ns$z", ["ns", "z"], is_gone=_resolves)
    current = location_claims.holder(store, LOCATION)
    assert current is not None and current.table == "ns$z"


def test_a_new_table_under_a_name_whose_release_was_lost_is_given_its_own_uuid(tmp_path: Path) -> None:
    store = ClaimStore(control_root=str(tmp_path))
    location_claims.take(store, LOCATION, "ns$x", ["ns", "x"], is_gone=_resolves, uuid=ENDED)

    taken = location_claims.take(store, LOCATION, "ns$x", ["ns", "x"], is_gone=_resolves, uuid=MINTED)

    held = location_claims.holder(store, LOCATION)
    assert (taken.uuid, held.uuid if held is not None else None) == (MINTED, MINTED), "the new table took the ended table's uuid"
