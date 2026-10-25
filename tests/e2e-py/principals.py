"""The principal a bearer proves, as the catalog derives it ([[LH-063]]).

A person is `<idp-id>~<user id>`: the idp id the deployment names their Dex connector (`auth.oidc.connectors`) and the
user id that connector reports. No token carries it whole: Dex reports the connector and the user id in
`federated_claims`, and only when the client asks for the `federated:id` scope, while the map from connector to idp id
is the deployment's. So a suite asks the catalog, whose `/management/v1/me` answers exactly the principal every door keys
that bearer on.
"""

from __future__ import annotations

import os

import requests


#: The client a suite signs in as: the development Dex's sign-in client, rask's `auth.oidc.clientId`.
DEX_CLIENT = os.environ.get("LANCE_E2E_OIDC_CLIENT_ID", "rask-web")

#: The lakehouse's audience, rask's `auth.oidc.audience`: every governed door refuses a token that does not name it.
DEX_AUDIENCE = os.environ.get("LANCE_E2E_OIDC_AUDIENCE", "rask-lakehouse")

#: The scope a suite's password grant asks Dex for: `federated:id` makes Dex report the connector and the user id, and
#: Dex's cross-client scope puts the lakehouse's audience in the token, which it issues only to a client the audience
#: client trusts ([[LH-515]]).
DEX_SCOPE = f"openid email federated:id audience:server:client_id:{DEX_AUDIENCE}"


def principal_of(catalog: str, bearer: str) -> str:
    """The principal key the catalog derives for `bearer`."""
    answer = requests.get(f"{catalog.rstrip('/')}/management/v1/me", headers={"authorization": f"Bearer {bearer}"}, timeout=30)
    assert answer.status_code == 200, f"GET /management/v1/me -> {answer.status_code}: {answer.text[:300]}"
    return str(answer.json()["sub"])
