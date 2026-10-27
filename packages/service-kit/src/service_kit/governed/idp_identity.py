"""A caller Dex vouched for: a person, the user their connector reports ([[LH-063]]), or a client signed in for itself (ADR 0181).

ADR 0159 D5. Dex brokers every upstream identity provider behind one issuer, and its `sub` packs the connector's id
with the upstream user id (`IDTokenSubject{user_id, conn_id}`, measured on the dev Dex v2.42.0 2026-10-16), so a grant
keyed on `sub` is lost when a connector is renamed, and every connector's users share one namespace. The door keys on
`federated_claims` instead, which Dex puts in the id token and the access token alike when the client asks for the
`federated:id` scope (measured the same day): the connector id picks the idp id from `RASK_OIDC_CONNECTORS`, and the
upstream user id is the subject. A renamed connector is one more entry naming the same idp id.

A Dex client signed in with the client-credentials grant (Dex v2.46.0 and later) is the machine `dex-client~<client
id>`. Dex refuses `federated:id` on that grant, issues the claims of the client itself (`UserID = client.ID`) under the
empty connector id no configured connector may carry, and names the client in `azp` whenever the token is issued for
another audience, which every door's audience is (dex v2.46.1 `server/grants/clientcredentials.go`,
`server/tokens/issuer.go`, `server/tokens/claims.go` `GetAudience`). So such a token is told by its `sub` being exactly
`IDTokenSubject{user_id = azp}`: a person's `sub` always encodes a connector, and the issuer's signature binds both claims.

A token carrying no federated claims that is not a client's, or naming a connector the deployment does not map, names
nobody, so it is refused 401 and the reason is logged: the first is a person's client that did not ask for
`federated:id`, the second a connector the deployment never named.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping

from lance_namespace import UnauthenticatedError
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from service_kit.governed.machine_identity import ServicePrincipal, client_principal
from service_kit.governed.oidc import IDToken, OIDCVerifier
from service_kit.governed.principal import Principal


log = logging.getLogger(__name__)


class FederatedClaims(BaseModel):
    """Dex's `federated_claims`: the connector that authenticated the user, and the id that connector reports."""

    connector_id: str
    user_id: str


class PersonPrincipal(BaseModel):
    """A caller the IdP vouched for: the principal its connector maps to, and the claims of the token that proved it."""

    model_config = ConfigDict(frozen=True)

    principal: Principal
    claims: IDToken

    @field_validator("principal")
    @classmethod
    def _a_person(cls, principal: Principal) -> Principal:
        if principal.is_machine:
            raise ValueError(f"{principal.key!r} is a machine, not a person")
        return principal

    @property
    def subject(self) -> str:
        """The principal key, the subject id every door's authorization reads from a verified caller."""
        return self.principal.key


def _client_subject(client_id: str) -> str:
    """Dex's `sub` for a client signed in for itself: base64url, unpadded, of the protobuf `IDTokenSubject{user_id = 1}`.

    The empty `conn_id` (field 2) is a proto3 default, so it is not encoded (dex `server/internal/codec.go`).
    """
    raw = client_id.encode("utf-8")
    length = bytearray()
    size = len(raw)
    while True:
        low, size = size & 0x7F, size >> 7
        length.append(low | (0x80 if size else 0))
        if not size:
            break
    return base64.urlsafe_b64encode(b"\x0a" + bytes(length) + raw).rstrip(b"=").decode("ascii")


def _signed_in_client(claims: IDToken) -> str | None:
    """The client a client-credentials token names, or `None` when the token is not one."""
    client_id = (claims.model_extra or {}).get("azp")
    if not isinstance(client_id, str) or not client_id:
        return None
    return client_id if claims.sub == _client_subject(client_id) else None


class IdpVerifier:
    """Verify a bearer Dex issued, and name the person or the client it proves."""

    def __init__(self, verifier: OIDCVerifier, connectors: Mapping[str, str]) -> None:
        self._verifier = verifier
        self._connectors = dict(connectors)

    def warm(self) -> list[tuple[str, str]]:
        """Resolve the issuer now and report what failed; see `OIDCVerifier.warm`."""
        return self._verifier.warm()

    def verify(self, token: str) -> PersonPrincipal | ServicePrincipal:
        """The caller the token proves; `UnauthenticatedError` for a token at fault, `ProviderUnavailableError` for the IdP."""
        claims = self._verifier.verify(token)
        federated = (claims.model_extra or {}).get("federated_claims")
        if federated is None:
            client_id = _signed_in_client(claims)
            if client_id is None:
                log.warning("oidc_token_names_no_connector", extra={"reason": "no federated_claims: the client did not ask for the federated:id scope"})
                raise UnauthenticatedError("The token names no connector: a client asks for the federated:id scope")
            try:
                return ServicePrincipal(principal=client_principal(client_id))
            except ValidationError as exc:
                raise UnauthenticatedError("The token's client names no principal") from exc
        try:
            connector = FederatedClaims.model_validate(federated)
        except ValidationError as exc:
            raise UnauthenticatedError("Invalid or expired token") from exc
        idp_id = self._connectors.get(connector.connector_id)
        if idp_id is None:
            log.warning("oidc_connector_unmapped", extra={"connector_id": connector.connector_id[:128]})
            raise UnauthenticatedError("The token's connector names no identity provider this deployment knows")
        try:
            principal = Principal(idp_id=idp_id, subject=connector.user_id)
        except ValidationError as exc:
            raise UnauthenticatedError("Invalid or expired token") from exc
        return PersonPrincipal(principal=principal, claims=claims)
