"""A person is the user their Dex connector reports, under the idp id the deployment names that connector ([[LH-063]]).

ADR 0159 D5. Dex brokers every upstream identity provider behind one issuer, and its `sub` packs the connector's id
with the upstream user id (`IDTokenSubject{user_id, conn_id}`, measured on the dev Dex v2.42.0 2026-10-16), so a grant
keyed on `sub` is lost when a connector is renamed, and every connector's users share one namespace. The door keys on
`federated_claims` instead, which Dex puts in the id token and the access token alike when the client asks for the
`federated:id` scope (measured the same day): the connector id picks the idp id from `RASK_OIDC_CONNECTORS`, and the
upstream user id is the subject. A renamed connector is one more entry naming the same idp id.

A token carrying no federated claims, or naming a connector the deployment does not map, names nobody, so it is refused
401 and the reason is logged: the first is a client that did not ask for `federated:id`, the second a connector the
deployment never named.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from lance_namespace import UnauthenticatedError
from pydantic import BaseModel, ConfigDict, ValidationError

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

    @property
    def subject(self) -> str:
        """The principal key, the subject id every door's authorization reads from a verified caller."""
        return self.principal.key


class PersonVerifier:
    """Verify a person's bearer against the IdP, and name the principal it proves."""

    def __init__(self, verifier: OIDCVerifier, connectors: Mapping[str, str]) -> None:
        self._verifier = verifier
        self._connectors = dict(connectors)

    def warm(self) -> list[tuple[str, str]]:
        """Resolve the issuer now and report what failed; see `OIDCVerifier.warm`."""
        return self._verifier.warm()

    def verify(self, token: str) -> PersonPrincipal:
        """The person the token proves; `UnauthenticatedError` for a token at fault, `ProviderUnavailableError` for the IdP."""
        claims = self._verifier.verify(token)
        federated = (claims.model_extra or {}).get("federated_claims")
        if federated is None:
            log.warning("oidc_token_names_no_connector", extra={"reason": "no federated_claims: the client did not ask for the federated:id scope"})
            raise UnauthenticatedError("The token names no connector: a client asks for the federated:id scope")
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
