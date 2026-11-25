"""The catalog's answer at its vend doors, for a test that stands in for the catalog in front of a client of those doors
(maintenance asking for a maintain-tier session or a trash reclaim session): the body is the door's own response model,
so a field the catalog renames or adds is seen here too."""

from typing import Any

from catalog.schemas import CredentialResponse, VendedCredentials


def vended(storage_options: dict[str, str], *, location: str | None = None) -> dict[str, Any]:
    """A direct vend of ``storage_options``, for the table at ``location`` (a reclaim session names none)."""
    answer = CredentialResponse(mode="direct", credentials=VendedCredentials(storage_options=storage_options), location=location)
    return answer.model_dump(mode="json", exclude_none=True)
