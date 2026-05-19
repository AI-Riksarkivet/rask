# API Reference

The HCP Unified API provides a single REST interface that combines **S3 data-plane** operations with **MAPI (Management API) administration** for Hitachi Content Platform. This allows applications to manage storage objects, buckets, tenants, namespaces, and system configuration through one consistent, JWT-authenticated gateway.

## Base path

All endpoints are served under:

```
/api/v1
```

## Interactive documentation

| Tool | URL | Description |
|------|-----|-------------|
| Swagger UI | `/docs` | Interactive API explorer with try-it-out support |
| ReDoc | `/redoc` | Alternative read-optimized API reference |

## Authentication

All endpoints require a JWT bearer token obtained via the [Authentication](authentication.md) flow, **except** the health probes listed below.

## Endpoint groups

| Group | Prefix | Description | Access level |
|-------|--------|-------------|--------------|
| **Authentication** | `/api/v1/auth` | Login via OAuth2 password flow to obtain a JWT token. | Public |
| **Health** | `/liveness`, `/readiness`, `/health` | Liveness probe, readiness probe, and legacy health endpoint. | Public |
| **S3 Buckets** | `/api/v1/buckets` | Create, list, and manage S3 buckets including versioning and ACLs. | JWT required |
| **S3 Objects** | `/api/v1/buckets/{bucket}/objects` | Upload, download, copy, and delete objects within buckets. | JWT required |
| **S3 Versions** | `/api/v1/buckets/{bucket}/versions` | List object versions and delete markers for versioning-enabled buckets. | JWT required |
| **S3 Multipart** | `/api/v1/buckets/{bucket}/multipart` | Multipart upload: initiate, upload parts, complete, abort, list parts. | JWT required |
| **S3 Credentials** | `/api/v1/presign`, `/api/v1/credentials` | Generate presigned URLs and retrieve S3 access credentials. | JWT required |
| **System Admin: Tenants** | `/api/v1/mapi/tenants` | List and create tenants. | System admin |
| **System Admin: Identity** | `/api/v1/mapi/userAccounts`, `.../groupAccounts` | Manage system-level user and group accounts. | System admin |
| **System Admin: Infrastructure** | `/api/v1/mapi/network`, `.../storage/licenses`, `.../nodes/statistics` | Network settings, licenses, and system statistics. | System admin |
| **System Admin: Operations** | `/api/v1/mapi/healthCheckReport`, `.../logs`, `.../supportaccesscredentials` | Health checks, log collection, and support access. | System admin |
| **System Admin: Replication** | `/api/v1/mapi/services/replication` | Manage replication links, certificates, and schedules. | System admin |
| **System Admin: Erasure Coding** | `/api/v1/mapi/services/erasureCoding` | Manage erasure-coding topologies. | System admin |
| **Tenant Admin: Settings** | `/api/v1/mapi/tenants/{name}` | Console security, contact info, email, namespace defaults, permissions, CORS. | Tenant admin (ADMINISTRATOR) |
| **Tenant Admin: Statistics** | `/api/v1/mapi/tenants/{name}/statistics` | Tenant statistics and chargeback reports. | Tenant monitor (MONITOR) |
| **Tenant Admin: Identity** | `/api/v1/mapi/tenants/{name}/userAccounts`, `.../groupAccounts` | Manage tenant-level user and group accounts. | Tenant security (SECURITY) |
| **Tenant Admin: Content Classes** | `/api/v1/mapi/tenants/{name}/contentClasses` | Manage content classes for classification and retention. | Tenant admin (ADMINISTRATOR) |
| **Namespace: Management** | `/api/v1/mapi/tenants/{name}/namespaces` | Create, list, and manage namespaces. | Tenant admin (ADMINISTRATOR) |
| **Namespace: Compliance** | `/api/v1/mapi/tenants/{name}/namespaces/{ns}/...` | Compliance settings and retention classes. | Tenant compliance (COMPLIANCE) |
| **Namespace: Access** | `/api/v1/mapi/tenants/{name}/namespaces/{ns}/...` | Permissions, protocols (HTTP, NFS, CIFS, SMTP), CORS. | Tenant admin (ADMINISTRATOR) |
| **Namespace: Indexing** | `/api/v1/mapi/tenants/{name}/namespaces/{ns}/...` | Custom metadata indexing settings. | Tenant admin (ADMINISTRATOR) |
| **Namespace: Statistics** | `/api/v1/mapi/tenants/{name}/namespaces/{ns}/statistics` | Namespace statistics and chargeback reports. | Tenant monitor (MONITOR) |
| **Metadata Query** | `/api/v1/query/tenants/{name}` | Search objects by metadata and audit operations. | JWT required |
| **Lance Explorer** | `/api/v1/lance` | Browse LanceDB datasets: list tables, inspect schemas, read rows, and search. | JWT required |

## Access levels

- **Public** -- No authentication required.
- **JWT required** -- Any authenticated user with valid HCP credentials.
- **System admin** -- Requires a system-level HCP user account (login without tenant).
- **Tenant admin / security / monitor / compliance** -- Requires a tenant-scoped user account with the corresponding HCP role (`ADMINISTRATOR`, `SECURITY`, `MONITOR`, or `COMPLIANCE`).

## Python SDK

For scripting and automation, the [rahcp Python SDK](../sdk/index.md) provides a high-level async client that handles authentication, retries, presigned URL transfers, and multipart uploads automatically:

```python
from rahcp_client import HCPClient

async with HCPClient.from_env() as client:
    # S3 operations
    await client.s3.upload("bucket", "key", Path("file.bin"))
    await client.s3.download("bucket", "key", Path("output.bin"))
    result = await client.s3.list_objects("bucket", prefix="data/")

    # MAPI operations
    namespaces = await client.mapi.list_namespaces("tenant")
```

The CLI is included by default:

```bash
uv pip install rahcp
rahcp s3 ls my-bucket
rahcp s3 upload-all my-bucket ./local-dir
rahcp s3 verify my-bucket ./local-dir
```

See the [Python SDK documentation](../sdk/index.md) for full details.

## Related pages

- [Python SDK](../sdk/index.md) -- `rahcp-client` async client with automatic retries, presigned URLs, and multipart uploads.
- [Authentication](authentication.md) -- Login flow, JWT details, and code examples.
- [S3 Buckets](s3-buckets.md) -- Bucket CRUD, versioning, and ACLs.
- [S3 Objects](s3-objects.md) -- Object upload, download, copy, and delete.
- [System Administration](system.md) -- System-level MAPI endpoints.
- [Tenant Administration](tenants.md) -- Tenant-level settings and identity.
- [Namespaces](namespaces.md) -- Namespace management, compliance, and access.
- [Workflows](workflows.md) -- End-to-end curl, Python, and SDK examples for common tasks.
- [Argo Workflows](argo.md) -- ETL pipelines, batch processing, and presigned URL workflows with YAML and Hera.
- [Error Handling](error-handling.md) -- Retries, idempotency, ACID patterns, and fault-tolerant uploads.
