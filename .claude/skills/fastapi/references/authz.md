# Authorization (authz)

What a request is allowed to do. Pairs with [`authn.md`](authn.md) (who the request is). Two layers — pick the smallest that fits:

| Layer                       | When                                                           | Mechanism                                          |
| --------------------------- | -------------------------------------------------------------- | -------------------------------------------------- |
| **Coarse — role / scope**   | "admin only", "active user only", "has scope X".               | Layered FastAPI dependencies. No external service. |
| **Fine — relational**       | Per-object permissions inherited through relationships (folder → document, org → team → user); audit answers like "who can read X?". | **OpenFGA** (Zanzibar-inspired authorization service). |

## Contents

- Coarse-grained authorization — `require_active`, `require_superuser`, router-level application
- Fine-grained with OpenFGA — when / when not
- OpenFGA model — `.fga` schema with types and relations
- Wire OpenFGA into FastAPI — client factory + `require_permission` dependency
- Beyond `check` — `batch_check`, `list_objects`, `list_users` (with examples)
- Writing tuples — after a DB mutation succeeds
- Service-layer integration + post-filtering lists
- Operational notes
- Local development with the Playground

## Coarse-grained authorization (scopes, roles)

For "admin only" / "active user only" / "has scope X" checks, layer dependencies. No external service needed:

```python
# api/authz_deps.py
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUserDep   # from authn.md — local JWT
from app.models import User


def require_active(user: CurrentUserDep) -> User:
    if not user.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Inactive user")
    return user


def require_superuser(user: Annotated[User, Depends(require_active)]) -> User:
    if not user.is_superuser:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient privileges")
    return user


# Apply at the router level when every route in the group needs it:
admin_router = APIRouter(prefix="/admin", dependencies=[Depends(require_superuser)])
```

Compose the same way with `OIDCUserDep` instead of `CurrentUserDep` when the user comes from an external IdP (see `authn.md`).

## OAuth2 scopes — FastAPI's `Security()` + `SecurityScopes`

Between coarse role checks and OpenFGA's relational permissions sits OAuth2's **scope** primitive: a flat list of capability strings (`users:read`, `items:write`) granted to a token at issue time. FastAPI has first-class support via `Security()` (instead of `Depends()`) and `SecurityScopes`, which also surfaces the available scopes in `/docs`.

```python
# core/scopes.py
from fastapi.security import OAuth2PasswordBearer, SecurityScopes
from fastapi import Depends, HTTPException, Security, status
from typing import Annotated
import jwt

from app.core.config import settings
from app.core.security import ALGORITHM
from app.models import User


SCOPES = {
    "users:read":  "Read user profiles",
    "users:write": "Create / update users",
    "items:read":  "Read items",
    "items:write": "Create / update items",
}

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token", scopes=SCOPES)


async def get_current_user(
    security_scopes: SecurityScopes,
    token: Annotated[str, Depends(oauth2_scheme)],
) -> User:
    authenticate_value = (
        f'Bearer scope="{security_scopes.scope_str}"' if security_scopes.scopes else "Bearer"
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        token_scopes: list[str] = payload.get("scopes", [])
        if not sub:
            raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": authenticate_value})
    except jwt.PyJWTError as e:
        raise HTTPException(401, "Could not validate credentials",
                            headers={"WWW-Authenticate": authenticate_value}) from e

    for required in security_scopes.scopes:
        if required not in token_scopes:
            raise HTTPException(403, f"Missing required scope: {required}",
                                headers={"WWW-Authenticate": authenticate_value})

    return await load_user(sub)   # your repo lookup
```

Apply via `Security(...)` on the route:

```python
@router.get("/users/me")
async def read_users_me(
    user: Annotated[User, Security(get_current_user, scopes=["users:read"])],
) -> User:
    return user
```

**Multiple scopes are AND.** `scopes=["users:delete", "admin"]` requires both. For OR semantics, write a separate dep (an `any(...)` over `token_scopes`).

**Don't roll your own scope hierarchy.** A `SCOPE_HIERARCHY = {"admin": {"users:read", "users:write", ...}}` dict is the start of a poor man's RBAC that becomes unmanageable past ~10 scopes. Once you find yourself wanting hierarchy or per-object rules, switch to **OpenFGA** (below) — the model file IS the hierarchy, validated and visualizable in the Playground.

**Issue scopes at the token endpoint.** Validate `form_data.scopes` against what the user is allowed to request before encoding them into the JWT. Never trust scope claims from a token signed by anyone but you.

## Fine-grained authorization with OpenFGA

Use [OpenFGA](https://openfga.dev/) when permissions become **relational** ("alice can read this document because she's a member of the editors team for its parent folder") and can't be expressed as a simple role/scope.

**Use when:**

- Per-object permissions across many objects (documents, folders, projects, organizations).
- Inherited permissions through relationships (folder → document, org → team → user).
- Audit answers like "who can read this document?" / "what documents can this user read?".
- Permissions sourced from multiple systems (some from your DB, some from an external directory).

**Don't use when:**

- A single `is_admin` flag does the job.
- You have <5 roles and they don't compose.
- You don't yet have anyone asking "why can / can't X access Y?".

**Use when:**

- Per-object permissions across many objects (documents, folders, projects, organizations).
- Inherited permissions through relationships (folder → document, org → team → user).
- Audit answers like "who can read this document?" / "what documents can this user read?".
- Permissions sourced from multiple systems (some from your DB, some from an external directory).

**Don't use when:**

- A single `is_admin` flag does the job.
- You have <5 roles and they don't compose.
- You don't yet have anyone asking "why can/can't X access Y?".

## Model

OpenFGA models live in `model.fga` (treat the file like a migration — see operational notes). Define types and relations:

```
model
  schema 1.1

type user

type editors
  relations
    define member: [user]

type folder
  relations
    define editor: [editors#member]
    define reader: [user] or editor

type document
  relations
    define parent: [folder]
    define reader: reader from parent
    define writer: editor from parent
    define owner: [user] and editor from parent
```

Read: a `document` has a `reader` relation that is inherited from the `parent` folder's `reader`. An `owner` must be _both_ a directly-assigned user AND an editor on the parent.

## Wire it into FastAPI

Client factory (one instance per process, lifecycle in the lifespan):

```python
# core/fga.py
from functools import lru_cache
from openfga_sdk import OpenFgaClient, ClientConfiguration
from openfga_sdk.client import ClientCheckRequest

from app.core.config import settings


@lru_cache(maxsize=1)
def get_fga_client() -> OpenFgaClient:
    return OpenFgaClient(ClientConfiguration(
        api_url=settings.FGA_API_URL,
        store_id=settings.FGA_STORE_ID,
        authorization_model_id=settings.FGA_MODEL_ID,
    ))


async def check(*, user: str, relation: str, object: str) -> bool:
    client = get_fga_client()
    response = await client.check(ClientCheckRequest(
        user=f"user:{user}",
        relation=relation,
        object=object,
    ))
    return bool(response.allowed)
```

Route-level dependency:

```python
# api/fga_deps.py
from typing import Annotated
from fastapi import Depends, HTTPException, status

from app.api.oidc_deps import OIDCUserDep
from app.core.fga import check


def require_permission(relation: str, object_template: str):
    """
    object_template uses `{}` for path params, e.g. "document:{document_id}".
    Returns a dependency that 403s when the OIDC user lacks the relation.
    """
    async def dep(user: OIDCUserDep, **path_params: str) -> None:
        obj = object_template.format(**path_params)
        if not await check(user=user.sub, relation=relation, object=obj):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden")
    return dep


# Usage on a route:
@router.get(
    "/documents/{document_id}",
    dependencies=[Depends(require_permission("reader", "document:{document_id}"))],
)
async def read_document(document_id: str) -> Document: ...
```

## Beyond `check` — useful APIs

OpenFGA exposes more than yes/no checks. The four that matter for an API:

| Call            | Use case                                                  | Endpoint shape                                  |
| --------------- | --------------------------------------------------------- | ----------------------------------------------- |
| `check`         | "Can user X do Y on object Z?"                            | Most route guards.                              |
| `batch_check`   | Same question, many objects, one round-trip.              | Filtering a search result page.                 |
| `list_objects`  | "What documents can user X read?"                         | Building user-scoped dashboards.                |
| `list_users`    | "Who can read document Z?" (audit / sharing UI).          | Permissions dialog.                             |

Prefer `batch_check` over many `check`s when filtering — same network round-trip cost as one call.

```python
from openfga_sdk.client.models import (
    ClientBatchCheckItem, ClientBatchCheckRequest, ClientListObjectsRequest,
    ClientListUsersRequest, ClientTuple, ClientWriteRequest,
)
from openfga_sdk.models.fga_object import FgaObject
from openfga_sdk.models.user_type_filter import UserTypeFilter


async def batch_check(
    sub: str, relation: str, objects: list[str]
) -> dict[str, bool]:
    """Returns {object_id: allowed} for a single user across many objects."""
    client = get_fga_client()
    items = [ClientBatchCheckItem(user=f"user:{sub}", relation=relation, object=o)
             for o in objects]
    resp = await client.batch_check(ClientBatchCheckRequest(checks=items))
    return {r.request.object: r.allowed for r in resp.result}


async def list_objects(sub: str, relation: str, object_type: str) -> list[str]:
    """e.g. list_objects("anne", "reader", "document") → ["document:1", …]"""
    client = get_fga_client()
    resp = await client.list_objects(ClientListObjectsRequest(
        user=f"user:{sub}", relation=relation, type=object_type,
    ))
    return resp.objects


async def list_users(object_id: str, relation: str) -> list[str]:
    """e.g. list_users("document:1", "reader") → ["anne", "bob"]"""
    client = get_fga_client()
    resp = await client.list_users(ClientListUsersRequest(
        object=FgaObject(type=object_id.split(":")[0], id=object_id.split(":")[1]),
        relation=relation,
        user_filters=[UserTypeFilter(type="user")],
    ))
    return [u.object.id for u in resp.users]
```

## Writing tuples — after a DB mutation succeeds

When a new resource is created or sharing changes, write the corresponding tuple. **One write path** — never sprinkle tuple writes across routes.

```python
# core/fga.py — continued
async def write_tuples(items: list[ClientTuple]) -> None:
    client = get_fga_client()
    await client.write(ClientWriteRequest(writes=items))


# services/documents.py
from openfga_sdk.client.models import ClientTuple


async def create_document(session: AsyncSession, *, owner_sub: str,
                          folder_id: str, title: str) -> Document:
    doc = Document(title=title, folder_id=folder_id)
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    # Make owner_sub the owner + link to its parent folder.
    await write_tuples([
        ClientTuple(user=f"user:{owner_sub}", relation="owner", object=f"document:{doc.id}"),
        ClientTuple(user=f"folder:{folder_id}", relation="parent", object=f"document:{doc.id}"),
    ])
    return doc
```

> Pure outbox / 2PC is overkill at this scale. If the OpenFGA write fails after the DB commit, your runbook is "replay tuple writes from the audit log" — accept the small drift window. Add a transactional outbox the day a single failed write becomes a real incident.

## Service-layer integration + post-filtering lists

Route deps (`require_permission`) work for **single-object** GETs / mutations. For **list** endpoints, push the check into the service so you can filter the candidate set:

```python
# services/documents.py
from app.core.fga import batch_check
from app.core.exceptions import AuthorizationError  # see production-patterns.md § Exception handlers


async def get_document(session: AsyncSession, *, sub: str, doc_id: str) -> Document:
    doc = await session.get(Document, doc_id)
    if doc is None:
        raise NotFoundError(f"document {doc_id}")
    if not await check(user=sub, relation="reader", object=f"document:{doc_id}"):
        raise AuthorizationError(f"reader required on document:{doc_id}")
    return doc


async def search_documents(session: AsyncSession, *, sub: str, q: str) -> list[Document]:
    # 1. Fetch candidates with whatever filter the caller wants — no auth at the DB layer.
    candidates = (await session.scalars(
        select(Document).where(Document.title.ilike(f"%{q}%")).limit(200)
    )).all()
    if not candidates:
        return []

    # 2. One batch_check round-trip, not 200 check calls.
    allowed = await batch_check(sub, "reader", [f"document:{d.id}" for d in candidates])
    return [d for d in candidates if allowed.get(f"document:{d.id}")]
```

`AuthorizationError` is a domain exception — register a handler that maps it to `HTTPException(403)` once globally (see `production-patterns.md` § Exception handlers). Routes never construct 403 responses themselves.

### When to use which

| You're building…                         | Pattern                                                            |
| ---------------------------------------- | ------------------------------------------------------------------ |
| `GET /documents/{id}`                    | Route dep `require_permission("reader", "document:{id}")`.         |
| `GET /documents?q=…` (filtered list)     | Service + `batch_check` on the candidate set.                      |
| `GET /me/documents` (user-scoped index)  | Service + `list_objects("reader", "document")` — let FGA enumerate.|
| `GET /documents/{id}/permissions`        | Service + `list_users(f"document:{id}", relation)`.                |
| `POST /documents`                        | Route dep on the parent folder + `write_tuples` after commit.      |

## Operational notes

- **Store + model are versioned in OpenFGA.** Treat the `.fga` file like a migration: bump the model in a deploy step (`fga model write`) and pin `FGA_MODEL_ID` in env. Never read "latest" — risks reading a half-deployed schema.
- **Deploy OpenFGA next to your service** (same VPC / cluster). Authorization checks are on the request hot path; cross-region latency stacks fast.
- **Cache nothing in the app.** OpenFGA already caches; an app-level cache invalidates badly when tuples change.
- **Write tuples through one path.** One service method that creates tuples after a mutation succeeds in your DB. Transactional outbox is overkill until it isn't.

## Local development with the Playground

A single-container OpenFGA + Playground at `localhost:3000/playground` is the fastest way to debug a model.

```yaml
# compose.yml — OpenFGA dev (SQLite-backed)
services:
  openfga:
    image: openfga/openfga:latest
    command: run
    environment:
      OPENFGA_DATASTORE_ENGINE: sqlite
      OPENFGA_DATASTORE_URI: file:/data/openfga.db
    ports: ["8080:8080", "3000:3000"]
    volumes: ["openfga-data:/data"]
volumes: { openfga-data: }
```

Use the Playground to validate the model + run example assertions **before** writing app code that depends on it.
