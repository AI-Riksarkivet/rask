"""OpenFGA stores held in memory, which the real catalog app asks through the real `lakehouse.governed.fga_reads` and `fga_writes` functions.

Neither is for a test whose claim is who may act, or which tuples a door leaves behind: that test asks a real OpenFGA
(the root conftest's ``openfga_store``, `tests/integration/governed_catalog.py`). ``Grants`` answers each check from an
explicit grant set, for a test that counts what a door asks or whose claim is what lineage records. ``TupleStore`` holds
what the doors write and allows every check, for a test whose claim is neither.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from openfga_sdk.client.models import ClientBatchCheckRequest, ClientBatchCheckResponse, ClientCheckRequest, ClientTuple, ClientWriteRequest
from openfga_sdk.client.models.batch_check_single_response import ClientBatchCheckSingleResponse
from openfga_sdk.client.models.list_users_request import ClientListUsersRequest
from openfga_sdk.exceptions import ApiException
from openfga_sdk.models import CheckResponse, ListUsersResponse, ReadRequestTupleKey, ReadResponse, Tuple, TupleKey
from openfga_sdk.models.error_code import ErrorCode
from openfga_sdk.models.validation_error_message_response import ValidationErrorMessageResponse

from lakehouse.governed.fga_ids import user_object
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal, is_principal
from tests.app_state import app_of


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
        """Allow ``user`` (a person's name or a principal key, as :func:`principal_key` reads it) each ``(relation, object)`` in ``checks``."""
        self.allowed |= {(user_object(principal_key(user)), relation, obj) for relation, obj in checks}


class TupleStore:
    """An OpenFGA store held in memory that keeps every tuple the doors write and allows every check."""

    def __init__(self) -> None:
        self.tuples: set[Triple] = set()

    async def check(self, body: ClientCheckRequest, options: Options = None) -> CheckResponse:
        del body, options
        return CheckResponse(allowed=True)

    async def batch_check(self, body: ClientBatchCheckRequest, options: Options = None) -> ClientBatchCheckResponse:
        del options
        return ClientBatchCheckResponse(
            result=[
                ClientBatchCheckSingleResponse(
                    allowed=True, request=ClientTuple(user=item.user, relation=item.relation, object=item.object), correlation_id=str(index)
                )
                for index, item in enumerate(body.checks)
            ]
        )

    async def read(self, body: ReadRequestTupleKey, options: Options = None) -> ReadResponse:
        """The tuples on ``body.object``, or every tuple held when the read names no object, as OpenFGA reads a whole store."""
        del options
        held = [
            Tuple(key=TupleKey(user=u, relation=r, object=o), timestamp=datetime.now(UTC))
            for u, r, o in sorted(self.tuples)
            if body.object is None or o == body.object
        ]
        return ReadResponse(tuples=held, continuation_token="")

    async def write(self, body: ClientWriteRequest, options: Options = None) -> None:
        """Hold what is written; refuse a delete of a tuple not held, as OpenFGA does, which a revoke reads as gone."""
        del options
        doomed = {(t.user, t.relation, t.object) for t in body.deletes or []}
        if doomed - self.tuples:
            raise _absent_delete()
        self.tuples |= {(t.user, t.relation, t.object) for t in body.writes or []}
        self.tuples -= doomed

    async def list_users(self, body: ClientListUsersRequest, options: Options = None) -> ListUsersResponse:
        """The project door's display of its admins, which it degrades to nobody without; no claim reads it here."""
        del body, options
        return ListUsersResponse(users=[])

    async def close(self) -> None:
        return None


def _absent_delete() -> ApiException:
    """What the SDK raises for a delete of an absent tuple: a 400 whose parsed body carries the structured code."""
    exc = ApiException(status=400, reason="Bad Request")
    exc.parsed_exception = ValidationErrorMessageResponse(
        code=ErrorCode.WRITE_FAILED_DUE_TO_INVALID_INPUT,
        message="cannot delete a tuple which does not exist",
    )
    return exc


def principal_key(who: str) -> str:
    """The principal ``who`` names: a principal key as written, and a bare name the person ``dex~<name>``.

    `dex` is the idp id the dev Dex's password connector maps to, so ``"alice"`` is the person that connector reports as
    `alice`.
    """
    return who if is_principal(who) else Principal(idp_id="dex", subject=who).key


def act_as(client: TestClient, who: str) -> None:
    """Make every later request on ``client`` authenticate as the person ``who`` names (see :func:`principal_key`).

    The app's verifier is a mock; it answers the `PersonPrincipal` a verified Dex token for that person would prove.
    """
    principal = Principal.parse(principal_key(who))
    claims = IDToken(iss="i", sub=principal.subject, aud="lance", exp=1, iat=1)
    app_of(client).state.oidc.verify.return_value = PersonPrincipal(principal=principal, claims=claims)
