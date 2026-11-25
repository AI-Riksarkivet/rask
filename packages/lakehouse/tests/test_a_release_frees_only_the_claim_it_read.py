"""A release frees only the claim it read ([[LH-458]]).

A rename hands its location's claim from the source id to the destination under the claim's ETag
(``take`` with ``previous``), and a drop or deregister of the source releases the source's claim. When
the hand-over lands between the release's read and its write, a release that removes whatever then
stands at the key removes the destination's fresh claim: the destination resolves to the location with
no claim, and a third id takes it and registers over the same bytes, so a destructive drop of either id
deletes what the other still resolves to (the loss :mod:`lakehouse.records.location_claims` exists
to prevent).

Driven on a real local control root. The hand-over runs inside the release's own read of the record,
so it lands after that read and before the release writes, on every run.

A take for a table its door just brought into being records the uuid the door minted ([[LH-519]]), even over a
claim the table's own name still holds: a release lost to a crash leaves that claim behind, and the table it named
has ended, since the door's backend call found the name free.
"""

from pathlib import Path

import pytest

from lakehouse.records import location_claims
from lakehouse.records.location_claims import ClaimStore, LocationClaim, LocationHeldError
from lakehouse.storage import conditional_records
from lakehouse.storage.options import StorageOptions


LOCATION = "s3://lance-catalog/aa11bb22_ns$x"
ENDED = "11111111-1111-4111-8111-111111111111"
MINTED = "22222222-2222-4222-8222-222222222222"


def _resolves(_claim: LocationClaim) -> bool:
    """``is_gone`` for holders that all still resolve to the location."""
    return False


def test_a_release_leaves_the_claim_a_hand_over_wrote_after_its_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ClaimStore(control_root=str(tmp_path))
    location_claims.take(store, LOCATION, "ns$x", segments=["ns", "x"], is_gone=_resolves)
    read = conditional_records.read_json_at
    handed_over: list[str] = []

    def _read_then_hand_over(root_uri: str, storage_options: StorageOptions, key: str) -> conditional_records.StoreRead:
        found = read(root_uri, storage_options, key)
        if not handed_over:
            handed_over.append(key)
            location_claims.take(store, LOCATION, "ns$y", segments=["ns", "y"], previous="ns$x", is_gone=_resolves)
        return found

    monkeypatch.setattr(conditional_records, "read_json_at", _read_then_hand_over)

    released = location_claims.release(store, LOCATION, "ns$x")

    assert handed_over == [location_claims.claim_key(LOCATION)], "the hand-over never ran between the release's read and its write"
    assert released is False
    current = location_claims.holder(store, LOCATION)
    assert current is not None and (current.table, current.segments) == ("ns$y", ["ns", "y"])
    with pytest.raises(LocationHeldError):
        location_claims.take(store, LOCATION, "ns$z", segments=["ns", "z"], is_gone=_resolves)


def test_a_release_by_the_holder_frees_the_location_for_another_table(tmp_path: Path) -> None:
    store = ClaimStore(control_root=str(tmp_path))
    location_claims.take(store, LOCATION, "ns$x", segments=["ns", "x"], is_gone=_resolves)

    released = location_claims.release(store, LOCATION, "ns$x")

    assert released is True
    assert location_claims.holder(store, LOCATION) is None
    location_claims.take(store, LOCATION, "ns$z", segments=["ns", "z"], is_gone=_resolves)
    current = location_claims.holder(store, LOCATION)
    assert current is not None and current.table == "ns$z"


def test_a_new_table_under_a_name_whose_release_was_lost_is_given_its_own_uuid(tmp_path: Path) -> None:
    store = ClaimStore(control_root=str(tmp_path))
    location_claims.take(store, LOCATION, "ns$x", segments=["ns", "x"], is_gone=_resolves, uuid=ENDED)

    taken = location_claims.take(store, LOCATION, "ns$x", segments=["ns", "x"], is_gone=_resolves, uuid=MINTED)

    held = location_claims.holder(store, LOCATION)
    assert (taken.uuid, held.uuid if held is not None else None) == (MINTED, MINTED), "the new table took the ended table's uuid"


def test_a_release_naming_its_uuid_leaves_a_claim_the_same_name_took_since(tmp_path: Path) -> None:
    """A release naming the uuid it owns the claim for frees only that claim: the same name registered again at the
    location since holds the claim under a uuid of its own, and keeps it."""
    store = ClaimStore(control_root=str(tmp_path))
    location_claims.take(store, LOCATION, "ns$x", segments=["ns", "x"], is_gone=_resolves, uuid=MINTED)

    refused = location_claims.release(store, LOCATION, "ns$x", uuid=ENDED)
    owned = location_claims.release(store, LOCATION, "ns$x", uuid=MINTED)

    assert (refused, owned, location_claims.holder(store, LOCATION)) == (False, True, None)


def test_a_record_that_is_no_claim_is_left_out_of_the_held_claims_and_said(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The listing feeds sweeps that must go on past one bad record, and the warning is all that says a location's
    record went unread."""
    store = ClaimStore(control_root=str(tmp_path))
    location_claims.take(store, LOCATION, "ns$x", segments=["ns", "x"], is_gone=_resolves)
    stray = tmp_path / location_claims.claim_key("s3://lance-catalog/stray")
    stray.write_text('{"location": "lance-catalog/stray"}')

    with caplog.at_level("WARNING", logger="lakehouse.records.location_claims"):
        held = location_claims.held_claims(store)

    assert [claim.table for claim in held] == ["ns$x"]
    assert [(r.message, r.__dict__["location"]) for r in caplog.records] == [("location_claim_malformed", "lance-catalog/stray")]
