# Namespaces API

Namespace endpoints manage storage namespaces within an HCP tenant. Each namespace is a logical container for objects with its own configuration for protocols, compliance, indexing, and access control.

All endpoints require JWT authentication. See [Authentication](authentication.md) for details.

!!! tip "rahcp SDK and CLI"
    ```python
    ns = await client.mapi.list_namespaces("tenant", verbose=True)
    await client.mapi.create_namespace("tenant", {"name": "new-ns", "hardQuota": "100 GB"})
    template = await client.mapi.export_namespace("tenant", "my-ns")
    ```
    ```bash
    rahcp ns list dev-ai --verbose
    rahcp ns export dev-ai my-ns > template.json
    rahcp ns import dev-ai template.json
    ```
    See the [Python SDK](../sdk/index.md) for full documentation.

---

## Namespace Management

**Base path:** `/api/v1/mapi/tenants/{tenant_name}/namespaces`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../namespaces` | List all namespaces in the tenant. Add `verbose=true` for full details. |
| `PUT` | `.../namespaces` | Create a new namespace. Body: [NamespaceCreate](#namespacecreate). |
| `GET` | `.../namespaces/{ns_name}` | Get namespace details. |
| `HEAD` | `.../namespaces/{ns_name}` | Check whether a namespace exists. Returns `200` or `404`. |
| `POST` | `.../namespaces/{ns_name}` | Update namespace settings. Body: [NamespaceUpdate](#namespaceupdate). |
| `DELETE` | `.../namespaces/{ns_name}` | Delete a namespace. |

### NamespaceCreate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Namespace name (must be unique within the tenant) |
| `description` | string | No | Human-readable description |
| `hardQuota` | string | No | Maximum storage capacity (e.g., `"50 GB"`) |
| `softQuota` | integer | No | Soft quota percentage (0--100) |
| `hashScheme` | string | No | Object hashing scheme |
| `optimizedFor` | string | No | Optimization target (e.g., `cloud`, `default`) |
| `versioningSettings` | object | No | Versioning configuration |
| `tags` | object | No | Custom tags |

### NamespaceUpdate

All fields are optional: `description`, `hardQuota`, `softQuota`, `tags`, `versioningSettings`, and more.

---

## Namespace Templates

Export namespace configurations as reusable templates.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../namespaces/{ns_name}/export` | Export a single namespace as a template |
| `GET` | `.../namespaces/export?names=ns1,ns2` | Export multiple namespaces as a template bundle |

---

## Versioning

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../namespaces/{ns_name}/versioningSettings` | Get versioning configuration |
| `POST` | `.../namespaces/{ns_name}/versioningSettings` | Update versioning configuration |

---

## Compliance

Manage compliance settings and retention classes. Requires the `COMPLIANCE` role.

**Base path:** `/api/v1/mapi/tenants/{tenant_name}/namespaces/{ns_name}`

### Compliance Settings

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../complianceSettings` | Get namespace compliance configuration |
| `POST` | `.../complianceSettings` | Update namespace compliance configuration |

### Retention Classes

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../retentionClasses` | List all retention classes |
| `PUT` | `.../retentionClasses` | Create a new retention class |
| `GET` | `.../retentionClasses/{class_name}` | Get retention class details |
| `HEAD` | `.../retentionClasses/{class_name}` | Check whether a retention class exists |
| `POST` | `.../retentionClasses/{class_name}` | Update a retention class |
| `DELETE` | `.../retentionClasses/{class_name}` | Delete a retention class |

### RetentionClassCreate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Retention class name |
| `value` | string | Yes | Retention period (e.g., `"A+7y"` for 7 years after ingest) |
| `description` | string | No | Human-readable description |
| `allowDeletion` | boolean | No | Allow deletion before retention expires |

---

## Access & Protocols

Manage namespace permissions and protocol configurations. Requires the `ADMINISTRATOR` role.

### Permissions

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../permissions` | Get namespace permissions |
| `POST` | `.../permissions` | Update namespace permissions |

### Protocol Configuration

Each protocol has its own get/set endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../protocols/httpConfig` | Get HTTP/REST protocol settings |
| `POST` | `.../protocols/httpConfig` | Update HTTP/REST protocol settings |
| `GET` | `.../protocols/nfsConfig` | Get NFS protocol settings |
| `POST` | `.../protocols/nfsConfig` | Update NFS protocol settings |
| `GET` | `.../protocols/cifsConfig` | Get CIFS/SMB protocol settings |
| `POST` | `.../protocols/cifsConfig` | Update CIFS/SMB protocol settings |
| `GET` | `.../protocols/smtpConfig` | Get SMTP protocol settings |
| `POST` | `.../protocols/smtpConfig` | Update SMTP protocol settings |

### CORS

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../cors` | Get namespace CORS configuration |
| `PUT` | `.../cors` | Set namespace CORS configuration |
| `DELETE` | `.../cors` | Remove namespace CORS configuration |

### Replication Collision Settings

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../replicationCollisionSettings` | Get collision handling settings |
| `POST` | `.../replicationCollisionSettings` | Update collision handling settings |

---

## Indexing

Manage custom metadata indexing. Requires the `ADMINISTRATOR` role.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../cifsConfig` | Get CIFS config (includes indexing settings) |
| `POST` | `.../cifsConfig` | Update CIFS config and indexing settings |

---

## Namespace Statistics

View namespace resource usage and chargeback data. Requires the `MONITOR` role.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `.../statistics` | Get namespace statistics |
| `GET` | `.../chargebackReport` | Get namespace chargeback report |

### Chargeback Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `start` | string | -- | Start time (ISO 8601) |
| `end` | string | -- | End time (ISO 8601) |
| `granularity` | string | `total` | Report granularity: `hour`, `day`, or `total` |

---

## Code Examples

### curl

```bash
BASE="http://localhost:8000/api/v1"
TOKEN="<your-token>"
AUTH="Authorization: Bearer $TOKEN"
TENANT="research"

# ── Namespace management ─────────────────────────────────────

# List namespaces
curl -s "$BASE/mapi/tenants/$TENANT/namespaces?verbose=true" \
  -H "$AUTH" | jq .

# Create a namespace
curl -s -X PUT "$BASE/mapi/tenants/$TENANT/namespaces" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "datasets",
    "description": "ML training datasets",
    "hardQuota": "200 GB",
    "softQuota": 90
  }' | jq .

# Get namespace details
curl -s "$BASE/mapi/tenants/$TENANT/namespaces/datasets?verbose=true" \
  -H "$AUTH" | jq .

# Delete a namespace
curl -s -X DELETE "$BASE/mapi/tenants/$TENANT/namespaces/datasets" \
  -H "$AUTH" | jq .

# ── Templates ────────────────────────────────────────────────

# Export single namespace template
curl -s "$BASE/mapi/tenants/$TENANT/namespaces/datasets/export" \
  -H "$AUTH" | jq . > datasets-template.json

# Export multiple namespaces
curl -s "$BASE/mapi/tenants/$TENANT/namespaces/export?names=datasets,archives" \
  -H "$AUTH" | jq . > bundle.json

# ── Compliance & retention ───────────────────────────────────

# Get compliance settings
curl -s "$BASE/mapi/tenants/$TENANT/namespaces/datasets/complianceSettings" \
  -H "$AUTH" | jq .

# Update compliance settings
curl -s -X POST "$BASE/mapi/tenants/$TENANT/namespaces/datasets/complianceSettings" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}' | jq .

# Create a retention class (7-year hold)
curl -s -X PUT "$BASE/mapi/tenants/$TENANT/namespaces/datasets/retentionClasses" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "legal-hold-7y",
    "value": "A+7y",
    "description": "Seven-year legal retention"
  }' | jq .

# List retention classes
curl -s "$BASE/mapi/tenants/$TENANT/namespaces/datasets/retentionClasses?verbose=true" \
  -H "$AUTH" | jq .

# ── Statistics ───────────────────────────────────────────────

# Namespace statistics
curl -s "$BASE/mapi/tenants/$TENANT/namespaces/datasets/statistics" \
  -H "$AUTH" | jq .

# Namespace chargeback
curl -s "$BASE/mapi/tenants/$TENANT/namespaces/datasets/chargebackReport?\
start=2025-01-01T00:00:00Z&end=2025-02-01T00:00:00Z&granularity=day" \
  -H "$AUTH" | jq .
```

### Python

```python
import httpx

BASE = "http://localhost:8000/api/v1"

async def namespace_examples(token: str):
    headers = {"Authorization": f"Bearer {token}"}
    tenant = "research"

    async with httpx.AsyncClient(base_url=BASE, headers=headers) as c:
        # Create namespace
        resp = await c.put(
            f"/mapi/tenants/{tenant}/namespaces",
            json={
                "name": "datasets",
                "description": "ML training datasets",
                "hardQuota": "200 GB",
                "softQuota": 90,
            },
        )
        resp.raise_for_status()
        print("Created:", resp.json())

        # Export template
        resp = await c.get(
            f"/mapi/tenants/{tenant}/namespaces/datasets/export"
        )
        template = resp.json()
        print("Template keys:", list(template.keys()))

        # Update compliance
        resp = await c.post(
            f"/mapi/tenants/{tenant}/namespaces/datasets/complianceSettings",
            json={"enabled": True},
        )
        resp.raise_for_status()

        # Create retention class
        resp = await c.put(
            f"/mapi/tenants/{tenant}/namespaces/datasets/retentionClasses",
            json={
                "name": "legal-hold-7y",
                "value": "A+7y",
                "description": "Seven-year legal retention",
            },
        )
        resp.raise_for_status()
        print("Retention class created:", resp.json())

        # Namespace statistics
        resp = await c.get(
            f"/mapi/tenants/{tenant}/namespaces/datasets/statistics"
        )
        print("Stats:", resp.json())
```
