"""What a request is authorized with, before anything is decided: the OpenFGA client, the settings and the caller."""

from typing import Annotated

from fastapi import Depends
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict, InstanceOf, SkipValidation

from catalog.api.dependencies import FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.machine_identity import ServicePrincipal


__all__ = ["AuthzContext", "AuthzContextDep", "get_authz_context"]


class AuthzContext(BaseModel):
    """The three values every gate, grant and listing of a request is decided from, as the request carries them.

    Raw, not resolved: building one raises nothing, so a door still answers an unwired client 503 and a missing caller
    401 where it asks (:func:`catalog.api.authz.decisions.authorization`), never earlier. The client and the settings
    are taken as given (the settings were validated where they were built); the caller is the principal the authenticator
    built, kept as that object: its type is what a grant decides by (a ``ServicePrincipal`` owns nothing it creates), so
    nothing else, a dict of its fields included, is made into one here.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    client: SkipValidation[OpenFgaClient | None]
    settings: SkipValidation[Settings]
    token: InstanceOf[PersonPrincipal] | InstanceOf[ServicePrincipal] | None


async def get_authz_context(settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> AuthzContext:
    """The request's :class:`AuthzContext`, from the dependencies that provide its three values.

    ``async`` because nothing in it blocks: a sync dependency runs in the threadpool, one hop per request for nothing.
    """
    return AuthzContext(client=client, settings=settings, token=token)


AuthzContextDep = Annotated[AuthzContext, Depends(get_authz_context)]
