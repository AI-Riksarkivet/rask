"""A caller an identity provider vouched for: a person ([[XC-214]], [[LH-063]]), or a client signed in for itself (ADR 0181).

ADR 0208. The deployment names every issuer a person or a client signs in at (`OidcSettings.oidc_person_providers`: the
console's `RASK_OIDC_ISSUER`, then `RASK_OIDC_PROVIDERS`), each with an idp id of its own, and a token is named by the
issuer its verified `iss` is, so two issuers' people and clients never share a namespace.

Any OpenID provider names a person by its issuer and their subject: OpenID Connect Core 1.0 § 2 makes `sub` unique within
the issuer and never reassigned, and the issuer's signature binds both. So a person is `<idp id>~<subject>`, the issuer's
idp id and the first of its subject claims the token carries, `sub` unless the deployment names another first for a
provider whose `sub` is not the id a grant needs (Entra's `oid`). That rule is `SubjectNaming`.

A Dex that brokers several upstream providers behind one issuer may be named connector by connector instead
(`ConnectorNaming`, a provider's connector map). Its `sub` packs the connector's id with the upstream user id
(`IDTokenSubject{user_id, conn_id}`, measured on the dev Dex v2.42.0 2026-10-16), so every connector's users share one
idp id and a renamed connector re-keys them. Its `federated_claims`, which Dex adds to the id token and the access token
alike when the client asks for the `federated:id` scope (measured the same day), name the connector, which picks the idp
id, and the upstream user id, which is the subject. A renamed connector is one more entry naming the same idp id. A token
carrying no federated claims, or naming a connector the deployment does not map, names nobody under that rule.

A client signed in for itself with the client-credentials grant is the machine `<idp id>-client~<client id>`. Where the
deployment names how its issuer marks such a token (a provider's client marker: Keycloak's service-account token carries
`client_id` and a `sub` that is a hidden user's; Entra's carries `idtyp: app`), a token carrying the mark is a client's,
named by its `client_id`, `azp` or `appid`. Otherwise a token is a client's when its `sub` is the client's: equal to the
`client_id` the token names (RFC 9068 § 2.2), or Dex's encoding of the client its `azp` names. `azp` alone is never
compared with a raw `sub`: every id token a client is issued names it there, so a person whose provider let them pick a
`sub` equal to a public client's id would read as that client. Dex (v2.46.0 and later) issues such a token the client's
own claims (`UserID = client.ID`) under the empty connector id no configured connector may carry, and names the client in
`azp` whenever the token is issued for another audience, which every door's audience is (dex v2.46.1
`server/grants/clientcredentials.go`, `server/tokens/issuer.go`, `server/tokens/claims.go` `GetAudience`). Keeping a
client's `sub` apart from every person's is the authorization server's (RFC 9068 § 5).

A token naming nobody is refused 401 and the reason is logged.
"""

import base64
import logging
from collections.abc import Sequence

from lance_namespace import UnauthenticatedError
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from lakehouse.governed.machine_identity import ServicePrincipal, client_principal
from lakehouse.governed.oidc import IDToken, OIDCVerifier
from lakehouse.governed.principal import Principal
from lakehouse.governed.settings import ClientMarker, OidcProvider


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


#: The rule a provider names a person by.
type PersonNaming = SubjectNaming | ConnectorNaming

#: The claims a marked client's token names its client id in, the first carried winning: RFC 9068's, OpenID Connect's
#: authorized party, and Entra's application id.
_CLIENT_ID_CLAIMS = ("client_id", "azp", "appid")


def person_naming(provider: OidcProvider) -> PersonNaming:
    """The rule `provider` names a person by (`OidcProvider` refuses subject claims beside a connector map)."""
    if provider.connectors:
        return ConnectorNaming(connectors=provider.connectors)
    if provider.subject_claims is None:
        return SubjectNaming(idp_id=provider.idp_id)
    return SubjectNaming(idp_id=provider.idp_id, claims=provider.subject_claims)


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
    return base64.urlsafe_b64encode(b"\x0a" + length.take_bytes() + raw).rstrip(b"=").decode("ascii")


def _marked_client(claims: IDToken, marker: ClientMarker) -> str | None:
    """The client a token `marker` marks names, `None` when it carries no mark; `UnauthenticatedError` when it names none."""
    extra = claims.model_extra or {}
    mark = extra.get(marker.claim)
    if mark is None or (marker.value is not None and mark != marker.value):
        return None
    client_id = next((value for name in _CLIENT_ID_CLAIMS if isinstance(value := extra.get(name), str) and value), None)
    if client_id is None:
        log.warning("oidc_marked_client_names_no_client", extra={"claim": marker.claim})
        raise UnauthenticatedError("The token is a client's and names no client")
    return client_id


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
    """Verify a bearer one of the deployment's issuers issued, and name the person or the client it proves."""

    def __init__(self, verifier: OIDCVerifier, providers: Sequence[OidcProvider]) -> None:
        self._verifier = verifier
        self._providers = {provider.issuer.rstrip("/"): provider for provider in providers}
        self._naming = {issuer: person_naming(provider) for issuer, provider in self._providers.items()}

    def refresh(self) -> list[tuple[str, str]]:
        """Fetch every issuer's keys now and report what failed; see `OIDCVerifier.refresh`."""
        return self._verifier.refresh()

    async def refresh_forever(self) -> None:
        """Fetch every issuer's keys again in the background until cancelled; see `OIDCVerifier.refresh_forever`."""
        await self._verifier.refresh_forever()

    def verify(self, token: str) -> PersonPrincipal | ServicePrincipal:
        """The caller the token proves; `UnauthenticatedError` for a token at fault, `ProviderUnavailableError` for the IdP."""
        claims = self._verifier.verify(token)
        issuer = claims.iss.rstrip("/")
        provider = self._providers.get(issuer)
        if provider is None:
            raise UnauthenticatedError("Unrecognized token issuer")
        client_id = _signed_in_client(claims) if provider.client_marker is None else _marked_client(claims, provider.client_marker)
        try:
            if client_id is not None:
                return ServicePrincipal(principal=client_principal(provider.idp_id, client_id))
            return PersonPrincipal(principal=self._naming[issuer].person(claims), claims=claims)
        except ValidationError as exc:
            raise UnauthenticatedError("Invalid or expired token") from exc


__all__ = [
    "ConnectorNaming",
    "FederatedClaims",
    "IdpVerifier",
    "PersonNaming",
    "PersonPrincipal",
    "SubjectNaming",
    "person_naming",
]
