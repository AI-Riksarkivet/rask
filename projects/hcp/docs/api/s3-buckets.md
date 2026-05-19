# S3 Buckets API

Manage S3-compatible buckets on the HCP system. All endpoints require JWT authentication.

**Base path:** `/api/v1/buckets`

!!! tip "rahcp SDK and CLI"
    ```python
    # SDK
    buckets = await client.s3.list_buckets()
    ```
    ```bash
    # CLI
    rahcp s3 ls                          # list buckets
    rahcp s3 ls my-bucket --prefix data/ # list objects
    ```
    See the [Python SDK](../sdk/index.md) for full documentation.

!!! tip "Full schema details"
    See the auto-generated [S3 Schema Reference](reference/s3.md) for exact field types and defaults, or the [Swagger UI](/docs#/S3%20Buckets) for try-it-out.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/buckets` | List all buckets |
| `POST` | `/api/v1/buckets` | Create a bucket |
| `HEAD` | `/api/v1/buckets/{bucket}` | Check if bucket exists |
| `DELETE` | `/api/v1/buckets/{bucket}` | Delete a bucket |
| `GET` | `/api/v1/buckets/{bucket}/versioning` | Get versioning status |
| `PUT` | `/api/v1/buckets/{bucket}/versioning` | Set versioning |
| `GET` | `/api/v1/buckets/{bucket}/versions` | List object versions |
| `GET` | `/api/v1/buckets/{bucket}/acl` | Get bucket ACL |
| `PUT` | `/api/v1/buckets/{bucket}/acl` | Set bucket ACL |

---

### List all buckets

```
GET /api/v1/buckets
```

Returns all buckets accessible to the authenticated user.

**Response:** `ListBucketsResponse`

| Field | Type | Description |
|-------|------|-------------|
| `buckets` | array | List of bucket objects (`name`, `creation_date`) |
| `owner` | object | Bucket owner (`display_name`, `id`) |

---

### Create a bucket

```
POST /api/v1/buckets
```

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `bucket` | string | Yes | Name for the new bucket |

**Response:** `BucketMutationResponse` — `{ status, bucket }`

---

### Check if bucket exists

```
HEAD /api/v1/buckets/{bucket}
```

Returns `200 OK` if the bucket exists, `404 Not Found` otherwise. No response body.

---

### Delete a bucket

```
DELETE /api/v1/buckets/{bucket}
```

Delete an S3 bucket. The bucket must be empty.

**Response:** `BucketMutationResponse`

---

### Get bucket versioning

```
GET /api/v1/buckets/{bucket}/versioning
```

**Response:** `BucketVersioningResponse`

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `Enabled`, `Suspended`, or empty (never enabled) |
| `mfa_delete` | string | MFA delete status |

---

### Set bucket versioning

```
PUT /api/v1/buckets/{bucket}/versioning
```

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | string | Yes | `Enabled` or `Suspended` |

**Response:** `VersioningMutationResponse` — `{ status, versioning }`

---

### Get bucket ACL

```
GET /api/v1/buckets/{bucket}/acl
```

**Response:** `AclResponse` — `{ owner, grants }`

---

### Set bucket ACL

```
PUT /api/v1/buckets/{bucket}/acl
```

**Request body:** `AclPolicy` — `{ Owner: { ID, DisplayName }, Grants: [{ Grantee, Permission }] }`

**Response:** `BucketMutationResponse`

---

## Code Examples

```bash
# List all buckets
curl -s http://localhost:8000/api/v1/buckets \
  -H "Authorization: Bearer $TOKEN" | jq .

# Create a bucket
curl -s -X POST http://localhost:8000/api/v1/buckets \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"bucket": "my-new-bucket"}' | jq .

# Check if a bucket exists
curl -s -o /dev/null -w "%{http_code}" -X HEAD \
  http://localhost:8000/api/v1/buckets/my-new-bucket \
  -H "Authorization: Bearer $TOKEN"

# Delete a bucket
curl -s -X DELETE http://localhost:8000/api/v1/buckets/my-new-bucket \
  -H "Authorization: Bearer $TOKEN" | jq .

# Enable versioning
curl -s -X PUT http://localhost:8000/api/v1/buckets/my-bucket/versioning \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status": "Enabled"}' | jq .
```
