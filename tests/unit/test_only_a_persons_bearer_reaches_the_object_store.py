"""`web_identity` vending re-presents the caller's raw bearer to the object store, so only a person's bearer may reach it."""

from __future__ import annotations

from fastapi.security import HTTPAuthorizationCredentials

from catalog.api import security
from service_kit.governed.machine_identity import ServicePrincipal
from service_kit.governed.oidc import IDToken


_SUB = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"
_PRODUCER = "system:serviceaccount:default:rask-sa-medallion-producer"


def test_only_a_persons_bearer_is_forwarded_to_the_object_store() -> None:
    """`web_identity` vending re-presents the raw bearer, so a service's catalog-audience token must answer None."""
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="a.real.jwt")
    person = IDToken(iss="https://dex.example/dex", sub=_SUB, aud="lance-catalog", iat=0, exp=1 << 31)
    service = ServicePrincipal(service_account=_PRODUCER)

    assert security.raw_bearer(credentials, person) == "a.real.jwt"
    assert security.raw_bearer(credentials, service) is None
