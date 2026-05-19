# Tenants API

All tenant endpoints require JWT authentication via the `Authorization: Bearer <token>` header.
See [Authentication](authentication.md) for details on obtaining tokens.

!!! tip "rahcp SDK and CLI"
    ```python
    async with HCPClient.from_env() as client:
        namespaces = await client.mapi.list_namespaces("dev-ai", verbose=True)
        await client.mapi.create_namespace("dev-ai", {"name": "new-ns", "hardQuota": "100 GB"})
    ```
    ```bash
    rahcp ns list dev-ai --verbose
    rahcp ns create dev-ai --name new-ns --quota "100 GB"
    ```
    See the [Python SDK](../sdk/index.md) for full documentation.

## Roles

Tenant-level user accounts are assigned one or more roles that control access to management operations:

| Role | Description |
|------|-------------|
| `ADMINISTRATOR` | Full tenant administration including user, namespace, and settings management |
| `SECURITY` | Manage console security, search security, and user account settings |
| `MONITOR` | Read-only access to statistics, chargeback reports, and tenant configuration |
| `COMPLIANCE` | Manage compliance settings, retention classes, and content classes |

---

## Tenant Management

System-level endpoints for creating and managing tenants. Requires system-level administrator credentials.

**Base path:** `/api/v1/mapi/tenants`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/mapi/tenants` | List all tenants. Add `verbose=true` for full details. |
| `PUT` | `/api/v1/mapi/tenants` | Create a new tenant. Body: [TenantCreate](#tenantcreate). Query params: `username`, `password`, `forcePasswordChange`, `initialSecurityGroup`. |
| `GET` | `/api/v1/mapi/tenants/{tenant_name}` | Get tenant details. |
| `HEAD` | `/api/v1/mapi/tenants/{tenant_name}` | Check whether a tenant exists. Returns `200` or `404`. |
| `POST` | `/api/v1/mapi/tenants/{tenant_name}` | Update tenant settings. Body: [TenantUpdate](#tenantupdate). |

### Query Parameters

All list endpoints support standard query parameters:

| Parameter | Type | Description |
|-----------|------|-------------|
| `offset` | integer | Pagination offset (zero-based) |
| `count` | integer | Maximum number of results to return |
| `sortType` | string | Field to sort by |
| `sortOrder` | string | Sort direction (`asc` or `desc`) |
| `filterType` | string | Field to filter on |
| `filterString` | string | Filter value |
| `verbose` | boolean | Include full details in response (default: `false`) |

### TenantCreate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Tenant name (must be unique) |
| `systemVisibleDescription` | string | No | Description visible to system administrators |
| `tenantVisibleDescription` | string | No | Description visible to tenant administrators |
| `hardQuota` | string | No | Maximum storage capacity (e.g., `"100 GB"`) |
| `softQuota` | integer | No | Soft quota percentage (0--100) |
| `namespaceQuota` | string | No | Maximum number of namespaces |
| `authenticationTypes` | object | No | Enabled authentication types |
| `complianceConfigurationEnabled` | boolean | No | Allow compliance configuration |
| `versioningConfigurationEnabled` | boolean | No | Allow versioning configuration |
| `searchConfigurationEnabled` | boolean | No | Allow search configuration |
| `replicationConfigurationEnabled` | boolean | No | Allow replication configuration |
| `tags` | object | No | Custom tags for the tenant |

### TenantUpdate

| Field | Type | Description |
|-------|------|-------------|
| `administrationAllowed` | boolean | Whether tenant administration is allowed |
| `maxNamespacesPerUser` | integer | Maximum namespaces a user can own |
| `tenantVisibleDescription` | string | Tenant-visible description |
| `tags` | object | Custom tags |

---

## Tenant Settings

Configuration endpoints for tenant-level administrators. Use `GET` to read current settings and `POST` to update.

**Base path:** `/api/v1/mapi/tenants/{tenant_name}`

### Console Security

Manage password policies, account lockout rules, and IP access restrictions.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../consoleSecurity` | Get current console security settings |
| `POST` | `.../consoleSecurity` | Update console security settings |

Key fields: `minimumPasswordLength`, `disableAfterAttempts`, `forcePasswordChangeDays`, `logoutOnInactive`, `loginMessage`, `ipSettings`.

### Contact Information

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../contactInfo` | Get tenant contact information |
| `POST` | `.../contactInfo` | Update tenant contact information |

Fields: `firstName`, `lastName`, `emailAddress`, `primaryPhone`, `address1`, `city`, `state`, `zipOrPostalCode`, `countryOrRegion`.

### Email Notification

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../emailNotification` | Get email notification settings |
| `POST` | `.../emailNotification` | Update email notification settings |

Fields: `enabled`, `emailTemplate` (with `from`, `subject`, `body`), `recipients` (with `address`, `importance`, `severity`).

### Namespace Defaults

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../namespaceDefaults` | Get default namespace creation settings |
| `POST` | `.../namespaceDefaults` | Update default namespace creation settings |

### Search Security

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../searchSecurity` | Get search console IP restrictions |
| `POST` | `.../searchSecurity` | Update search console IP restrictions |

### Service Plans

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../availableServicePlans` | List service plans available to this tenant (read-only) |
| `GET` | `.../availableServicePlans/{plan_name}` | Get details for a specific service plan |

### Permissions

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../permissions` | Get tenant-level permissions |
| `POST` | `.../permissions` | Update tenant-level permissions |

### CORS Configuration

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../cors` | Get tenant CORS configuration |
| `PUT` | `.../cors` | Set tenant CORS configuration |
| `DELETE` | `.../cors` | Remove tenant CORS configuration |

---

## Tenant Statistics

Monitoring endpoints for tenant resource usage and chargeback data. Requires the `MONITOR` role.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../statistics` | Get tenant storage statistics (object count, storage used, etc.) |
| `GET` | `.../chargebackReport` | Get chargeback usage report |

### Chargeback Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `start` | string | -- | Start time (ISO 8601 format) |
| `end` | string | -- | End time (ISO 8601 format) |
| `granularity` | string | `total` | Report granularity: `hour`, `day`, or `total` |

---

## User Accounts

Manage tenant-level user accounts for console and API access.

**Base path:** `/api/v1/mapi/tenants/{tenant_name}/userAccounts`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../userAccounts` | List all user accounts in the tenant |
| `PUT` | `.../userAccounts` | Create a new user account |
| `GET` | `.../userAccounts/{username}` | Get user account details |
| `HEAD` | `.../userAccounts/{username}` | Check whether a user account exists |
| `POST` | `.../userAccounts/{username}` | Update a user account |
| `DELETE` | `.../userAccounts/{username}` | Delete a user account |
| `POST` | `.../userAccounts/{username}/changePassword` | Change user password |
| `GET` | `.../userAccounts/{username}/dataAccessPermissions` | Get user data access permissions |
| `POST` | `.../userAccounts/{username}/dataAccessPermissions` | Update user data access permissions |

### UserAccountCreate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `username` | string | Yes | Unique username |
| `fullName` | string | Yes | User's full name |
| `localAuthentication` | boolean | Yes | Enable local (password) authentication |
| `enabled` | boolean | Yes | Whether the account is active |
| `forcePasswordChange` | boolean | Yes | Require password change on first login |
| `description` | string | No | Account description |
| `roles` | object | No | Assigned roles (e.g., `{"role": ["ADMINISTRATOR"]}`) |
| `allowNamespaceManagement` | boolean | No | Allow user to create and manage namespaces |

### UpdatePasswordRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `newPassword` | string | Yes | The new password |
| `oldPassword` | string | No | The current password (required for non-admin callers) |

---

## Group Accounts

Manage tenant-level group accounts, typically mapped to external directory groups.

**Base path:** `/api/v1/mapi/tenants/{tenant_name}/groupAccounts`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../groupAccounts` | List all group accounts in the tenant |
| `PUT` | `.../groupAccounts` | Create a new group account |
| `GET` | `.../groupAccounts/{groupname}` | Get group account details |
| `HEAD` | `.../groupAccounts/{groupname}` | Check whether a group account exists |
| `POST` | `.../groupAccounts/{groupname}` | Update a group account |
| `DELETE` | `.../groupAccounts/{groupname}` | Delete a group account |
| `GET` | `.../groupAccounts/{groupname}/dataAccessPermissions` | Get group data access permissions |
| `POST` | `.../groupAccounts/{groupname}/dataAccessPermissions` | Update group data access permissions |

---

## Content Classes

Define custom metadata schemas for object classification and indexing.

**Base path:** `/api/v1/mapi/tenants/{tenant_name}/contentClasses`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../contentClasses` | List all content classes in the tenant |
| `PUT` | `.../contentClasses` | Create a new content class |
| `GET` | `.../contentClasses/{name}` | Get content class details |
| `HEAD` | `.../contentClasses/{name}` | Check whether a content class exists |
| `POST` | `.../contentClasses/{name}` | Update a content class |
| `DELETE` | `.../contentClasses/{name}` | Delete a content class |

---

## Code Examples

### curl

```bash
BASE="http://localhost:8000/api/v1"
TOKEN="<your-token>"
AUTH="Authorization: Bearer $TOKEN"
TENANT="research"

# ── Tenant management (system admin) ──────────────────────────

# List all tenants (verbose)
curl -s "$BASE/mapi/tenants?verbose=true" -H "$AUTH" | jq .

# Create a tenant with initial admin user
curl -s -X PUT "$BASE/mapi/tenants?username=tenantadmin&password=Ch4ng3Me!" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "research",
    "systemVisibleDescription": "Research storage",
    "hardQuota": "500 GB",
    "softQuota": 80
  }' | jq .

# ── Tenant settings (tenant admin) ───────────────────────────

# Get tenant details
curl -s "$BASE/mapi/tenants/$TENANT" -H "$AUTH" | jq .

# Update tenant description
curl -s -X POST "$BASE/mapi/tenants/$TENANT" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"tenantVisibleDescription": "Updated description"}' | jq .

# ── User accounts ────────────────────────────────────────────

# List users
curl -s "$BASE/mapi/tenants/$TENANT/userAccounts?verbose=true" \
  -H "$AUTH" | jq .

# Create a user with MONITOR + COMPLIANCE roles
curl -s -X PUT "$BASE/mapi/tenants/$TENANT/userAccounts?password=InitP4ss!" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "auditor",
    "fullName": "Compliance Auditor",
    "localAuthentication": true,
    "enabled": true,
    "forcePasswordChange": true,
    "roles": {"role": ["MONITOR", "COMPLIANCE"]}
  }' | jq .

# Change user password
curl -s -X POST "$BASE/mapi/tenants/$TENANT/userAccounts/auditor/changePassword" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"newPassword": "N3wP4ssw0rd!"}' | jq .

# Delete a user
curl -s -X DELETE "$BASE/mapi/tenants/$TENANT/userAccounts/auditor" \
  -H "$AUTH" | jq .

# ── Group accounts ───────────────────────────────────────────

# List groups
curl -s "$BASE/mapi/tenants/$TENANT/groupAccounts" -H "$AUTH" | jq .

# Create a group
curl -s -X PUT "$BASE/mapi/tenants/$TENANT/groupAccounts" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{
    "groupname": "data-engineers",
    "roles": {"role": ["ADMINISTRATOR"]}
  }' | jq .

# Delete a group
curl -s -X DELETE "$BASE/mapi/tenants/$TENANT/groupAccounts/data-engineers" \
  -H "$AUTH" | jq .

# ── Statistics & chargeback ──────────────────────────────────

# Tenant statistics
curl -s "$BASE/mapi/tenants/$TENANT/statistics" -H "$AUTH" | jq .

# Chargeback report (daily, January 2025)
curl -s "$BASE/mapi/tenants/$TENANT/chargebackReport?\
start=2025-01-01T00:00:00Z&end=2025-02-01T00:00:00Z&granularity=day" \
  -H "$AUTH" | jq .
```

### Python

```python
import httpx

BASE = "http://localhost:8000/api/v1"

async def tenant_examples(token: str):
    headers = {"Authorization": f"Bearer {token}"}
    tenant = "research"

    async with httpx.AsyncClient(base_url=BASE, headers=headers) as c:
        # List tenants (system admin)
        resp = await c.get("/mapi/tenants", params={"verbose": True})
        print("Tenants:", [t["name"] for t in resp.json()])

        # Create tenant with admin user (system admin)
        resp = await c.put(
            "/mapi/tenants",
            params={"username": "tenantadmin", "password": "Ch4ng3Me!"},
            json={
                "name": "research",
                "systemVisibleDescription": "Research storage",
                "hardQuota": "500 GB",
                "softQuota": 80,
            },
        )
        resp.raise_for_status()

        # Create a user
        resp = await c.put(
            f"/mapi/tenants/{tenant}/userAccounts",
            params={"password": "InitP4ss!"},
            json={
                "username": "auditor",
                "fullName": "Compliance Auditor",
                "localAuthentication": True,
                "enabled": True,
                "forcePasswordChange": True,
                "roles": {"role": ["MONITOR", "COMPLIANCE"]},
            },
        )
        resp.raise_for_status()

        # Get statistics
        resp = await c.get(f"/mapi/tenants/{tenant}/statistics")
        print("Stats:", resp.json())

        # Chargeback report
        resp = await c.get(
            f"/mapi/tenants/{tenant}/chargebackReport",
            params={
                "start": "2025-01-01T00:00:00Z",
                "end": "2025-02-01T00:00:00Z",
                "granularity": "day",
            },
        )
        print("Chargeback entries:", resp.json())
```
