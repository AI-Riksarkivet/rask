"""A verified caller as a service's verifier hands it to a door, for a test that drives a handler or a dependency with a
caller of its own rather than through a token an issuer signed (`tests.governed_catalog` signs one)."""

from typing import Any

from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal


def person(name: str = "alice", **claims: Any) -> PersonPrincipal:  # noqa: ANN401 — any claim a token carries, validated by IDToken
    """``name`` signed in through Dex: the principal ``dex~<name>``, holding a token whose ``claims`` replace or join the
    ones every verified token carries (its issuer, subject, audience and times)."""
    token = IDToken.model_validate({"iss": "https://dex.example", "sub": name, "aud": "lance", "exp": 1, "iat": 1, **claims})
    return PersonPrincipal(principal=Principal(idp_id="dex", subject=name), claims=token)
