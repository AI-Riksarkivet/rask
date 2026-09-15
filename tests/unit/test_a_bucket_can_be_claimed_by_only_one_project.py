"""Two projects cannot both come to own the same bucket, however the creates interleave.

[[LH-053]]. `create_warehouse` reads the warehouse listing, scans it for a rival claim on the caller's
bucket, and writes its record afterwards. Between the read and the write there is a window — a network
provision sits inside it — and the scan is a pure function of a listing taken before the rival existed.
Two concurrent creates naming one bucket under different projects therefore BOTH pass the guard and
BOTH write, which is precisely the cross-tenant takeover the guard was added to close: a project policy
set through the second warehouse governs, and can destroy version history in, the first project's data.

THE GUARD IS NOT WRONG, IT IS UNARBITRATED — that distinction is why the fix is a claim record rather
than a better scan. No read-then-check can close this; the store has to pick the winner. The estate
already answers exactly this shape for its id-minting creates: a conditional PUT (`If-None-Match: *`
on S3, `O_CREAT|O_EXCL` locally) whose loser surfaces as `RecordExistsError` rather than
last-writer-wins, which is how `bind_namespace` arbitrates a namespace binding.

A SAME-PROJECT RE-CREATE MUST STAY IDEMPOTENT. `create_warehouse`'s partial-failure retry path relies
on re-POSTing an existing id, so a claim already held by the caller's own project is a pass, not a
collision. Only a claim naming a DIFFERENT project is a refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lance_namespace import NamespaceAlreadyExistsError

from catalog.services import warehouses


@pytest.fixture
def control_root(tmp_path: Path) -> str:
    return tmp_path.as_uri()


def test_the_first_project_to_claim_a_bucket_gets_it(control_root: str) -> None:
    warehouses.claim_bucket(control_root, {}, bucket="acme-wh", project="acme", warehouse_id="wh-acme")

    assert warehouses.bucket_claim(control_root, {}, bucket="acme-wh") == {
        "bucket": "acme-wh",
        "project": "acme",
        "warehouse_id": "wh-acme",
    }


def test_a_rival_project_is_refused_even_when_it_read_the_listing_first(control_root: str) -> None:
    """The race itself: the rival's scan saw nothing, and the STORE still refuses it.

    Written as two claims with no listing in between, because that is what the window amounts to — the
    scan cannot see a record that does not exist yet, so its verdict carries no information about who
    writes next.
    """
    warehouses.claim_bucket(control_root, {}, bucket="acme-wh", project="acme", warehouse_id="wh-acme")

    with pytest.raises(NamespaceAlreadyExistsError, match="acme-wh"):
        warehouses.claim_bucket(control_root, {}, bucket="acme-wh", project="mallory", warehouse_id="wh-evil")


def test_the_owning_project_may_reclaim_its_own_bucket(control_root: str) -> None:
    """The partial-failure retry path: re-POSTing an existing warehouse must not collide with itself."""
    warehouses.claim_bucket(control_root, {}, bucket="acme-wh", project="acme", warehouse_id="wh-acme")

    warehouses.claim_bucket(control_root, {}, bucket="acme-wh", project="acme", warehouse_id="wh-acme")


def test_one_project_may_back_two_warehouses_with_one_bucket(control_root: str) -> None:
    """The work+gold pair the estate actually ships, which is why the claim is keyed by PROJECT.

    `projects_claiming_bucket` already subtracts the caller's own project on purpose; a claim keyed by
    warehouse id would refuse the second warehouse of a legitimate pair.
    """
    warehouses.claim_bucket(control_root, {}, bucket="acme-wh", project="acme", warehouse_id="wh-work")

    warehouses.claim_bucket(control_root, {}, bucket="acme-wh", project="acme", warehouse_id="wh-gold")


def test_an_unclaimed_bucket_reads_as_nothing(control_root: str) -> None:
    """So a caller can tell "nobody holds this" from "somebody does" without catching an exception."""
    assert warehouses.bucket_claim(control_root, {}, bucket="never-claimed") is None
