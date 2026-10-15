"""An OpenFGA store held in memory, for integration tests whose claim is who may act.

The real catalog app asks it through the real `service_kit.governed.fga` functions, and it answers each check
from an explicit grant set, so a test names every grant it relies on and nothing else is allowed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openfga_sdk.client.models import ClientBatchCheckRequest, ClientBatchCheckResponse, ClientCheckRequest, ClientTuple, ClientWriteRequest
from openfga_sdk.client.models.batch_check_single_response import ClientBatchCheckSingleResponse
from openfga_sdk.models import CheckResponse, ReadRequestTupleKey, ReadResponse

from service_kit.governed.oidc import IDToken


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

type Triple = tuple[str, str, str]
type Options = dict[str, int | str | dict[str, int | str]] | None


class Grants:
    """An OpenFGA store held in memory that allows exactly the checks in ``allowed``."""

    def __init__(self) -> None:
        self.allowed: set[Triple] = set()

    async def check(self, body: ClientCheckRequest, options: Options = None) -> CheckResponse:
        del options
        return CheckResponse(allowed=(body.user, body.relation, body.object) in self.allowed)

    async def batch_check(self, body: ClientBatchCheckRequest, options: Options = None) -> ClientBatchCheckResponse:
        del options
        return ClientBatchCheckResponse(
            result=[
                ClientBatchCheckSingleResponse(
                    allowed=(item.user, item.relation, item.object) in self.allowed,
                    request=ClientTuple(user=item.user, relation=item.relation, object=item.object),
                    correlation_id=str(index),
                )
                for index, item in enumerate(body.checks)
            ]
        )

    async def read(self, body: ReadRequestTupleKey, options: Options = None) -> ReadResponse:
        del body, options
        return ReadResponse(tuples=[], continuation_token="")

    async def write(self, body: ClientWriteRequest, options: Options = None) -> None:
        del body, options

    async def close(self) -> None:
        return None

    def grant(self, user: str, *checks: tuple[str, str]) -> None:
        """Allow ``user`` each ``(relation, object)`` in ``checks``."""
        self.allowed |= {(f"user:{user}", relation, obj) for relation, obj in checks}


def act_as(client: TestClient, sub: str) -> None:
    """Make every later request on ``client`` authenticate as ``sub`` (the app's OIDC verifier is a mock)."""
    client.app.state.oidc.verify.return_value = IDToken(iss="i", sub=sub, aud="lance", exp=1, iat=1)
