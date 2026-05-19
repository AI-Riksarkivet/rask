# API Workflows

Copy-paste-ready **curl**, **Python 3.13+ (`httpx`)**, and **[rahcp SDK](../sdk/index.md)** examples for the most common HCP API workflows. Every example assumes the API is running at `http://localhost:8000` -- adjust `BASE` to match your environment.

!!! tip "Use the rahcp SDK for scripts and applications"
    The `rahcp-client` SDK handles authentication, retries, presigned URLs, and multipart uploads automatically. Install it with `uv pip install rahcp` and see the [Python SDK](../sdk/index.md) page for full documentation.

The HCP Unified API exposes two endpoint families under the same JWT token:

| Family | Prefix | Purpose | Examples |
|--------|--------|---------|----------|
| **S3 data-plane** | `/buckets`, `/presign` | Object storage — upload, download, copy, delete, presigned URLs, multipart | Sections 3, 8, 9 |
| **MAPI management** | `/mapi/tenants/...` | Administration — tenants, namespaces, users, groups, statistics, compliance | Sections 2, 4, 5, 6 |
| **Query** | `/query/tenants/...` | Metadata search — find objects by custom metadata, audit operations | Section 7 |

All paths below are relative to `BASE` (`http://localhost:8000/api/v1`).

## Prerequisites

```bash
# One-off script -- uv handles the virtual environment automatically
uv run --with httpx my_script.py

# Or add httpx to an existing uv project
uv add httpx
uv run python my_script.py
```

All Python examples target **Python >= 3.13** and use **async** `httpx`. Run them with `asyncio.run()` or inside an async framework. A minimal entry point:

```python
# upload_report.py
import asyncio

async def main():
    ...  # paste any example here

asyncio.run(main())
```

```bash
uv run --python 3.13 --with httpx upload_report.py
```

---

## 1. Authentication helper

Obtain a JWT token once and reuse it for all subsequent requests.

=== "curl"

    ```bash
    # Variables -- set once per session
    BASE="http://localhost:8000/api/v1"

    # System-level login
    TOKEN=$(curl -s -X POST "$BASE/auth/token" \
      -d "username=<system-admin>&password=<password>" | jq -r .access_token)

    # Tenant-scoped login (slash notation)
    TOKEN=$(curl -s -X POST "$BASE/auth/token" \
      -d "username=<tenant>/<username>&password=<password>" | jq -r .access_token)

    # Tenant-scoped login (explicit tenant field)
    TOKEN=$(curl -s -X POST "$BASE/auth/token" \
      -d "username=<username>&password=<password>&tenant=<tenant>" | jq -r .access_token)

    # Verify the token works
    curl -s -H "Authorization: Bearer $TOKEN" "$BASE/buckets" | jq .
    ```

=== "Python"

    ```python
    import httpx

    BASE = "http://localhost:8000/api/v1"

    async def login(
        username: str,
        password: str,
        tenant: str | None = None,
    ) -> str:
        """Return a JWT access token."""
        data: dict[str, str] = {"username": username, "password": password}
        if tenant:
            data["tenant"] = tenant
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{BASE}/auth/token", data=data)
            resp.raise_for_status()
            return resp.json()["access_token"]

    def authed_client(token: str) -> httpx.AsyncClient:
        """Create a reusable client with the Authorization header."""
        return httpx.AsyncClient(
            base_url=BASE,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
    ```

=== "rahcp SDK"

    ```python
    from rahcp_client import HCPClient

    # System-level login
    client = HCPClient(
        endpoint="http://localhost:8000/api/v1",
        username="<system-admin>",
        password="<password>",
    )

    # Tenant-scoped login
    client = HCPClient(
        endpoint="http://localhost:8000/api/v1",
        username="<username>",
        password="<password>",
        tenant="<tenant>",
    )

    # From environment variables (HCP_ENDPOINT, HCP_USERNAME, etc.)
    client = HCPClient.from_env()

    # Use as async context manager (auto-authenticates)
    async with client:
        result = await client.s3.list_buckets()
        print(result)
    ```

!!! note "Token lifetime"
    Tokens expire after **8 hours** by default (configurable via `API_TOKEN_EXPIRE_MINUTES`). For long-running scripts, re-authenticate when you receive a `401` response. The rahcp SDK handles token refresh automatically.

---

## 2. Tenant provisioning

Create a tenant, add an administrator user, then create a namespace -- the typical day-one setup.

=== "curl"

    ```bash
    BASE="http://localhost:8000/api/v1"

    # 1. Login as system admin
    TOKEN=$(curl -s -X POST "$BASE/auth/token" \
      -d "username=<system-admin>&password=<password>" | jq -r .access_token)
    AUTH="Authorization: Bearer $TOKEN"

    # 2. Create a new tenant with initial admin user
    TENANT="<tenant>"
    curl -s -X PUT "$BASE/mapi/tenants?username=<tenant-admin>&password=<tenant-password>" \
      -H "$AUTH" \
      -H "Content-Type: application/json" \
      -d '{
        "name": "'"$TENANT"'",
        "systemVisibleDescription": "Department storage",
        "hardQuota": "500 GB",
        "softQuota": 80
      }' | jq .

    # 3. Login as the new tenant admin
    TENANT_TOKEN=$(curl -s -X POST "$BASE/auth/token" \
      -d "username=$TENANT/<tenant-admin>&password=<tenant-password>" | jq -r .access_token)
    TAUTH="Authorization: Bearer $TENANT_TOKEN"

    # 4. Create a namespace
    curl -s -X PUT "$BASE/mapi/tenants/$TENANT/namespaces" \
      -H "$TAUTH" \
      -H "Content-Type: application/json" \
      -d '{
        "name": "datasets",
        "description": "ML training datasets",
        "hardQuota": "200 GB",
        "softQuota": 90
      }' | jq .
    ```

=== "Python"

    ```python
    import httpx

    BASE = "http://localhost:8000/api/v1"

    async def provision_tenant():
        async with httpx.AsyncClient(base_url=BASE) as c:
            # Login as system admin
            resp = await c.post("/auth/token", data={
                "username": "<system-admin>", "password": "<password>",
            })
            token = resp.json()["access_token"]
            c.headers["Authorization"] = f"Bearer {token}"

            tenant = "<tenant>"

            # Create tenant with initial admin
            resp = await c.put(
                "/mapi/tenants",
                params={"username": "<tenant-admin>", "password": "<tenant-password>"},
                json={
                    "name": tenant,
                    "systemVisibleDescription": "Department storage",
                    "hardQuota": "500 GB",
                    "softQuota": 80,
                },
            )
            resp.raise_for_status()
            print("Tenant created:", resp.json())

            # Login as tenant admin
            resp = await c.post(
                "/auth/token",
                data={"username": f"{tenant}/<tenant-admin>", "password": "<tenant-password>"},
            )
            tenant_token = resp.json()["access_token"]
            c.headers["Authorization"] = f"Bearer {tenant_token}"

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
            print("Namespace created:", resp.json())
    ```

---

## 3. Object lifecycle

Upload, list, download, copy, and delete objects using the S3 endpoints.

=== "curl"

    ```bash
    BASE="http://localhost:8000/api/v1"
    TOKEN="<your-token>"
    AUTH="Authorization: Bearer $TOKEN"
    BUCKET="my-bucket"

    # Upload a file
    curl -s -X POST "$BASE/buckets/$BUCKET/objects/reports/q1.pdf" \
      -H "$AUTH" \
      -F "file=@/tmp/q1-report.pdf" | jq .

    # List objects with prefix
    curl -s "$BASE/buckets/$BUCKET/objects?prefix=reports/&max_keys=50" \
      -H "$AUTH" | jq .

    # Download a file
    curl -s "$BASE/buckets/$BUCKET/objects/reports/q1.pdf" \
      -H "$AUTH" -o q1-report.pdf

    # Copy to another bucket
    curl -s -X POST "$BASE/buckets/archive/objects/2025/q1.pdf/copy" \
      -H "$AUTH" \
      -H "Content-Type: application/json" \
      -d "{\"source_bucket\": \"$BUCKET\", \"source_key\": \"reports/q1.pdf\"}" | jq .

    # Delete the original
    curl -s -X DELETE "$BASE/buckets/$BUCKET/objects/reports/q1.pdf" \
      -H "$AUTH" | jq .
    ```

=== "Python"

    ```python
    import httpx
    from pathlib import Path

    BASE = "http://localhost:8000/api/v1"

    async def object_lifecycle(token: str):
        headers = {"Authorization": f"Bearer {token}"}
        bucket = "my-bucket"

        async with httpx.AsyncClient(base_url=BASE, headers=headers) as c:
            # Upload
            file_path = Path("/tmp/q1-report.pdf")
            with file_path.open("rb") as f:
                resp = await c.post(
                    f"/buckets/{bucket}/objects/reports/q1.pdf",
                    files={"file": (file_path.name, f, "application/pdf")},
                )
                resp.raise_for_status()
                print("Uploaded:", resp.json())

            # List
            resp = await c.get(
                f"/buckets/{bucket}/objects",
                params={"prefix": "reports/", "max_keys": 50},
            )
            for obj in resp.json()["objects"]:
                print(f"  {obj['key']}  ({obj['size']} bytes)")

            # Download
            resp = await c.get(f"/buckets/{bucket}/objects/reports/q1.pdf")
            Path("q1-report.pdf").write_bytes(resp.content)

            # Copy to archive bucket
            resp = await c.post(
                "/buckets/archive/objects/2025/q1.pdf/copy",
                json={"source_bucket": bucket, "source_key": "reports/q1.pdf"},
            )
            resp.raise_for_status()

            # Delete original
            resp = await c.delete(f"/buckets/{bucket}/objects/reports/q1.pdf")
            resp.raise_for_status()
            print("Deleted original")
    ```

=== "rahcp SDK"

    ```python
    from rahcp_client import HCPClient
    from pathlib import Path

    async with HCPClient.from_env() as client:
        bucket = "my-bucket"

        # Upload
        etag = await client.s3.upload(bucket, "reports/q1.pdf", Path("/tmp/q1-report.pdf"))
        print(f"Uploaded: {etag}")

        # List
        result = await client.s3.list_objects(bucket, prefix="reports/", max_keys=50)
        for obj in result["objects"]:
            print(f"  {obj['key']}  ({obj['size']} bytes)")

        # Download
        size = await client.s3.download(bucket, "reports/q1.pdf", Path("q1-report.pdf"))
        print(f"Downloaded {size} bytes")

        # Copy to archive bucket
        await client.s3.copy("archive", "2025/q1.pdf", bucket, "reports/q1.pdf")

        # Delete original
        await client.s3.delete(bucket, "reports/q1.pdf")
        print("Deleted original")
    ```

---

## 4. Namespace backup / export

Export namespace configuration as a reusable template -- useful for disaster recovery or cloning environments.

=== "curl"

    ```bash
    BASE="http://localhost:8000/api/v1"
    TOKEN="<your-token>"
    AUTH="Authorization: Bearer $TOKEN"
    TENANT="<tenant>"

    # Export a single namespace template
    curl -s "$BASE/mapi/tenants/$TENANT/namespaces/datasets/export" \
      -H "$AUTH" | jq . > datasets-template.json

    # Export multiple namespaces at once
    curl -s "$BASE/mapi/tenants/$TENANT/namespaces/export?names=datasets,archives" \
      -H "$AUTH" | jq . > namespace-bundle.json

    # Review the template
    cat datasets-template.json | jq '.name, .hardQuota, .complianceSettings'
    ```

=== "Python"

    ```python
    import httpx
    import json
    from pathlib import Path

    BASE = "http://localhost:8000/api/v1"

    async def export_namespaces(token: str, tenant: str, ns_names: list[str]):
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(base_url=BASE, headers=headers) as c:
            if len(ns_names) == 1:
                resp = await c.get(
                    f"/mapi/tenants/{tenant}/namespaces/{ns_names[0]}/export"
                )
            else:
                resp = await c.get(
                    f"/mapi/tenants/{tenant}/namespaces/export",
                    params={"names": ",".join(ns_names)},
                )

            resp.raise_for_status()
            template = resp.json()

            filename = f"{'-'.join(ns_names)}-template.json"
            Path(filename).write_text(json.dumps(template, indent=2))
            print(f"Exported to {filename}")
            return template
    ```

=== "rahcp SDK"

    ```python
    from rahcp_client import HCPClient
    import json
    from pathlib import Path

    async with HCPClient.from_env() as client:
        tenant = "<tenant>"

        # Export a single namespace
        template = await client.mapi.export_namespace(tenant, "datasets")
        Path("datasets-template.json").write_text(json.dumps(template, indent=2))

        # Export multiple namespaces
        bundle = await client.mapi.export_namespaces(tenant, ["datasets", "archives"])
        Path("namespace-bundle.json").write_text(json.dumps(bundle, indent=2))
    ```

---

## 5. User management

Create users, assign roles, and change passwords at the tenant level.

!!! warning "Example passwords"
    The passwords in these examples (`InitialP4ss!`, `N3wS3cure!`) are **for illustration only**. In production, use a strong password policy and never commit credentials to source control.

=== "curl"

    ```bash
    BASE="http://localhost:8000/api/v1"
    TOKEN="<your-tenant-admin-token>"
    AUTH="Authorization: Bearer $TOKEN"
    TENANT="<tenant>"

    # Create a user with MONITOR role
    curl -s -X PUT "$BASE/mapi/tenants/$TENANT/userAccounts?password=InitialP4ss!" \
      -H "$AUTH" \
      -H "Content-Type: application/json" \
      -d '{
        "username": "analyst",
        "fullName": "Data Analyst",
        "localAuthentication": true,
        "enabled": true,
        "forcePasswordChange": true,
        "description": "Read-only monitoring account",
        "roles": {"role": ["MONITOR"]}
      }' | jq .

    # List all users
    curl -s "$BASE/mapi/tenants/$TENANT/userAccounts?verbose=true" \
      -H "$AUTH" | jq .

    # Update user -- add COMPLIANCE role
    curl -s -X POST "$BASE/mapi/tenants/$TENANT/userAccounts/analyst" \
      -H "$AUTH" \
      -H "Content-Type: application/json" \
      -d '{
        "roles": {"role": ["MONITOR", "COMPLIANCE"]}
      }' | jq .

    # Change password
    curl -s -X POST "$BASE/mapi/tenants/$TENANT/userAccounts/analyst/changePassword" \
      -H "$AUTH" \
      -H "Content-Type: application/json" \
      -d '{"newPassword": "N3wS3cure!"}' | jq .

    # Delete a user
    curl -s -X DELETE "$BASE/mapi/tenants/$TENANT/userAccounts/analyst" \
      -H "$AUTH" | jq .
    ```

=== "Python"

    ```python
    import httpx

    BASE = "http://localhost:8000/api/v1"

    async def manage_users(token: str, tenant: str):
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(base_url=BASE, headers=headers) as c:
            # Create user
            resp = await c.put(
                f"/mapi/tenants/{tenant}/userAccounts",
                params={"password": "InitialP4ss!"},
                json={
                    "username": "analyst",
                    "fullName": "Data Analyst",
                    "localAuthentication": True,
                    "enabled": True,
                    "forcePasswordChange": True,
                    "description": "Read-only monitoring account",
                    "roles": {"role": ["MONITOR"]},
                },
            )
            resp.raise_for_status()
            print("User created:", resp.json())

            # List users
            resp = await c.get(
                f"/mapi/tenants/{tenant}/userAccounts",
                params={"verbose": True},
            )
            for user in resp.json():
                print(f"  {user['username']} -- roles: {user.get('roles', {})}")

            # Update roles
            resp = await c.post(
                f"/mapi/tenants/{tenant}/userAccounts/analyst",
                json={"roles": {"role": ["MONITOR", "COMPLIANCE"]}},
            )
            resp.raise_for_status()

            # Change password
            resp = await c.post(
                f"/mapi/tenants/{tenant}/userAccounts/analyst/changePassword",
                json={"newPassword": "N3wS3cure!"},
            )
            resp.raise_for_status()
            print("Password changed")

            # Delete a user
            resp = await c.delete(
                f"/mapi/tenants/{tenant}/userAccounts/analyst",
            )
            resp.raise_for_status()
            print("User deleted")
    ```

---

## 6. Monitoring and chargeback

Fetch storage statistics and pull chargeback reports for billing or capacity planning.

=== "curl"

    ```bash
    BASE="http://localhost:8000/api/v1"
    TOKEN="<your-monitor-token>"
    AUTH="Authorization: Bearer $TOKEN"
    TENANT="<tenant>"

    # Tenant-level statistics
    curl -s "$BASE/mapi/tenants/$TENANT/statistics" \
      -H "$AUTH" | jq .

    # Namespace-level statistics
    curl -s "$BASE/mapi/tenants/$TENANT/namespaces/datasets/statistics" \
      -H "$AUTH" | jq .

    # Chargeback report for January 2025 (daily granularity)
    curl -s "$BASE/mapi/tenants/$TENANT/chargebackReport?\
    start=2025-01-01T00:00:00Z&end=2025-02-01T00:00:00Z&granularity=day" \
      -H "$AUTH" | jq .

    # Namespace-level chargeback
    curl -s "$BASE/mapi/tenants/$TENANT/namespaces/datasets/chargebackReport?\
    start=2025-01-01T00:00:00Z&end=2025-02-01T00:00:00Z&granularity=total" \
      -H "$AUTH" | jq .
    ```

=== "Python"

    ```python
    import httpx

    BASE = "http://localhost:8000/api/v1"

    async def monitoring(token: str, tenant: str, namespace: str):
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(base_url=BASE, headers=headers) as c:
            # Tenant statistics
            resp = await c.get(f"/mapi/tenants/{tenant}/statistics")
            stats = resp.json()
            print(f"Tenant storage used: {stats.get('storageCapacityUsed', 'N/A')}")
            print(f"Object count: {stats.get('objectCount', 'N/A')}")

            # Namespace statistics
            resp = await c.get(
                f"/mapi/tenants/{tenant}/namespaces/{namespace}/statistics"
            )
            ns_stats = resp.json()
            print(f"Namespace '{namespace}': {ns_stats}")

            # Chargeback report (daily for January 2025)
            resp = await c.get(
                f"/mapi/tenants/{tenant}/chargebackReport",
                params={
                    "start": "2025-01-01T00:00:00Z",
                    "end": "2025-02-01T00:00:00Z",
                    "granularity": "day",
                },
            )
            report = resp.json()
            print(f"Chargeback entries: {len(report) if isinstance(report, list) else 'N/A'}")
            return report
    ```

---

## 7. Metadata query

Search objects by indexed metadata and audit operations across namespaces.

!!! note
    Metadata query endpoints use the `/api/v1/query` prefix (not `/api/v1/mapi`). The target namespace must have indexing enabled.

!!! tip "Query syntax"
    The `query` field uses **Lucene syntax**: `field:value`, `AND`/`OR` operators, range queries (`size:[1048576 TO *]`), and wildcards (`contentType:application/*`). Enclose exact phrases in escaped quotes (`\"engineering\"`).

=== "curl"

    ```bash
    BASE="http://localhost:8000/api/v1"
    TOKEN="<your-token>"
    AUTH="Authorization: Bearer $TOKEN"
    TENANT="<tenant>"

    # Search for large files (>1 MB) in the datasets namespace
    curl -s -X POST "$BASE/query/tenants/$TENANT/objects" \
      -H "$AUTH" \
      -H "Content-Type: application/json" \
      -d '{
        "query": "namespace:datasets AND size:[1048576 TO *]",
        "count": 50,
        "offset": 0,
        "sort": "-changeTimeMilliseconds",
        "verbose": true,
        "objectProperties": ["urlName", "size", "contentType", "changeTimeString"]
      }' | jq .

    # Search by custom metadata
    curl -s -X POST "$BASE/query/tenants/$TENANT/objects" \
      -H "$AUTH" \
      -H "Content-Type: application/json" \
      -d '{
        "query": "customMetadata.department:\"engineering\" AND contentType:application/pdf",
        "count": 100,
        "verbose": true
      }' | jq .

    # Audit operations -- all deletes in the last 24 hours
    curl -s -X POST "$BASE/query/tenants/$TENANT/operations" \
      -H "$AUTH" \
      -H "Content-Type: application/json" \
      -d '{
        "count": 100,
        "verbose": true,
        "systemMetadata": {
          "changeTime": {
            "start": "2025-01-01T00:00:00Z",
            "end": "2025-01-02T00:00:00Z"
          },
          "transactions": {
            "transaction": ["delete", "purge"]
          }
        }
      }' | jq .
    ```

=== "Python"

    ```python
    import httpx

    BASE = "http://localhost:8000/api/v1"

    async def search_objects(token: str, tenant: str):
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(base_url=BASE, headers=headers) as c:
            # Search for large files
            resp = await c.post(
                f"/query/tenants/{tenant}/objects",
                json={
                    "query": "namespace:datasets AND size:[1048576 TO *]",
                    "count": 50,
                    "sort": "-changeTimeMilliseconds",
                    "verbose": True,
                    "objectProperties": [
                        "urlName", "size", "contentType", "changeTimeString",
                    ],
                },
            )
            resp.raise_for_status()
            results = resp.json()
            for obj in results.get("resultSet", []):
                print(f"  {obj.get('urlName')}  ({obj.get('size')} bytes)")

    async def audit_deletes(token: str, tenant: str, start: str, end: str):
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(base_url=BASE, headers=headers) as c:
            resp = await c.post(
                f"/query/tenants/{tenant}/operations",
                json={
                    "count": 100,
                    "verbose": True,
                    "systemMetadata": {
                        "changeTime": {"start": start, "end": end},
                        "transactions": {"transaction": ["delete", "purge"]},
                    },
                },
            )
            resp.raise_for_status()
            ops = resp.json()
            print(f"Found {len(ops.get('resultSet', []))} delete/purge operations")
            return ops
    ```

---

## 8. Bulk operations

Delete, presign, or download multiple objects in a single request.

=== "curl"

    ```bash
    BASE="http://localhost:8000/api/v1"
    TOKEN="<your-token>"
    AUTH="Authorization: Bearer $TOKEN"
    BUCKET="my-bucket"

    # Bulk delete
    curl -s -X POST "$BASE/buckets/$BUCKET/objects/delete" \
      -H "$AUTH" \
      -H "Content-Type: application/json" \
      -d '{"keys": ["temp/file1.txt", "temp/file2.txt", "temp/file3.txt"]}' | jq .

    # Bulk presigned URLs (for sharing download links)
    curl -s -X POST "$BASE/buckets/$BUCKET/objects/presign" \
      -H "$AUTH" \
      -H "Content-Type: application/json" \
      -d '{
        "keys": ["reports/q1.pdf", "reports/q2.pdf"],
        "expires_in": 7200
      }' | jq .

    # Bulk download as ZIP
    curl -s -X POST "$BASE/buckets/$BUCKET/objects/download" \
      -H "$AUTH" \
      -H "Content-Type: application/json" \
      -d '{"keys": ["reports/q1.pdf", "reports/q2.pdf", "reports/q3.pdf"]}' \
      -o reports.zip
    ```

=== "Python"

    ```python
    import httpx
    from pathlib import Path

    BASE = "http://localhost:8000/api/v1"

    async def bulk_operations(token: str, bucket: str):
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(base_url=BASE, headers=headers) as c:
            # Bulk delete
            resp = await c.post(
                f"/buckets/{bucket}/objects/delete",
                json={"keys": ["temp/file1.txt", "temp/file2.txt"]},
            )
            resp.raise_for_status()
            result = resp.json()
            print(f"Deleted: {result.get('deleted', [])}")
            if result.get("errors"):
                print(f"Errors: {result['errors']}")

            # Bulk presigned URLs
            resp = await c.post(
                f"/buckets/{bucket}/objects/presign",
                json={
                    "keys": ["reports/q1.pdf", "reports/q2.pdf"],
                    "expires_in": 7200,
                },
            )
            resp.raise_for_status()
            for item in resp.json().get("urls", []):
                print(f"  {item['key']}: {item['url']}")

            # Bulk download as ZIP
            resp = await c.post(
                f"/buckets/{bucket}/objects/download",
                json={"keys": ["reports/q1.pdf", "reports/q2.pdf"]},
            )
            resp.raise_for_status()
            Path("reports.zip").write_bytes(resp.content)
            print("Downloaded reports.zip")
    ```

=== "rahcp SDK"

    ```python
    from rahcp_client import HCPClient

    async with HCPClient.from_env() as client:
        bucket = "my-bucket"

        # Bulk delete
        result = await client.s3.delete_bulk(bucket, [
            "temp/file1.txt", "temp/file2.txt", "temp/file3.txt",
        ])
        print(f"Deleted: {result}")

        # Bulk presigned URLs
        urls = await client.s3.presign_bulk(bucket, [
            "reports/q1.pdf", "reports/q2.pdf",
        ], expires=7200)
        for item in urls:
            print(f"  {item['key']}: {item['url']}")
    ```

---

## 9. Multipart upload

Upload large files using presigned multipart upload -- the recommended approach for files >= 100 MB.

=== "curl"

    ```bash
    BASE="http://localhost:8000/api/v1"
    TOKEN="<your-token>"
    AUTH="Authorization: Bearer $TOKEN"
    BUCKET="my-bucket"
    KEY="large-dataset.tar.gz"
    FILE="/tmp/large-dataset.tar.gz"
    FILE_SIZE=$(stat -f%z "$FILE" 2>/dev/null || stat -c%s "$FILE")

    # 1. Get presigned URLs
    PRESIGN=$(curl -s -X POST "$BASE/buckets/$BUCKET/multipart/$KEY/presign" \
      -H "$AUTH" \
      -H "Content-Type: application/json" \
      -d "{\"file_size\": $FILE_SIZE}")

    UPLOAD_ID=$(echo "$PRESIGN" | jq -r .upload_id)
    TOTAL_PARTS=$(echo "$PRESIGN" | jq -r .total_parts)
    PART_SIZE=$(echo "$PRESIGN" | jq -r .part_size)

    echo "Upload ID: $UPLOAD_ID, Parts: $TOTAL_PARTS, Part size: $PART_SIZE"

    # 2. Upload each part directly to HCP (no auth needed -- URL is presigned)
    PARTS="[]"
    for i in $(seq 0 $((TOTAL_PARTS - 1))); do
      PART_NUM=$((i + 1))
      URL=$(echo "$PRESIGN" | jq -r ".urls[$i].url")
      SKIP=$((i * PART_SIZE))

      ETAG=$(dd if="$FILE" bs=$PART_SIZE skip=$i count=1 2>/dev/null | \
        curl -s -X PUT "$URL" --data-binary @- -D - -o /dev/null | \
        grep -i etag | tr -d '\r' | awk '{print $2}')

      PARTS=$(echo "$PARTS" | jq ". + [{\"PartNumber\": $PART_NUM, \"ETag\": $ETAG}]")
      echo "  Part $PART_NUM/$TOTAL_PARTS uploaded (ETag: $ETAG)"
    done

    # 3. Complete the upload
    curl -s -X POST "$BASE/buckets/$BUCKET/multipart/$KEY/complete" \
      -H "$AUTH" \
      -H "Content-Type: application/json" \
      -d "{\"upload_id\": \"$UPLOAD_ID\", \"parts\": $PARTS}" | jq .
    ```

=== "Python"

    ```python
    import asyncio
    import httpx
    from pathlib import Path

    BASE = "http://localhost:8000/api/v1"

    async def multipart_upload(
        token: str,
        bucket: str,
        key: str,
        file_path: str,
        concurrency: int = 6,
    ):
        """Upload a large file using presigned multipart upload."""
        headers = {"Authorization": f"Bearer {token}"}
        path = Path(file_path)
        file_size = path.stat().st_size

        async with httpx.AsyncClient(base_url=BASE, headers=headers) as c:
            # 1. Get presigned URLs
            resp = await c.post(
                f"/buckets/{bucket}/multipart/{key}/presign",
                json={"file_size": file_size},
            )
            resp.raise_for_status()
            presign = resp.json()
            upload_id = presign["upload_id"]
            part_size = presign["part_size"]
            urls = presign["urls"]
            print(f"Uploading {file_size} bytes in {len(urls)} parts")

        # 2. Upload parts directly to HCP (presigned -- no auth header)
        data = path.read_bytes()
        semaphore = asyncio.Semaphore(concurrency)

        async def upload_part(part_info: dict) -> dict:
            async with semaphore:
                pn = part_info["part_number"]
                url = part_info["url"]
                start = (pn - 1) * part_size
                end = min(start + part_size, file_size)
                chunk = data[start:end]

                async with httpx.AsyncClient() as hcp:
                    resp = await hcp.put(url, content=chunk)
                    resp.raise_for_status()
                    etag = resp.headers["etag"]
                    print(f"  Part {pn}/{len(urls)} uploaded")
                    return {"PartNumber": pn, "ETag": etag}

        parts = await asyncio.gather(*[upload_part(u) for u in urls])
        parts = sorted(parts, key=lambda p: p["PartNumber"])

        # 3. Complete the upload
        async with httpx.AsyncClient(base_url=BASE, headers=headers) as c:
            resp = await c.post(
                f"/buckets/{bucket}/multipart/{key}/complete",
                json={"upload_id": upload_id, "parts": parts},
            )
            resp.raise_for_status()
            print("Upload complete:", resp.json())
    ```

=== "rahcp SDK"

    ```python
    from rahcp_client import HCPClient
    from pathlib import Path

    async with HCPClient.from_env() as client:
        # upload() auto-selects multipart for files > 64 MB
        etag = await client.s3.upload(
            "my-bucket",
            "large-dataset.tar.gz",
            Path("/tmp/large-dataset.tar.gz"),
        )
        print(f"Uploaded: {etag}")

        # Or explicitly use multipart with custom concurrency
        etag = await client.s3.upload_multipart(
            "my-bucket",
            "large-dataset.tar.gz",
            Path("/tmp/large-dataset.tar.gz"),
            concurrency=8,
        )
    ```

!!! warning "CORS required for browser uploads"
    If calling presigned URLs from a browser, the target HCP namespace must have CORS configured to allow `PUT` requests and expose the `ETag` header. See [S3 Objects -- CORS Configuration](s3-objects.md#cors-configuration-for-presigned-uploads) for details.

---

## Related pages

- [Python SDK](../sdk/index.md) -- `rahcp-client` async client with automatic retries, presigned URLs, and multipart uploads.
- [Authentication](authentication.md) -- Login flow, JWT details, and token refresh patterns.
- [S3 Buckets](s3-buckets.md) -- Bucket CRUD, versioning, and ACL reference.
- [S3 Objects](s3-objects.md) -- Object upload, download, copy, delete, and CORS configuration.
- [Tenants](tenants.md) -- Tenant-level settings and identity management.
- [Namespaces](namespaces.md) -- Namespace management, compliance, and access.
- [Argo Workflows](argo.md) -- ETL pipelines, presigned URL workflows, batch fan-out/fan-in with YAML and Hera.
- [Error Handling](error-handling.md) -- Retries, idempotency, ACID patterns, and fault-tolerant multipart uploads.
