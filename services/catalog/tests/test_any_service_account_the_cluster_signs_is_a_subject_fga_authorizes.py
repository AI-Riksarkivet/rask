"""At the catalog any token the cluster's service-account issuer signs is a subject, and OpenFGA alone authorizes it.

[[LH-474]], ADR 0156 C5. A pipeline on Kubernetes authenticates with its own namespace's service-account token: the
door verifies it offline against the cluster issuer (signature, `iss`, the door's audience, expiry) and the caller is
`kubernetes~system:serviceaccount:<ns>:<sa>`, from any namespace, with no list of accounts in front of the door. What
that subject may do is decided by the tuples that name it, as the `user` OpenFGA accepts: a user id may carry one `:`
only (measured on the live store 2026-10-14: `user:kubernetes~system:serviceaccount:default:rask-sa-maintenance` is
refused "the 'user' field is malformed", HTTP 400), so the subject's `:` is written `%3A`. A token another key signed
under the issuer's name, or one minted for another door, authenticates nobody.

The catalog app as deployed, on a real `dir` namespace, with authentication on: its lifespan builds the verifiers from
these settings. OpenFGA is a stand-in at the client the catalog holds: it keeps a tuple set, answers a Check by it, and
refuses a malformed user id as the live store does, so the user id the catalog sends is the one asserted on.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from lance_namespace import connect
from lance_namespace_urllib3_client.models import CreateNamespaceRequest
from openfga_sdk.client.models import ClientCheckRequest
from openfga_sdk.exceptions import ValidationException


#: A pipeline's own account in a namespace the release knows nothing about.
_PIPELINE_NS, _PIPELINE_SA = "etl-team", "nightly-pipeline"
#: The OpenFGA user that account is, which the one tuple below names.
_PIPELINE_USER = f"user:kubernetes~system%3Aserviceaccount%3A{_PIPELINE_NS}%3A{_PIPELINE_SA}"


class _CheckResponse:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed


class _OpenFga:
    """OpenFGA's Check over a tuple set, refusing a user id with a second `:` as the store does (measured 2026-10-14)."""

    def __init__(self, tuples: set[tuple[str, str]]) -> None:
        self.tuples = tuples
        self.asked: list[str] = []

    async def check(self, body: ClientCheckRequest, options: object = None) -> _CheckResponse:
        del options
        self.asked.append(body.user)
        if body.user.partition(":")[2].count(":"):
            raise ValidationException(reason="the 'user' field is malformed")
        return _CheckResponse((body.user, body.object) in self.tuples)

    async def close(self) -> None:
        return None


@pytest.fixture
def catalog(sa_issuer: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, _OpenFga]]:
    env = {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": "https://idp.invalid",
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_SA_ISSUER": sa_issuer.issuer,
        "RASK_SA_AUDIENCE": "rask-catalog",
        "RASK_SA_FETCH_TOKEN_FILE": str(sa_issuer.fetch_token_file),
        "RASK_SA_CA_FILE": str(sa_issuer.ca_file),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    connect("dir", {"root": str(tmp_path)}).create_namespace(CreateNamespaceRequest(id=["db"]))

    from catalog.core.config import Settings, get_settings

    get_settings.cache_clear()
    from catalog.main import app

    # One tuple, on the namespace the request reads, naming the pipeline's account and nobody else.
    openfga = _OpenFga({(_PIPELINE_USER, "namespace:db")})
    with TestClient(app, raise_server_exceptions=False) as client:
        # Authorization on for the requests, after a lifespan that built the verifiers and no OpenFGA client.
        governed = Settings.model_validate({**env, "RASK_FGA_ENABLED": True, "RASK_FGA_API_URL": "http://openfga.invalid:8080"})
        app.dependency_overrides[get_settings] = lambda: governed
        app.state.fga = openfga
        yield client, openfga
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _signed_by_another_key(issuer: str, kid: str) -> str:
    """A token claiming the cluster's issuer and its key id, signed by a key that issuer never published."""
    now = int(time.time())
    claims = {
        "iss": issuer,
        "sub": f"system:serviceaccount:{_PIPELINE_NS}:{_PIPELINE_SA}",
        "aud": ["rask-catalog"],
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256", headers={"kid": kid})


def _another_clusters_token(issuer: Any, directory: Any) -> str:
    """The pipeline's account as another cluster's issuer signs it: an issuer of the same kind, just not this door's."""
    other = type(issuer)(directory)
    try:
        return other.mint(_PIPELINE_SA, audience="rask-catalog", namespace=_PIPELINE_NS)
    finally:
        other.close()


@pytest.mark.parametrize(
    ("caller", "status", "asked_about"),
    [
        pytest.param("granted-account", 200, _PIPELINE_USER, id="an-arbitrary-namespaces-account-holding-a-tuple"),
        pytest.param("ungranted-account", 403, "user:kubernetes~system%3Aserviceaccount%3Aetl-team%3Aother-job", id="an-account-holding-none"),
        pytest.param("another-door", 401, None, id="a-token-minted-for-another-door"),
        pytest.param("another-key", 401, None, id="a-token-claiming-the-issuer-signed-by-another-key"),
        pytest.param("another-issuer", 401, None, id="a-token-another-clusters-issuer-signed"),
    ],
)
def test_a_service_account_token_is_its_subject_and_its_tuples_alone_authorize_it(
    catalog: tuple[TestClient, _OpenFga], sa_issuer: Any, tmp_path: Any, caller: str, status: int, asked_about: str | None
) -> None:
    client, openfga = catalog
    token = {
        "granted-account": lambda: sa_issuer.mint(_PIPELINE_SA, audience="rask-catalog", namespace=_PIPELINE_NS),
        "ungranted-account": lambda: sa_issuer.mint("other-job", audience="rask-catalog", namespace=_PIPELINE_NS),
        "another-door": lambda: sa_issuer.mint(_PIPELINE_SA, audience="rask-lineage", namespace=_PIPELINE_NS),
        "another-key": lambda: _signed_by_another_key(sa_issuer.issuer, sa_issuer.kid),
        "another-issuer": lambda: _another_clusters_token(sa_issuer, tmp_path),
    }[caller]()

    answered = client.post("/v1/namespace/db/describe", json={}, headers={"Authorization": f"Bearer {token}"})

    assert answered.status_code == status, answered.text
    assert set(openfga.asked) == ({asked_about} if asked_about else set()), f"OpenFGA was asked about {sorted(set(openfga.asked))}"
