# Authentication

The HCP Unified API uses **OAuth2 password flow** to issue JWT bearer tokens. Credentials provided at login are encrypted into the token and forwarded to HCP on every subsequent API request -- the API itself does not store passwords.

!!! tip "Use the rahcp SDK"
    The [Python SDK](../sdk/index.md) handles authentication automatically -- including token refresh on 401:
    ```python
    from rahcp_client import HCPClient
    async with HCPClient(endpoint="http://localhost:8000/api/v1",
                          username="admin", password="secret", tenant="dev-ai") as client:
        await client.s3.list_buckets()  # authenticated automatically
    ```
    Or from environment variables: `client = HCPClient.from_env()`

## Auth flow

1. **Login** by sending your credentials to `POST /api/v1/auth/token`.
2. Receive a **JWT access token** in the response.
3. Include the token in the `Authorization: Bearer <token>` header on all subsequent requests.
4. The API decodes the JWT on each request and uses the embedded credentials to authenticate against HCP.

### Username formats

| Format | Scope | Example |
|--------|-------|---------|
| `username` | System-level access (no tenant) | `admin` |
| `tenant/username` | Tenant-scoped access | `dev-ai/admin` |

When logging in with `tenant/username`, the API extracts the tenant name and routes MAPI requests to the correct tenant context. The tenant can also be provided as a separate `tenant` form field (used by the frontend); the explicit field takes priority over the slash notation.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/auth/token` | Login (OAuth2 password flow). Body: `username`, `password` (form data), optional `tenant`. Returns `access_token` and `token_type`. |

### POST /api/v1/auth/token

**Request body** (form data, `application/x-www-form-urlencoded`):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `username` | string | Yes | HCP username, or `tenant/username` for tenant-scoped access. |
| `password` | string | Yes | HCP password. |
| `tenant` | string | No | Tenant name. If provided, takes priority over the slash notation in `username`. |

**Response** (`200 OK`):

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer"
}
```

## JWT details

| Property | Value |
|----------|-------|
| Algorithm | HS256 |
| Default expiration | 480 minutes (8 hours) |
| Expiration setting | `API_TOKEN_EXPIRE_MINUTES` environment variable |
| Signing key setting | `API_SECRET_KEY` environment variable |

### Token payload

| Claim | Description |
|-------|-------------|
| `sub` | Username (without tenant prefix) |
| `pwd` | Password (used to authenticate against HCP on each request) |
| `tenant` | Tenant name (present only for tenant-scoped tokens) |
| `exp` | Expiration timestamp (UTC) |

!!! warning "Protect your API_SECRET_KEY"
    The JWT contains the user's password in the `pwd` claim. The token is signed but **not encrypted** -- anyone with access to a token can decode the payload. In production:

    - Set `API_SECRET_KEY` to a strong, random value (do **not** use the default `change-me-in-production`).
    - Always serve the API over HTTPS to prevent token interception.
    - Keep token expiration reasonable (the default 8 hours is suitable for interactive sessions).

## HCP auth types

The API supports two HCP authentication mechanisms, configured via the `HCP_AUTH_TYPE` environment variable (default: `hcp`):

| Type | Header format | Description |
|------|---------------|-------------|
| `hcp` | `HCP base64(username):md5(password)` | HCP native authentication. The username is base64-encoded and the password is MD5-hashed. |
| `ad` | `AD username:password` | Active Directory authentication. Credentials are passed as plain text (over HTTPS). |

This setting controls how the API authenticates **to HCP** on behalf of the user. It does not affect how clients authenticate to this API (which always uses JWT bearer tokens).

## Code examples

### Login and use the token

```bash
# Login (system-level)
curl -X POST http://localhost:8000/api/v1/auth/token \
  -d "username=admin&password=mypassword"

# Login (tenant-scoped, using slash notation)
curl -X POST http://localhost:8000/api/v1/auth/token \
  -d "username=dev-ai/admin&password=mypassword"

# Login (tenant-scoped, using explicit tenant field)
curl -X POST http://localhost:8000/api/v1/auth/token \
  -d "username=admin&password=mypassword&tenant=dev-ai"
```

```bash
# Use the token for authenticated requests
TOKEN="eyJhbGciOiJIUzI1NiIs..."

# List S3 buckets
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/buckets

# List tenants (system admin)
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/mapi/tenants
```

### Swagger UI

The interactive documentation at `/docs` includes an **Authorize** button (lock icon, top-right). Enter your credentials there to authenticate all try-it-out requests:

- **Username:** `admin` (system-level) or `dev-ai/admin` (tenant-scoped)
- **Password:** your HCP password

## Error responses

| Status | Meaning |
|--------|---------|
| `401 Unauthorized` | Missing, expired, or invalid JWT token. |
| `422 Unprocessable Content` | Invalid tenant name format (must be alphanumeric, may contain hyphens). |

!!! warning "Login always succeeds"
    The login endpoint does **not** validate credentials against HCP -- it always returns a token. If the credentials are wrong, subsequent API calls will fail with `401` or `403` errors from HCP. This is by design: the API acts as a pass-through proxy, and HCP is the sole authority for credential validation.
