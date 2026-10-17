"""At the catalog every caller is the principal its issuer names, `<idp-id>~<claim>`, and OpenFGA alone authorizes it.

[[LH-474]], ADR 0156 C5. A pipeline on Kubernetes authenticates with its own namespace's service-account token: the
door verifies it offline against the cluster issuer (signature, `iss`, the door's audience, expiry) and the caller is
`kubernetes~system:serviceaccount:<ns>:<sa>`, from any namespace, with no list of accounts in front of the door. A token
another key signed under the issuer's name, or one minted for another door, authenticates nobody.

[[LH-063]], ADR 0159 D5. A person is `<idp-id>~<user id>`: the idp id the deployment names for the Dex connector that
authenticated them (`RASK_OIDC_CONNECTORS`), and the upstream user id Dex reports in `federated_claims`, never Dex's own
`sub`, which encodes the connector (measured on the dev Dex v2.42.0, `conftest.dex_subject`). So a connector renamed
under the same idp id keeps every grant, and a token that names no mapped connector names nobody.

What a subject may do is decided by the tuples that name it, as the `user` OpenFGA accepts: `user:` and the principal,
percent-encoded. A user id may carry one `:` only (measured on the live store 2026-10-14:
`user:kubernetes~system:serviceaccount:default:rask-sa-maintenance` is refused "the 'user' field is malformed", HTTP 400)
and a `#` makes it a userset, so neither reaches the store unencoded.

The catalog app as deployed, on a real `dir` namespace, with authentication on: its lifespan builds the verifiers from
these settings. OpenFGA is a stand-in at the client the catalog holds: it keeps a tuple set, answers a Check by it,
records a Write, and refuses a user id the live store would not take as one user, so the user id the catalog sends is the
one asserted on.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from lance_namespace import connect
from lance_namespace_urllib3_client.models import CreateNamespaceRequest
from openfga_sdk.client.models import ClientCheckRequest, ClientWriteRequest
from openfga_sdk.exceptions import ValidationException


#: A pipeline's own account in a namespace the release knows nothing about.
_PIPELINE_NS, _PIPELINE_SA = "etl-team", "nightly-pipeline"
#: The OpenFGA user that account is, which one tuple below names.
_PIPELINE_USER = f"user:kubernetes~system%3Aserviceaccount%3A{_PIPELINE_NS}%3A{_PIPELINE_SA}"

#: A person's upstream user id, reported by the Dex connector `corp`, which was renamed `corp-v2`: the deployment maps
#: both ids to the idp id `corp`, as an operator does across the rename.
_PERSON_ID = "4f1c2b9e-7d3a-4e8f-9a6b-0c5d2e1f3a7b"
_CONNECTORS = {"corp": "corp", "corp-v2": "corp"}
_PERSON_USER = f"user:corp~{_PERSON_ID}"
#: An upstream user id carrying `#`, and the one user it is.
_HASHED_ID = "corp#7"
_HASHED_USER = "user:corp~corp%237"


class _CheckResponse:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed


class _OpenFga:
    """OpenFGA's Check over a tuple set, and its Write recorded, refusing a user id that is not one user, as the store does.

    A second `:` is malformed (measured 2026-10-14); a `#` reads as a userset of `user`, a type that defines no relation.
    """

    def __init__(self, tuples: set[tuple[str, str]]) -> None:
        self.tuples = tuples
        self.asked: list[str] = []
        self.written: list[tuple[str, str, str]] = []

    async def check(self, body: ClientCheckRequest, options: object = None) -> _CheckResponse:
        del options
        self.asked.append(body.user)
        _refuse_a_user_that_is_not_one(body.user)
        return _CheckResponse((body.user, body.object) in self.tuples)

    async def write(self, body: ClientWriteRequest, options: object = None) -> None:
        del options
        for tup in body.writes or []:
            _refuse_a_user_that_is_not_one(tup.user)
            self.written.append((tup.user, tup.relation, tup.object))

    async def close(self) -> None:
        return None


def _refuse_a_user_that_is_not_one(user: str) -> None:
    if user.partition(":")[2].count(":") or "#" in user:
        raise ValidationException(reason="the 'user' field is malformed")


@pytest.fixture
def catalog(sa_issuer: Any, dex_issuer: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, _OpenFga]]:
    env = {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": dex_issuer.issuer,
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_OIDC_ALLOW_INSECURE": "true",
        "RASK_OIDC_CONNECTORS": json.dumps(_CONNECTORS),
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

    # The namespace the request reads, granted to the pipeline's account, the person and the user id carrying `#`; and the
    # root object the access-admin door's gate checks, granted to the person.
    openfga = _OpenFga({(_PIPELINE_USER, "namespace:db"), (_PERSON_USER, "namespace:db"), (_HASHED_USER, "namespace:db"), (_PERSON_USER, "estate:rask")})
    with TestClient(app, raise_server_exceptions=False) as client:
        # Authorization on for the requests, after a lifespan that built the verifiers and no OpenFGA client.
        governed = Settings.model_validate(
            {**env, "RASK_OIDC_CONNECTORS": _CONNECTORS, "RASK_FGA_ENABLED": True, "RASK_FGA_API_URL": "http://openfga.invalid:8080"}
        )
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


def _describe(client: TestClient, token: str) -> httpx.Response:
    return client.post("/v1/namespace/db/describe", json={}, headers={"Authorization": f"Bearer {token}"})


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

    answered = _describe(client, token)

    assert answered.status_code == status, answered.text
    assert set(openfga.asked) == ({asked_about} if asked_about else set()), f"OpenFGA was asked about {sorted(set(openfga.asked))}"


@pytest.mark.parametrize(
    ("connector", "user_id", "federated", "status", "asked_about"),
    [
        pytest.param("corp", _PERSON_ID, True, 200, _PERSON_USER, id="a-person-holding-a-tuple"),
        pytest.param("corp-v2", _PERSON_ID, True, 200, _PERSON_USER, id="the-same-person-once-their-connector-is-renamed"),
        pytest.param("corp", _HASHED_ID, True, 200, _HASHED_USER, id="a-user-id-carrying-a-hash-is-one-user-not-a-userset"),
        pytest.param("stray", _PERSON_ID, True, 401, None, id="a-connector-the-deployment-does-not-map"),
        pytest.param("corp", _PERSON_ID, False, 401, None, id="a-token-carrying-no-federated-claims"),
    ],
)
def test_a_person_is_the_principal_their_connector_maps_so_a_renamed_connector_keeps_the_grants(
    catalog: tuple[TestClient, _OpenFga], dex_issuer: Any, connector: str, user_id: str, federated: bool, status: int, asked_about: str | None
) -> None:
    client, openfga = catalog
    token = dex_issuer.mint(user_id, connector=connector, audience="lance-catalog", federated=federated)

    answered = _describe(client, token)

    assert answered.status_code == status, answered.text
    assert set(openfga.asked) == ({asked_about} if asked_about else set()), f"OpenFGA was asked about {sorted(set(openfga.asked))}"


#: The door's refusal of `alice`, bare or as the stored user id `user:alice`, which names the same string.
_NOT_A_PRINCIPAL = "'alice' is not a principal: 'alice' names no identity provider: a principal is '<idp-id>~<subject>'"


@pytest.mark.parametrize(
    ("user", "status", "media", "answer", "written"),
    [
        pytest.param(
            "dex~alice",
            200,
            "application/json",
            {"user": "user:dex~alice"},
            [("user:dex~alice", "reader", "table:db$t")],
            id="a-principal-is-the-one-user-it-is",
        ),
        pytest.param("alice", 400, "application/problem+json", {"detail": _NOT_A_PRINCIPAL}, [], id="a-bare-name-is-refused"),
        pytest.param("user:alice", 400, "application/problem+json", {"detail": _NOT_A_PRINCIPAL}, [], id="a-user-id-naming-no-principal-is-refused"),
    ],
)
def test_a_seeded_tuple_names_a_principal_and_a_bare_name_is_refused(
    catalog: tuple[TestClient, _OpenFga], dex_issuer: Any, user: str, status: int, media: str, answer: dict[str, str], written: list[tuple[str, str, str]]
) -> None:
    """The door every seed writes its grants through ([[LH-063]]): a grant names a principal, `<idp-id>~<subject>`, and is
    stored as the one OpenFGA user it is; a name no identity provider vouches for is refused before anything is written."""
    client, openfga = catalog
    admin = dex_issuer.mint(_PERSON_ID, connector="corp", audience="lance-catalog", federated=True)

    answered = client.post(
        "/management/v1/access/tuples", json={"user": user, "relation": "reader", "object": "table:db$t"}, headers={"Authorization": f"Bearer {admin}"}
    )

    body = answered.json()
    assert (answered.status_code, answered.headers["content-type"], {key: body.get(key) for key in answer}, openfga.written) == (
        status,
        media,
        answer,
        written,
    )
