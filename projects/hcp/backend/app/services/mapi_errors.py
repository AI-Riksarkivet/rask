"""Domain exceptions for the MAPI service layer.

These exceptions decouple services from the web framework.  The API
layer catches them and translates to ``HTTPException``.
"""

from __future__ import annotations


class MapiError(Exception):
    """Base error for MAPI operations."""

    def __init__(self, message: str, *, http_status: int = 502):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


class MapiTransportError(MapiError):
    """Connection / timeout errors talking to HCP MAPI."""


class MapiResponseError(MapiError):
    """HCP MAPI returned a non-2xx status."""

    def __init__(self, message: str, *, http_status: int, hcp_status: int):
        super().__init__(message, http_status=http_status)
        self.hcp_status = hcp_status
