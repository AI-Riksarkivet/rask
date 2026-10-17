"""The principal a bearer proves, as the catalog derives it ([[LH-063]]).

A person is `<idp-id>~<user id>`: the idp id the deployment names their Dex connector (`auth.oidc.connectors`) and the
user id that connector reports. No token carries it whole: Dex reports the connector and the user id in
`federated_claims`, and only when the client asks for the `federated:id` scope, while the map from connector to idp id
is the deployment's. So a suite asks the catalog, whose `/management/v1/me` answers exactly the principal every door keys
that bearer on.
"""

from __future__ import annotations

import requests


#: The scope a suite's password grant asks Dex for: `federated:id` makes Dex report the connector and the user id.
DEX_SCOPE = "openid email federated:id"


def principal_of(catalog: str, bearer: str) -> str:
    """The principal key the catalog derives for `bearer`."""
    answer = requests.get(f"{catalog.rstrip('/')}/management/v1/me", headers={"authorization": f"Bearer {bearer}"}, timeout=30)
    assert answer.status_code == 200, f"GET /management/v1/me -> {answer.status_code}: {answer.text[:300]}"
    return str(answer.json()["sub"])
