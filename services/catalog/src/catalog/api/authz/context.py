"""What a request is authorized with, before anything is decided: the OpenFGA client, the settings and the caller."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict, SkipValidation

from catalog.api.dependencies import FgaClientDep, SettingsDep
from catalog.api.security import Caller, CurrentToken
from catalog.core.config import Settings


__all__ = ["AuthzContext", "AuthzContextDep", "get_authz_context"]


class AuthzContext(BaseModel):
    """The three values every gate, grant and listing of a request is decided from, as the request carries them.

    Raw, not resolved: building one raises nothing, so a door still answers an unwired client 503 and a missing caller
    401 where it asks (:func:`catalog.api.authz.decisions.authorization`), never earlier. The client and the settings
    are taken as given (the settings were validated where they were built); the caller is a verified principal.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    client: SkipValidation[OpenFgaClient | None]
    settings: SkipValidation[Settings]
    token: Caller | None


def get_authz_context(settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> AuthzContext:
    """The request's :class:`AuthzContext`, from the dependencies that provide its three values."""
    return AuthzContext(client=client, settings=settings, token=token)


AuthzContextDep = Annotated[AuthzContext, Depends(get_authz_context)]
