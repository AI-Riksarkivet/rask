"""The authentication vectors of ``gen_port_vectors.py``: who the Python auth plane says a bearer names.

Every caller is put to the real ``lakehouse.governed.door_caller.caller_of`` with the real verifiers and settings.
Only the key source is replaced: each verifier is handed one recorded key where it would have fetched its issuer's,
so the tokens need no issuer running and no private key in the repository. A time claim is recorded as an offset from
the moment the token is checked, which the reader resolves against its own clock.
"""

from __future__ import annotations

import base64
import os
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


#: The secret the recorded tokens are signed with and the key id they name: a fixture's, protecting nothing.
TOKEN_SECRET = "recorded-tokens-only-fixture-secret"  # noqa: S105 - named as a fixture in the vector file itself
KID = "k1"
_IDP = "https://idp.example"
_SA = "https://kubernetes.default.svc"


def _now(offset: int) -> dict[str, int]:
    return {"$now": offset}


def _environments() -> dict[str, dict[str, str]]:
    """The environments the callers are recorded under, by name."""
    base = {"RASK_OIDC_ENABLED": "true", "RASK_OIDC_ISSUER": _IDP, "RASK_OIDC_AUDIENCE": "lance", "RASK_OIDC_IDP_ID": "idp"}
    accounts = {"RASK_SA_ISSUER": _SA, "RASK_SA_AUDIENCE": "rask"}
    return {
        "off": {"RASK_INSECURE_ALLOW_UNAUTHENTICATED": "true"},
        "plain": base | accounts,
        "subject claims": base | {"RASK_OIDC_SUBJECT_CLAIMS": '["email", "sub"]'},
        "connectors": base | {"RASK_OIDC_IDP_ID": "dex", "RASK_OIDC_CONNECTORS": '{"local": "local", "ldap": "corp"}'},
        "marked clients": base | {"RASK_OIDC_CLIENT_MARKER": '{"claim": "typ", "value": "client"}'},
        "marked by presence": base | {"RASK_OIDC_CLIENT_MARKER": '{"claim": "client_only"}'},
        "two issuers": base | {"RASK_OIDC_PROVIDERS": '[{"issuer": "https://other.example/", "idp_id": "other"}]'},
        "short-lived accounts": base | accounts | {"RASK_SA_MAX_TOKEN_SECONDS": "600"},
    }


def _refused_environments() -> dict[str, dict[str, str]]:
    """Environments the settings are checked under beside the recorded ones; most are refused."""
    base = _environments()["plain"]
    return {
        "nothing set": {},
        "enabled with no issuer": {"RASK_OIDC_ENABLED": "true", "RASK_OIDC_AUDIENCE": "lance", "RASK_OIDC_IDP_ID": "idp"},
        "enabled with no audience": {"RASK_OIDC_ENABLED": "true", "RASK_OIDC_ISSUER": _IDP, "RASK_OIDC_IDP_ID": "idp"},
        "enabled with no idp id": {"RASK_OIDC_ENABLED": "true", "RASK_OIDC_ISSUER": _IDP, "RASK_OIDC_AUDIENCE": "lance"},
        "an http issuer": base | {"RASK_OIDC_ISSUER": "http://idp.example"},
        "an http issuer, allowed": base | {"RASK_OIDC_ISSUER": "http://idp.example", "RASK_OIDC_ALLOW_INSECURE": "true"},
        "an idp id in capitals": base | {"RASK_OIDC_IDP_ID": "Idp"},
        "an idp id that is a machine's": base | {"RASK_OIDC_IDP_ID": "kubernetes"},
        "an idp id ending as a client's": base | {"RASK_OIDC_IDP_ID": "dex-client"},
        "subject claims beside connectors": base | {"RASK_OIDC_SUBJECT_CLAIMS": '["email"]', "RASK_OIDC_CONNECTORS": '{"local": "local"}'},
        "no subject claims named": base | {"RASK_OIDC_SUBJECT_CLAIMS": "[]"},
        "an empty subject claim": base | {"RASK_OIDC_SUBJECT_CLAIMS": '["email", ""]'},
        "an empty connector id": base | {"RASK_OIDC_CONNECTORS": '{"": "local"}'},
        "a connector mapped to a machine's idp": base | {"RASK_OIDC_CONNECTORS": '{"local": "kubernetes"}'},
        "a marker on a claim every token carries": base | {"RASK_OIDC_CLIENT_MARKER": '{"claim": "sub"}'},
        "one issuer named twice": base | {"RASK_OIDC_PROVIDERS": '[{"issuer": "https://idp.example/", "idp_id": "other"}]'},
        "one idp id for two issuers": base | {"RASK_OIDC_PROVIDERS": '[{"issuer": "https://other.example", "idp_id": "idp"}]'},
        "a second issuer over http": base | {"RASK_OIDC_PROVIDERS": '[{"issuer": "http://other.example", "idp_id": "other"}]'},
        "an account issuer with no audience": base | {"RASK_SA_AUDIENCE": ""},
        "an account issuer over http": base | {"RASK_SA_ISSUER": "http://kubernetes.default.svc"},
        "the account issuer is a person's": base | {"RASK_SA_ISSUER": _IDP + "/"},
        "a zero account lifetime": base | {"RASK_SA_MAX_TOKEN_SECONDS": "0"},
        "a zero key lifetime": base | {"RASK_OIDC_CACHE_TTL": "0"},
        "authorization without authentication": {"RASK_FGA_ENABLED": "true"},
        "a retired name": base | {"LINEAGE_OIDC_ISSUER": _IDP},
        "a retired name in lower case": base | {"lance_fga_enabled": "true"},
        "enabled written as 1": base | {"RASK_OIDC_ENABLED": "1"},
        "enabled written as a word that is no boolean": base | {"RASK_OIDC_ENABLED": "enabled"},
        "connectors that are no json": base | {"RASK_OIDC_CONNECTORS": "local=local"},
    }


def _caller_cases() -> list[dict[str, Any]]:
    """The callers put to the door: the environment, the token's claims (or the bearer as sent), and which verifiers are up."""
    person = {"iss": _IDP, "sub": "ada", "aud": "lance", "exp": _now(300), "iat": _now(-10)}
    account = {"iss": _SA, "sub": "system:serviceaccount:rask:lineage", "aud": "rask", "exp": _now(300), "iat": _now(-10)}
    dex_subject = "CgZjbGllbnQ"  # what Dex signs a client-credentials token's `sub` as, for the client "client"

    def case(name: str, env: str = "plain", claims: dict[str, Any] | None = None, **sent: Any) -> dict[str, Any]:  # noqa: ANN401 - a claim is arbitrary JSON
        return {"name": name, "env": env, "claims": claims, "kid": KID, "bearer": None, "verifiers": ["idp", "accounts"]} | sent

    def without(claims: dict[str, Any], *dropped: str) -> dict[str, Any]:
        return {key: value for key, value in claims.items() if key not in dropped}

    return [
        case("authentication off", env="off"),
        case("no bearer"),
        case("a person", claims=person),
        case("an issuer written with a trailing slash", claims=person | {"iss": _IDP + "/"}),
        case("another issuer", claims=person | {"iss": "https://evil.example"}),
        case("no issuer", claims=without(person, "iss")),
        case("expired", claims=person | {"exp": _now(-300)}),
        case("expired within the leeway", claims=person | {"exp": _now(-30)}),
        case("issued in the future", claims=person | {"iat": _now(300)}),
        case("issued ahead within the leeway", claims=person | {"iat": _now(30)}),
        case("not valid yet", claims=person | {"nbf": _now(300)}),
        case("valid from the past", claims=person | {"nbf": _now(-5)}),
        case("another audience", claims=person | {"aud": "other"}),
        case("an audience among several", claims=person | {"aud": ["other", "lance"]}),
        case("an empty audience list", claims=person | {"aud": []}),
        case("an audience that is a number", claims=person | {"aud": 5}),
        case("no audience", claims=without(person, "aud")),
        case("no subject", claims=without(person, "sub")),
        case("an empty subject", claims=person | {"sub": ""}),
        case("a subject that is a number", claims=person | {"sub": 5}),
        case("a subject with a control character", claims=person | {"sub": "ada\u0007"}),
        case("a subject outside ascii", claims=person | {"sub": "åsa/öst ~ 100%"}),
        case("no expiry", claims=without(person, "exp")),
        case("no issue time", claims=without(person, "iat")),
        case("an expiry that is text", claims=person | {"exp": "soon"}),
        case("an unknown key id", claims=person, kid="k2"),
        case("no key id", claims=person, kid=None),
        case("not a token", bearer="not-a-token"),
        case("three empty parts", bearer=".."),
        case("the person verifier is down", claims=person, verifiers=["accounts"]),
        case("no bearer and the person verifier is down", verifiers=["accounts"]),
        case("a client by its client id", claims=person | {"sub": "pipeline", "client_id": "pipeline"}),
        case("a client by its authorized party", claims=person | {"sub": dex_subject, "azp": "client"}),
        case("an authorized party that is not the subject", claims=person | {"azp": "client"}),
        case("a marked client", env="marked clients", claims=person | {"typ": "client", "azp": "pipeline"}),
        case("a marked client that names no client", env="marked clients", claims=person | {"typ": "client"}),
        case("a mark of another value is a person", env="marked clients", claims=person | {"typ": "user", "client_id": "ada"}),
        case("marked by presence", env="marked by presence", claims=person | {"client_only": True, "appid": "pipeline"}),
        case("unmarked under a presence marker is a person", env="marked by presence", claims=person | {"sub": "pipeline", "client_id": "pipeline"}),
        case("the first subject claim present", env="subject claims", claims=person | {"email": "ada@example.org"}),
        case("the next subject claim", env="subject claims", claims=person),
        case("an empty subject claim is passed over", env="subject claims", claims=person | {"email": ""}),
        case("a connector's person", env="connectors", claims=person | {"federated_claims": {"connector_id": "ldap", "user_id": "u-17"}}),
        case("no federated claims", env="connectors", claims=person),
        case("an unmapped connector", env="connectors", claims=person | {"federated_claims": {"connector_id": "github", "user_id": "u"}}),
        case("federated claims with no user", env="connectors", claims=person | {"federated_claims": {"connector_id": "ldap"}}),
        case("a client under connectors", env="connectors", claims=person | {"sub": "pipeline", "client_id": "pipeline"}),
        case("the second issuer's person", env="two issuers", claims=person | {"iss": "https://other.example"}),
        case("a service account", claims=account),
        case("a service account of another audience", claims=account | {"aud": "lance"}),
        case("a service account minted for too long", env="short-lived accounts", claims=account | {"exp": _now(900), "iat": _now(-10)}),
        case("a service account within the lifetime", env="short-lived accounts", claims=account | {"exp": _now(500), "iat": _now(-10)}),
        case("a subject that is no service account", claims=account | {"sub": "ada"}),
        case("a service account with no name", claims=account | {"sub": "system:serviceaccount:rask:"}),
        case("a service account with a colon in its name", claims=account | {"sub": "system:serviceaccount:rask:a:b"}),
        case("the account verifier is down", claims=account, verifiers=["idp"]),
        case("an account token where no account issuer is set", env="subject claims", claims=account),
    ]


def _resolved(claims: dict[str, Any], now: int) -> dict[str, Any]:
    return {key: now + value["$now"] if isinstance(value, dict) and "$now" in value else value for key, value in claims.items()}


def auth_vectors() -> dict[str, list[Any]]:
    """Each caller with who the door says it is, and each environment with whether the settings take it."""
    import jwt
    from lance_namespace import LanceNamespaceError
    from pydantic_settings import BaseSettings, SettingsConfigDict

    from lakehouse.governed import idp_identity, machine_identity, oidc
    from lakehouse.governed.door_caller import caller_of
    from lakehouse.governed.settings import GovernedAuthSettings

    class Auth(GovernedAuthSettings, BaseSettings):
        model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")

    def settings_under(environment: dict[str, str]) -> Auth:
        with patch.dict(os.environ, environment, clear=True):
            return Auth()

    key = jwt.PyJWK({"kty": "oct", "k": base64.urlsafe_b64encode(TOKEN_SECRET.encode()).rstrip(b"=").decode(), "kid": KID, "alg": "HS256"})
    keys: Any = SimpleNamespace(
        uri="https://recorded/keys", jwk_set_cache=None, get_signing_keys=lambda refresh=False: [key], match_kid=jwt.PyJWKClient.match_kid
    )

    def holding_the_key(verifier: oidc.OIDCVerifier, issuers: list[str]) -> None:
        for issuer in issuers:
            spec = oidc._Discovery(issuer=issuer.rstrip("/"), jwks_uri="https://recorded/keys")
            verifier._held[issuer.rstrip("/")] = oidc._Provider(spec=spec, jwk_client=keys, algorithms=["HS256"])

    def state_for(settings: Auth, up: list[str]) -> Any:  # noqa: ANN401 - the app state the door reads its verifiers from
        state = SimpleNamespace(oidc=None, sa_oidc=None)
        providers = settings.oidc_person_providers
        if "idp" in up and settings.oidc_enabled and providers and settings.oidc_audience:
            verifier = oidc.OIDCVerifier(
                [provider.issuer for provider in providers], settings.oidc_audience, settings.oidc_cache_ttl, leeway=settings.oidc_leeway
            )
            holding_the_key(verifier, [provider.issuer for provider in providers])
            state.oidc = idp_identity.IdpVerifier(verifier, providers)
        if "accounts" in up and settings.sa_issuer and settings.sa_audience:
            state.sa_oidc = machine_identity.ServiceAccountVerifier(
                settings.sa_issuer,
                settings.sa_audience,
                cache_ttl=settings.oidc_cache_ttl,
                leeway=settings.oidc_leeway,
                fetch_token_file=None,
                ca_file=None,
                max_lifetime=settings.sa_max_token_seconds,
            )
            holding_the_key(state.sa_oidc._oidc, [settings.sa_issuer])
        return state

    environments = _environments()
    callers: list[dict[str, Any]] = []
    for case in _caller_cases():
        settings = settings_under(environments[case["env"]])
        bearer = case["bearer"]
        if case["claims"] is not None:
            bearer = jwt.encode(
                _resolved(case["claims"], int(time.time())), TOKEN_SECRET, algorithm="HS256", headers={"kid": case["kid"]} if case["kid"] else None
            )
        try:
            caller = caller_of(state_for(settings, case["verifiers"]), settings, bearer)
        except LanceNamespaceError as refusal:
            callers.append(case | {"refused": {"name": type(refusal).__name__, "code": int(refusal.code), "message": str(refusal)}})
            continue
        if caller is None:
            callers.append(case | {"caller": None})
            continue
        kind = "service" if isinstance(caller, machine_identity.ServicePrincipal) else "person"
        callers.append(case | {"caller": {"kind": kind, "key": caller.subject, "fga_user": caller.principal.fga_user}})

    taken: list[dict[str, Any]] = []
    for name, environment in (environments | _refused_environments()).items():
        try:
            settings_under(environment)
        except ValueError:
            taken.append({"name": name, "env": environment, "taken": False})
        else:
            taken.append({"name": name, "env": environment, "taken": True})
    return {
        "secret": [{"secret": TOKEN_SECRET, "kid": KID}],
        "environments": [{"name": name, "env": environment} for name, environment in environments.items()],
        "callers": callers,
        "settings": taken,
    }
