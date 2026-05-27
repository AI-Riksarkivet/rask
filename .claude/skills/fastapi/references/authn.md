# Authentication & Authorization

Patterns for authentication (who is the request?) and authorization (what may they do?) in this project's FastAPI services. Covers three layers — pick the smallest one that fits the requirement.

## Contents

- House style — non-negotiable conventions
- Password hashing (local auth) — `pwdlib` Argon2 + bcrypt
- Self-issued JWT (local auth) — `PyJWT`, login + verify + current-user dep
- API keys (service-to-service) — `APIKeyHeader`, hashed at rest, scope per key
- OIDC token verification (external IdP) — JWKS cache, provider quick-start (Keycloak / Dex / Okta / Auth0 / Entra / Google), local-dev IdP, custom token models, protected routes
- Authorization → see [`authz.md`](authz.md)
- Putting it together — request flow ordering

| Layer                                       | When                                                                                              | Tools                             |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------- | --------------------------------- |
| **Local password + self-issued JWT**        | First-party users, you own the identity store, no SSO requirement.                                | `pwdlib`, `PyJWT`                 |
| **External OIDC token verification**        | Users authenticate against an IdP (Okta, Auth0, Entra, Keycloak, Google). You only verify tokens. | `PyJWT`, `httpx`, `cachetools`    |
| **Fine-grained authorization with OpenFGA** | Per-object permissions (who can read which document) that don't fit role/scope checks.            | `openfga-sdk`, `openfga` docker   |

> **Rule of thumb:** start with role/scope checks. Reach for OpenFGA only when permissions are relational (`user X can read folder Y because they're a member of editors Z`) — that's where if/else explodes.

## House style

- **`pwdlib` for password hashing**, not `passlib`. Argon2 primary, bcrypt fallback for legacy hashes — matches the full-stack-fastapi-template pattern.
- **`PyJWT` for tokens**, not `python-jose` (less maintained).
- **`httpx` for JWKS fetch**, not `requests` (async + same client as the rest of our stack).
- **`Annotated[..., Depends(...)]` aliases** for all auth dependencies. No bare `Depends()` in signatures.
- **Don't import a wrapper lib** for OIDC verification. Roll it in-house — it's ~80 lines and gives you precise control over caching, error mapping, and token-model evolution.
- **Pydantic models for token payloads**, never `dict`. The token is untrusted input — parse, don't validate.
- **Boundary error mapping**: cryptographic / JWT errors at the edge are mapped to `HTTPException(401)`. Don't leak `jwt.ExpiredSignatureError` or library names in `WWW-Authenticate`.

## Password hashing (local auth)

```python
# core/security.py
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

# Argon2 is the primary; bcrypt stays in the chain so legacy hashes still verify
# and get transparently rehashed on the next successful login.
password_hash = PasswordHash((Argon2Hasher(), BcryptHasher()))


def hash_password(plain: str) -> str:
    return password_hash.hash(plain)


def verify_password(plain: str, hashed: str) -> tuple[bool, str | None]:
    """Return (ok, new_hash_if_upgraded). Persist `new_hash_if_upgraded` to migrate legacy hashes."""
    return password_hash.verify_and_update(plain, hashed)
```

In the login flow, persist `new_hash_if_upgraded` when it's not `None` — that's how you migrate users off bcrypt without a batch job.

## Self-issued JWT (local auth)

When you're the identity provider, issue HS256 JWTs signed with `SECRET_KEY` (or RS256 if you publish a JWKS to other services).

```python
# core/security.py — JWT issue
import jwt
from datetime import datetime, timedelta, timezone

ALGORITHM = "HS256"


def create_access_token(subject: str, expires_in: timedelta) -> str:
    expire = datetime.now(timezone.utc) + expires_in
    return jwt.encode({"sub": subject, "exp": expire}, settings.SECRET_KEY, algorithm=ALGORITHM)
```

```python
# api/deps.py — verify and resolve current user
from typing import Annotated
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
import jwt

from app.core.config import settings
from app.core.security import ALGORITHM
from app.models import User
from app.api.deps import SessionDep

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/login/access-token")
TokenDep = Annotated[str, Depends(oauth2_scheme)]

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(session: SessionDep, token: TokenDep) -> User:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise CREDENTIALS_ERROR
    except jwt.PyJWTError as e:
        raise CREDENTIALS_ERROR from e

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise CREDENTIALS_ERROR
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]
```

Login endpoint and timing-attack-resistant authentication live in `crud.py` (see project-template.md § Repository pattern).

## API keys (service-to-service)

For internal-only / machine-to-machine traffic where there's no user. **Don't reuse this for end users** — JWT or OIDC are the right tools for human authn.

Store keys hashed at rest, exactly like passwords. Compare with a constant-time check.

```python
# core/api_keys.py
from typing import Annotated
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from app.api.deps import SessionDep
from app.core.security import password_hash   # pwdlib from § Password hashing


api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


class ServiceClient(BaseModel):
    name: str
    scopes: list[str] = []


async def get_service_client(
    session: SessionDep,
    key: Annotated[str | None, Security(api_key_scheme)],
) -> ServiceClient:
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-API-Key header required")

    # Lookup by hash prefix (first 12 chars) so we can index in SQL, then verify the full hash.
    row = await session.scalar(
        select(APIKeyRow).where(APIKeyRow.prefix == key[:12]).limit(1)
    )
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")

    ok, new_hash = password_hash.verify_and_update(key, row.hashed_key)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    if new_hash:                # transparent rehash on upgraded algorithm
        row.hashed_key = new_hash
        await session.commit()

    return ServiceClient(name=row.service_name, scopes=row.scopes)


ServiceDep = Annotated[ServiceClient, Depends(get_service_client)]
```

Usage:

```python
@router.post("/internal/sync", dependencies=[Depends(get_service_client)])
async def sync_data() -> dict[str, str]: ...
```

**Rules:**

- **Hash on issue, never store plaintext.** Issue a key once, show it to the user, hash it (`password_hash.hash(key)`), keep only the hash + a short prefix for lookup.
- **Rotate-on-leak playbook ready.** Revoking = deleting the DB row. No "old key still works for 24h" grace periods.
- **One scope-style permission per key.** Don't mix: `users:read` keys are different DB rows from `users:write` keys.
- **Don't accept keys via query string.** `?api_key=…` leaks to logs and browser history. Header only.

## OIDC token verification (external IdP)

When users authenticate against an external OIDC provider and you only need to verify the tokens, **do not** add a wrapper library. The verification is ~80 lines and you'll want full control over: JWKS cache TTL, custom token claims, error mapping, audience strategy.

Pattern (inspired by `fastapi-oidc`, adapted to our stack — `httpx`, `PyJWT`, in-process cache):

```python
# core/oidc.py
import time
import httpx
import jwt
from pydantic import BaseModel, ConfigDict


class IDToken(BaseModel):
    """OIDC ID token claims. extra='allow' to keep provider-specific fields accessible."""
    model_config = ConfigDict(extra="allow")
    iss: str
    sub: str
    aud: str | list[str]
    exp: int
    iat: int


# Provider-specific JSON; narrow at the call site rather than modelling every RFC 8414 field.
type DiscoveryDoc = dict[str, object]
type JWKS = dict[str, object]


class _OIDCCache:
    """Single-process JWKS + discovery cache. For multi-process, mount a shared Redis cache."""
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._discovery: tuple[float, DiscoveryDoc] | None = None
        self._jwks: tuple[float, JWKS] | None = None

    def _fresh(self, slot: tuple[float, object]) -> bool:
        return (time.monotonic() - slot[0]) < self._ttl


async def discover(issuer: str, cache: _OIDCCache) -> DiscoveryDoc:
    if (slot := cache._discovery) is not None and cache._fresh(slot):
        return slot[1]
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
        r.raise_for_status()
        spec = r.json()
    cache._discovery = (time.monotonic(), spec)
    return spec


async def jwks(jwks_uri: str, cache: _OIDCCache) -> JWKS:
    if (slot := cache._jwks) is not None and cache._fresh(slot):
        return slot[1]
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(jwks_uri)
        r.raise_for_status()
        keys = r.json()
    cache._jwks = (time.monotonic(), keys)
    return keys


async def verify_oidc_token(
    token: str,
    *,
    issuer: str,
    audience: str,
    cache: _OIDCCache,
) -> IDToken:
    spec = await discover(issuer, cache)
    keys = await jwks(spec["jwks_uri"], cache)
    algorithms: list[str] = spec["id_token_signing_alg_values_supported"]

    # PyJWT picks the right key from the JWKS via the kid header.
    jwk_client = jwt.PyJWKClient(spec["jwks_uri"], cache_jwk_set=True, max_cached_keys=16)
    signing_key = jwk_client.get_signing_key_from_jwt(token).key

    payload = jwt.decode(
        token,
        signing_key,
        algorithms=algorithms,
        audience=audience,
        issuer=spec["issuer"],
        options={"verify_at_hash": False},  # we don't have the access token here
    )
    return IDToken.model_validate(payload)
```

Wire it into a FastAPI dependency:

```python
# api/oidc_deps.py
from functools import lru_cache
from typing import Annotated
from fastapi import Depends, HTTPException, status
from fastapi.security import OpenIdConnect
import jwt

from app.core.config import settings
from app.core.oidc import IDToken, _OIDCCache, verify_oidc_token


@lru_cache(maxsize=1)
def _cache() -> _OIDCCache:
    return _OIDCCache(ttl_seconds=settings.OIDC_CACHE_TTL)


oidc_scheme = OpenIdConnect(openIdConnectUrl=f"{settings.OIDC_ISSUER}/.well-known/openid-configuration")
RawTokenDep = Annotated[str, Depends(oidc_scheme)]


async def get_oidc_user(raw: RawTokenDep) -> IDToken:
    token = raw.split(" ", 1)[-1]  # strip "Bearer "
    try:
        return await verify_oidc_token(
            token,
            issuer=settings.OIDC_ISSUER,
            audience=settings.OIDC_AUDIENCE,
            cache=_cache(),
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid OIDC token") from e


OIDCUserDep = Annotated[IDToken, Depends(get_oidc_user)]
```

### Provider quick-start

Drop-in values for the most common OIDC providers. Setting `OIDC_ISSUER` + `OIDC_AUDIENCE` is enough — discovery handles JWKS URI and signing algorithms automatically.

| Provider                | `OIDC_ISSUER`                                                  | `OIDC_AUDIENCE` (typical)              | Notes                                                                       |
| ----------------------- | -------------------------------------------------------------- | -------------------------------------- | --------------------------------------------------------------------------- |
| **Keycloak**            | `https://kc.example.com/realms/<realm>`                        | `account` or your client ID            | Self-hostable. Discovery: `…/.well-known/openid-configuration`.             |
| **Dex** (k8s-native)    | `https://dex.example.com`                                      | client ID configured in `staticClients`| Federates against GitHub/LDAP/SAML/etc. Pairs well with Kubernetes RBAC.    |
| **Okta**                | `https://<tenant>.okta.com/oauth2/default`                     | `api://default` or your custom audience| Use `/oauth2/default` (or your custom auth server), **not** the bare tenant URL. |
| **Auth0**               | `https://<tenant>.auth0.com/`                                  | API identifier from Auth0 dashboard     | Trailing slash on issuer is mandatory.                                       |
| **Microsoft Entra (Azure AD)** | `https://login.microsoftonline.com/<tenant_id>/v2.0` | application (client) ID                 | Use `v2.0` issuer; v1 tokens have different claims.                          |
| **Google**              | `https://accounts.google.com`                                  | OAuth 2.0 client ID                     | `aud` is the client ID, not a custom audience.                               |

The verification code in this file works against all of them — no per-provider code changes.

### Local IdP for dev: Keycloak or Dex

Spin one up in a sidecar to develop against real OIDC locally:

```yaml
# compose.yml — Keycloak dev
services:
  keycloak:
    image: quay.io/keycloak/keycloak:latest
    command: start-dev
    environment:
      KC_BOOTSTRAP_ADMIN_USERNAME: admin
      KC_BOOTSTRAP_ADMIN_PASSWORD: admin
    ports: ["8081:8080"]
```

Set `OIDC_ISSUER=http://localhost:8081/realms/master` in `.env`, create a client in the Keycloak admin UI, and you've got the exact same code path you'll use in prod.

### Custom token models

Provider-specific claims (Okta's `groups`, Auth0's `https://your.app/roles`) belong in a subclass:

```python
class OktaIDToken(IDToken):
    groups: list[str] = []
    email: str
```

Pass the class through `verify_oidc_token` so the parsed result is the right shape:

```python
async def verify_oidc_token[T: IDToken](
    token: str, *, issuer: str, audience: str, cache: _OIDCCache,
    token_cls: type[T] = IDToken,
) -> T:
    # ... same as before ...
    return token_cls.model_validate(payload)
```

### Protected routes — putting it on endpoints

Three patterns that compose. Each adds a more expensive check; combine only what you need.

```python
# api/routes/example.py
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.oidc_deps import OIDCUserDep                # authn (parsed token)
from app.api.fga_deps import require_permission          # fine-grained authz

router = APIRouter(prefix="/api/v1")


# Public — anyone can call.
@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# Protected — any authenticated user.
@router.get("/me")
async def me(user: OIDCUserDep) -> dict[str, str]:
    return {"sub": user.sub, "email": getattr(user, "email", "")}


# Protected + role check (coarse).
def require_admin(user: OIDCUserDep) -> None:
    if "admin" not in getattr(user, "groups", []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")


@router.get("/admin/stats", dependencies=[Depends(require_admin)])
async def admin_stats() -> dict[str, int]:
    return {"users": 42}


# Protected + per-object permission (fine, via OpenFGA).
@router.get(
    "/documents/{document_id}",
    dependencies=[Depends(require_permission("reader", "document:{document_id}"))],
)
async def read_document(document_id: str) -> dict[str, str]:
    return {"id": document_id, "content": "…"}
```

**Apply at the router level** when *every* route in the group needs the same check — cheaper to read and harder to forget:

```python
admin_router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(require_admin)],   # runs before every admin route
)
```

### Why not just install `fastapi-oidc`?

- The lib is ~200 lines, mostly thin wrappers around `python-jose` (which is less maintained than `PyJWT`).
- We already have `httpx` and `PyJWT` in the project — no new deps.
- Caching is in-process by default in that lib too; if you need cross-process, you're rewriting anyway.
- Rolling our own keeps the token model first-class (Pydantic), our error mapping consistent, and the audit surface tiny.


## Authorization → see `authz.md`

All authz patterns — coarse role / scope dependencies (`require_active`, `require_superuser`) and fine-grained per-object permissions with **OpenFGA** — live in [`authz.md`](authz.md). This file is strictly about *who* the request is. The combined ordering (authn → coarse → fine) appears below in § Putting it together.


## Putting it together

A typical request to a protected endpoint in this project:

1. **Authentication** — OIDC dep (`OIDCUserDep`) verifies the JWT signature, audience, issuer; returns a `IDToken`.
2. **Coarse authz** — router-level dep checks `require_active` (cheap, no network).
3. **Fine-grained authz** — route-level dep calls OpenFGA `check` for the specific object the path identifies.
4. **Business logic** — gets the user and proceeds; never re-checks the token or re-queries permissions inside.

That order is deliberate: cheapest check first, most expensive (OpenFGA round-trip) last.
