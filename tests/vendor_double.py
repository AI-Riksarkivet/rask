"""The catalog's credential vendor, for a test whose claim is whom a door vends a credential to, at what tier and for
which location: each ask is kept, and every one is answered with the same session, which no store accepts.

A test hands it to the app in place of the vendor the app builds (`app.dependency_overrides[get_vendor]`). Each method
carries the protocol's own parameters, so a door that asks with one the protocol does not name fails here as it would
against the real vendor.
"""

from collections.abc import Sequence
from typing import Any, Final

from pydantic import SecretStr

from catalog.core.vending import ReclaimScope, SessionTier, TableRecords, VendedSession
from lakehouse.storage.credential import S3KeyPair


__all__ = ["VENDED", "RecordingVendor"]

#: What every ask is answered with: a session's three parts, as an exchange at the store's STS hands them over.
VENDED: Final = VendedSession(
    key_pair=S3KeyPair(access_key_id="AK", secret_access_key=SecretStr("SK"), session_token=SecretStr("ST")),
    endpoint=None,
    region="us-east-1",
    expires_at_millis=1,
)


class RecordingVendor:
    """The `catalog.core.vending.CredentialVendor` a test reads back: :attr:`calls`, every ask with each of its arguments
    named, in the order they were made. It answers each with :data:`VENDED`."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def vend(
        self,
        *,
        table_location: str,
        tier: SessionTier,
        session_name: str,
        bases: Sequence[str] = (),
        data_directories: Sequence[str] = (),
        branch: str = "",
        records: TableRecords | None = None,
    ) -> VendedSession | None:
        """Keep a table's ask."""
        self.calls.append(
            {
                "table_location": table_location,
                "tier": tier,
                "session_name": session_name,
                "bases": bases,
                "data_directories": data_directories,
                "branch": branch,
                "records": records,
            }
        )
        return VENDED

    def vend_reclaim(self, scope: ReclaimScope, *, session_name: str) -> VendedSession | None:
        """Keep a reclaim's ask."""
        self.calls.append({"scope": scope, "session_name": session_name})
        return VENDED
