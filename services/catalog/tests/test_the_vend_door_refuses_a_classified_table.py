"""The real `vend_credentials` door, driven end to end: a classified column never reaches a vendor.

[[LH-058]]'s closes-when, and it asks for the DOOR rather than the reader beneath it: "a classified
column cannot be read raw through `credentials` by a subject lacking the column rung".

THE REAL FUNCTION, not a stand-in. Nothing in this repo called `vend_credentials` from a test before
this file, and a stand-in copying its shape is exactly the double that cannot see the change it exists
to catch — the refusal has to sit between the manifest read and `vendor.vend`, and only the real body
can prove which side of that line it landed on.

THE ASSERTION IS THAT THE VENDOR IS NEVER ASKED. Checking `mode == "server_mediated"` alone would pass
over a door that vends a credential and then discards it, which is a credential that was minted at the
object store and handed back to nobody — still an issuance, still auditable, still a real grant for its
TTL. So the fake vendor records every call and the test asserts the count.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import lance
import pyarrow as pa

from catalog.api.v1.endpoints.credentials import vend_credentials
from catalog.core.vending import CLASSIFICATION_KEY
from catalog.schemas import VendedCredentials


class _Namespace:
    """A backend answering `describe_table` with a real on-disk location and nothing else."""

    def __init__(self, location: str) -> None:
        self._location = location

    def describe_table(self, request: Any) -> Any:
        return SimpleNamespace(location=self._location)


class _Vendor:
    """Records every vend it is asked for — the thing under test is whether it is asked at all."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def vend(self, **kwargs: Any) -> VendedCredentials:
        self.calls.append(kwargs)
        # The REAL response model, so the control below exercises the door's own serialization rather
        # than stopping at a shape the door would have rejected anyway.
        return VendedCredentials(storage_options={"aws_access_key_id": "AK", "aws_secret_access_key": "SK"})


def _settings() -> SimpleNamespace:
    # FGA OFF so the read-tier path runs exactly as a cleared reader's would: the router guard has
    # already passed by the time this body runs, which is precisely the hole the row describes.
    return SimpleNamespace(
        fga_enabled=False,
        delimiter="$",
        vending_mode="sts",
        vend_sanctioned_bases=[],
        storage_options=lambda: {},
    )


def _table(tmp_path: Path, *, classify: str | None) -> str:
    location = str(tmp_path / "t.lance")
    dataset = lance.write_dataset(pa.table({"id": [1, 2], "ssn": ["a", "b"]}), location)
    if classify is not None:
        dataset.update_field_metadata({classify: {CLASSIFICATION_KEY: "restricted"}})
    return location


def _vend(location: str) -> tuple[Any, _Vendor]:
    vendor = _Vendor()
    response = asyncio.run(
        vend_credentials(
            id="ns$t",
            # CAST, not a real namespace/Settings: the door reaches `describe_table` and five settings
            # fields, and standing up either for real needs a backend and an STS endpoint that take no
            # part in this defect. Every attribute the door touches IS present on these.
            ns=cast("Any", _Namespace(location)),
            settings=cast("Any", _settings()),
            token=None,
            client=None,
            vendor=vendor,
            web_identity_token=None,
            tier="read",
            branch="",
        )
    )
    return response, vendor


def test_an_unclassified_table_is_still_vended_directly(tmp_path: Path) -> None:
    """The control. Without it, a door that refused everything would pass the real assertion below."""
    response, vendor = _vend(_table(tmp_path, classify=None))
    assert len(vendor.calls) == 1, "the door stopped vending at all — the refusal below proves nothing"
    assert response.mode == "direct"


def test_a_classified_column_stops_the_vend_before_the_vendor(tmp_path: Path) -> None:
    """The row's closes-when: raw object bytes are not handed out for a table with something to mask."""
    response, vendor = _vend(_table(tmp_path, classify="ssn"))
    assert vendor.calls == [], (
        "the vendor was asked for a credential on a table carrying a classified column — a 900 s "
        "object-store session over the whole prefix, which no policy can narrow to exclude a column."
    )
    assert response.mode == "server_mediated"
    assert response.credentials is None


def test_the_refusal_follows_the_FIELD_through_a_rename(tmp_path: Path) -> None:
    """A classification keyed on a NAME would be shed by a rename; this one is keyed on the field."""
    location = _table(tmp_path, classify="ssn")
    dataset: Any = lance.dataset(location)  # see the sibling file: pylance's annotation disagrees with its runtime
    dataset.alter_columns({"path": "ssn", "name": "taxpayer_id"})

    response, vendor = _vend(location)
    assert vendor.calls == [], "a rename shed the classification and the table became vendable again"
    assert response.mode == "server_mediated"
