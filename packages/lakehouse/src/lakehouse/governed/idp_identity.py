"""A caller the identity provider vouched for: a person ([[XC-214]], [[LH-063]]), or a client signed in for itself (ADR 0181).

Any OpenID provider names a person by its issuer and their subject: OpenID Connect Core 1.0 § 2 makes `sub` unique within
the issuer and never reassigned, and the issuer's signature binds both. So a person is `<idp id>~<subject>`, the idp id
the deployment names for its issuer (`RASK_OIDC_IDP_ID`), and the subject the first of `RASK_OIDC_SUBJECT_CLAIMS` the
token carries, `sub` unless the deployment names another first for a provider whose `sub` is not the id a grant needs
(Entra's `oid`). That rule is `SubjectNaming`.

A Dex that brokers several upstream providers behind one issuer may be named connector by connector instead
(`ConnectorNaming`, `RASK_OIDC_CONNECTORS`). Its `sub` packs the connector's id with the upstream user id
(`IDTokenSubject{user_id, conn_id}`, measured on the dev Dex v2.42.0 2026-10-16), so every connector's users share one
idp id and a renamed connector re-keys them. Its `federated_claims`, which Dex adds to the id token and the access token
alike when the client asks for the `federated:id` scope (measured the same day), name the connector, which picks the idp
id, and the upstream user id, which is the subject. A renamed connector is one more entry naming the same idp id. A token
carrying no federated claims, or naming a connector the deployment does not map, names nobody under that rule.

A client signed in for itself with the client-credentials grant is the machine `dex-client~<client id>`, under either
rule, when its token's `sub` is the client's: equal to the `client_id` the token names (RFC 9068 § 2.2), or Dex's encoding
of the client its `azp` names. `azp` alone is never compared with a raw `sub`: every id token a client is issued names it
there, so a person whose provider let them pick a `sub` equal to a public client's id would read as that client. Dex
(v2.46.0 and later) issues such a token the client's own claims (`UserID = client.ID`) under the empty connector id
no configured connector may carry, and names the client in `azp` whenever the token is issued for another audience,
which every door's audience is (dex v2.46.1 `server/grants/clientcredentials.go`, `server/tokens/issuer.go`,
`server/tokens/claims.go` `GetAudience`). Keeping a client's `sub` apart from every person's is the authorization
server's (RFC 9068 § 5).

A token naming nobody is refused 401 and the reason is logged.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping, Sequence

from lance_namespace import UnauthenticatedError
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from lakehouse.governed.machine_identity import ServicePrincipal, client_principal
from lakehouse.governed.oidc import IDToken, OIDCVerifier
from lakehouse.governed.principal import Principal


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


class SubjectNaming(BaseModel):
    """A person is the issuer's idp id and the first of `claims` their token carries."""

    model_config = ConfigDict(frozen=True)

    idp_id: str
    claims: tuple[str, ...] = ("sub",)

    def person(self, claims: IDToken) -> Principal:
        """The person `claims` names; `UnauthenticatedError` when the token carries none of the subject claims."""
        carried = claims.model_dump()
        subject = next((value for name in self.claims if isinstance(value := carried.get(name), str) and value), None)
        if subject is None:
            log.warning("oidc_token_names_no_subject", extra={"claims": list(self.claims)})
            raise UnauthenticatedError("The token names no subject this deployment reads")
        return Principal(idp_id=self.idp_id, subject=subject)


class ConnectorNaming(BaseModel):
    """A person is the idp id their Dex connector maps to and the user id that connector reports."""

    model_config = ConfigDict(frozen=True)

    connectors: dict[str, str]

    def person(self, claims: IDToken) -> Principal:
        """The person `claims` names; `UnauthenticatedError` for no federated claims, or a connector the map does not name."""
        federated = (claims.model_extra or {}).get("federated_claims")
        if federated is None:
            log.warning("oidc_token_names_no_connector", extra={"reason": "no federated_claims: the client did not ask for the federated:id scope"})
            raise UnauthenticatedError("The token names no connector: a client asks for the federated:id scope")
        try:
            connector = FederatedClaims.model_validate(federated)
        except ValidationError as exc:
            raise UnauthenticatedError("Invalid or expired token") from exc
        idp_id = self.connectors.get(connector.connector_id)
        if idp_id is None:
            log.warning("oidc_connector_unmapped", extra={"connector_id": connector.connector_id[:128]})
            raise UnauthenticatedError("The token's connector names no identity provider this deployment knows")
        return Principal(idp_id=idp_id, subject=connector.user_id)


#: The rule a deployment names a person by.
type PersonNaming = SubjectNaming | ConnectorNaming


def person_naming(*, idp_id: str | None, subject_claims: Sequence[str] | None, connectors: Mapping[str, str]) -> PersonNaming:
    """The rule the deployment's settings name a person by (`OidcSettings` refuses any other combination)."""
    if connectors:
        return ConnectorNaming(connectors=dict(connectors))
    if idp_id is None:
        raise ValueError("a person is named by an idp id or by a Dex connector map")
    return SubjectNaming(idp_id=idp_id) if subject_claims is None else SubjectNaming(idp_id=idp_id, claims=tuple(subject_claims))


def _dex_client_subject(client_id: str) -> str:
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
    """The client a token whose `sub` is the client's own names, or `None` when the token is not one."""
    extra = claims.model_extra or {}
    client_id, azp = extra.get("client_id"), extra.get("azp")
    if isinstance(client_id, str) and client_id and claims.sub == client_id:
        return client_id
    if isinstance(azp, str) and azp and claims.sub == _dex_client_subject(azp):
        return azp
    return None


class IdpVerifier:
    """Verify a bearer the identity provider issued, and name the person or the client it proves."""

    def __init__(self, verifier: OIDCVerifier, naming: PersonNaming) -> None:
        self._verifier = verifier
        self._naming = naming

    def refresh(self) -> list[tuple[str, str]]:
        """Fetch the issuer's keys now and report what failed; see `OIDCVerifier.refresh`."""
        return self._verifier.refresh()

    async def refresh_forever(self) -> None:
        """Fetch the issuer's keys again in the background until cancelled; see `OIDCVerifier.refresh_forever`."""
        await self._verifier.refresh_forever()

    def verify(self, token: str) -> PersonPrincipal | ServicePrincipal:
        """The caller the token proves; `UnauthenticatedError` for a token at fault, `ProviderUnavailableError` for the IdP."""
        claims = self._verifier.verify(token)
        client_id = _signed_in_client(claims)
        try:
            if client_id is not None:
                return ServicePrincipal(principal=client_principal(client_id))
            return PersonPrincipal(principal=self._naming.person(claims), claims=claims)
        except ValidationError as exc:
            raise UnauthenticatedError("Invalid or expired token") from exc
