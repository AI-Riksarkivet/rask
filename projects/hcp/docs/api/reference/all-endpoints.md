# Complete HCP API Endpoint Reference

Comprehensive reference of all API endpoints available in the HCP platform, covering both the S3-compatible data access API and the HCP Management API (MAPI).

---

## Part 1: S3-Compatible API (31 Operations)

The S3-compatible API provides standard object storage operations. Requests are authenticated via AWS Signature V2, AWS Signature V4, Active Directory, or HCP-native authentication. All requests require `Authorization` and `Date` (or `x-amz-date`) headers.

---

### 1.1 Service-Level Operations

#### GET / -- List Buckets

Returns a list of all buckets owned by the authenticated user within the tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `/` |
| **Required Headers** | `Authorization`, `Date` or `x-amz-date`, `Host` |
| **Optional Headers** | `x-hcp-pretty-print` |
| **Response** | XML `ListAllMyBucketsResult` containing `Buckets` list with `Name` and `CreationDate` per bucket |
| **Status Codes** | `200 OK` |

---

### 1.2 Bucket-Level Operations

#### PUT / -- Create a Bucket

Creates a new bucket with the specified name (3-63 characters, alphanumeric and hyphens). Optionally attaches an ACL via headers.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `/` or `/bucket-name` |
| **Required Headers** | `Authorization`, `Content-Length` (must be `0`), `Date`, `Host` |
| **Optional Headers** | `x-amz-acl` (canned ACL), `x-amz-grant-full-control`, `x-amz-grant-read`, `x-amz-grant-read-acp`, `x-amz-grant-write`, `x-amz-grant-write-acp` |
| **Status Codes** | `200 OK` (created or already owned), `409 Conflict` (owned by someone else) |
| **Notes** | Returns `200` even on successful creation (not `201`). Also returns `200` if the bucket already exists and is owned by the requester. |

---

#### HEAD / -- Check Bucket Existence

Verifies whether a bucket exists. Returns no body.

| Detail | Value |
|--------|-------|
| **Method** | `HEAD` |
| **Path** | `/` or `/bucket-name` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Status Codes** | `200 OK` (exists), `403 Forbidden` (no permission), `404 Not Found` (does not exist) |

---

#### DELETE / -- Delete a Bucket

Deletes an empty bucket. Must be the bucket owner.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `/` or `/bucket-name` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Status Codes** | `204 No Content` (success), `409 Conflict` (not empty) |

---

#### PUT /?acl -- Set Bucket ACL

Replaces the entire ACL for a bucket. Can use headers or XML request body. Can also change bucket owner via request body.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `/?acl` or `/bucket-name?acl` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Optional Headers** | `Content-Length` (for body), `x-amz-acl`, `x-amz-grant-full-control`, `x-amz-grant-read`, `x-amz-grant-read-acp`, `x-amz-grant-write`, `x-amz-grant-write-acp` |
| **Request Body** | XML `AccessControlPolicy` (alternative to headers) |
| **Status Codes** | `200 OK` |
| **Notes** | The `acl` query parameter is NOT case sensitive. Maximum 100 permission grants per ACL. |

---

#### GET /?acl -- Retrieve Bucket ACL

Returns the ACL for a bucket as XML.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `/?acl` or `/bucket-name?acl` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Optional Headers** | `x-hcp-pretty-print` |
| **Response** | XML `AccessControlPolicy` with `Owner` and `AccessControlList` |
| **Status Codes** | `200 OK` |

---

#### PUT /?versioning -- Enable or Disable Versioning

Sets versioning status to `Enabled` or `Suspended` via XML request body. Must be bucket owner.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `/?versioning` or `/bucket-name?versioning` |
| **Required Headers** | `Authorization`, `Content-Length`, `Date`, `Host` |
| **Request Body** | XML `VersioningConfiguration` with `Status` element (`Enabled` or `Suspended`) |
| **Response Headers** | Includes `Location` header |
| **Status Codes** | `200 OK` |

---

#### GET /?versioning -- Check Versioning Status

Returns the versioning status of a bucket.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `/?versioning` or `/bucket-name?versioning` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Optional Headers** | `x-hcp-pretty-print` |
| **Response** | XML `VersioningConfiguration` with `Status` |
| **Status Codes** | `200 OK` |

---

#### GET / -- List Bucket Contents (Version 1)

Lists objects and folders in a bucket. Supports current-only or version listings.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `/[?query-params]` or `/bucket-name[?query-params]` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Optional Headers** | `x-hcp-pretty-print` |
| **Query Parameters** | `versions` (enables version listing), `delimiter`, `encoding-type`, `prefix`, `max-keys` (default 1000), `marker`, `key-marker`, `version-id-marker` |
| **Response** | XML `ListBucketResult` (or `ListVersionsResult` with `versions` param) |
| **Status Codes** | `200 OK` |
| **Notes** | `marker` is ignored if `versions` is also specified. Use `key-marker` and `version-id-marker` for version listing pagination. |

---

#### GET /?list-type=2 -- List Bucket Contents (Version 2)

Version 2 listing with continuation tokens instead of markers.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `/?list-type=2[&query-params]` or `/bucket-name?list-type=2[&query-params]` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Query Parameters** | `list-type=2` (required), `continuation-token`, `delimiter`, `encoding-type`, `fetch-owner` (boolean), `max-keys` (default 1000), `prefix`, `start-after` |
| **Response** | XML `ListBucketResult` with `KeyCount` and `NextContinuationToken` |
| **Status Codes** | `200 OK` |
| **Notes** | Does NOT support the `versions` parameter. Owner information is NOT returned by default; set `fetch-owner=true` to include it. |

---

#### GET /?uploads -- List In-Progress Multipart Uploads

Lists all multipart uploads that have been initiated but not completed or aborted.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `/?uploads[&query-params]` or `/bucket-name?uploads[&query-params]` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Query Parameters** | `uploads` (required), `delimiter`, `encoding-type`, `key-marker`, `max-uploads` (default 1000, returns `400` if >1000), `prefix`, `upload-id-marker` |
| **Response** | XML `ListMultipartUploadsResult` |
| **Status Codes** | `200 OK`, `400 Bad Request` (if `max-uploads` > 1000) |
| **Notes** | `upload-id-marker` is ignored if `key-marker` is not also specified. |

---

#### PUT /?cors -- Set CORS Configuration

Adds a CORS rules XML configuration to a bucket (namespace).

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `/?cors` or `/bucket-name?cors` |
| **Required Headers** | `Authorization`, `Content-MD5`, `x-amz-date` |
| **Request Body** | XML `CORSConfiguration` with `CORSRule` elements (`AllowedOrigin`, `AllowedMethod`, `AllowedHeader`, `MaxAgeSeconds`, `ExposeHeader`) |
| **Status Codes** | `200 OK` |
| **Notes** | Maximum CORS configuration size: 2.5 MB. CORS does NOT support tenant-level API calls. |

---

#### GET /?cors -- Get CORS Configuration

Returns the CORS configuration XML for a bucket.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `/?cors` or `/bucket-name?cors` |
| **Required Headers** | `Authorization`, `Date` |
| **Response** | XML `CORSConfiguration` |
| **Status Codes** | `200 OK` |

---

#### DELETE /?cors -- Delete CORS Configuration

Removes the CORS configuration from a bucket.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `/?cors` or `/bucket-name?cors` |
| **Required Headers** | `Authorization`, `Date` |
| **Status Codes** | `204 No Content` |

---

### 1.3 Object-Level Operations

#### PUT /object-name -- Store an Object

Stores a new object or creates a new version if versioning is enabled. Supports custom metadata, ACLs, retention, Object Lock, and labeled holds.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `/object-name` or `/bucket-name/object-name` |
| **Required Headers** | `Authorization`, `Content-Length`, `Date`, `Host` |
| **Optional Headers** | `Content-MD5`, `Content-Type`, `Expect` (`100-continue`), `x-amz-acl`, `x-amz-grant-*`, `x-amz-meta-*` (custom metadata), `x-amz-server-side-encryption`, `x-amz-object-lock-mode` (`GOVERNANCE` or `COMPLIANCE`), `x-amz-object-lock-retain-until-date`, `x-amz-object-lock-legal-hold` (`ON` or `OFF`), `x-hcp-labelretentionhold` (JSON, max 100 labels, max 64-char IDs), `x-hcp-retention`, `x-hcp-retentionhold` |
| **Request Body** | Object data |
| **Status Codes** | `200 OK`, `409 Conflict` (versioning disabled and object exists), `411 Length Required` (missing `Content-Length`), `413 Request Entity Too Large` (insufficient space) |

---

#### PUT /folder-name/ -- Create a Folder

Creates an empty folder. Indicated by a trailing `/` or `Content-Type: x-directory`.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `/folder-name/` or `/bucket-name/folder-name/` |
| **Required Headers** | `Authorization`, `Content-Length` (should be `0`), `Date`, `Host` |
| **Optional Headers** | `Content-Type` (`x-directory`) |
| **Status Codes** | `200 OK`, `409 Conflict` (object or folder with same name already exists) |
| **Notes** | HCP ignores any request body. Trailing `/` or `%2F` indicates folder. |

---

#### HEAD /object-name -- Check Object or Folder Existence

Returns metadata headers for an object, object version, or folder. Supports conditional checks.

| Detail | Value |
|--------|-------|
| **Method** | `HEAD` |
| **Path** | `/object-name[?versionId=id]` or `/folder-name/` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Optional Query Params** | `versionId` |
| **Conditional Headers** | `If-Match`, `If-Modified-Since`, `If-None-Match`, `If-Unmodified-Since` |
| **Response Headers** | `Content-Length`, `Content-Type`, `ETag`, `Last-Modified`, `Accept-Ranges: bytes`, `x-amz-meta-*`, `x-amz-version-id`, `x-amz-delete-marker` (for delete markers), `x-amz-missing-meta`, `x-amz-object-lock-mode`, `x-amz-object-lock-retain-until-date`, `x-amz-object-lock-legal-hold`, `x-hcp-retention`, `x-hcp-retentionhold`, `x-hcp-labelretentionhold` |
| **Status Codes** | `200 OK`, `304 Not Modified`, `404 Not Found`, `412 Precondition Failed` |

---

#### GET /object-name -- Retrieve an Object

Downloads object data. Supports conditional retrieval, byte-range requests, and response header overrides.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `/object-name[?versionId=id]` or `/bucket-name/object-name[?versionId=id]` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Optional Query Params** | `versionId`, `response-cache-control`, `response-content-disposition`, `response-content-encoding`, `response-content-language`, `response-content-type`, `response-expires` |
| **Conditional Headers** | `If-Match`, `If-Modified-Since`, `If-None-Match`, `If-Unmodified-Since` |
| **Optional Headers** | `Range` (byte-range retrieval) |
| **Response Headers** | `Content-Length`, `Content-Type`, `ETag`, `Last-Modified`, `x-amz-version-id`, `x-hcp-labelretentionhold`, `x-hcp-labelretentionhold-labels` (JSON, privileged users only), `x-hcp-retention`, `x-hcp-retentionhold` |
| **Status Codes** | `200 OK`, `204 No Content` (current version is a delete marker), `206 Partial Content` (range request), `304 Not Modified`, `412 Precondition Failed`, `416 Requested Range Not Satisfiable` |

---

#### PUT /target-object-name (with x-amz-copy-source) -- Copy an Object

Copies an object within or between buckets. Can copy specific versions, replace metadata, set ACLs, and set retention/holds.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `/target-object-name` or `/bucket-name/target-object-name` |
| **Required Headers** | `Authorization`, `Content-Type`, `Date`, `Host`, `x-amz-copy-source` (format: `/bucket/object[?versionId=id]`) |
| **Optional Headers** | `x-amz-metadata-directive` (`COPY` default, or `REPLACE`), `x-amz-acl`, `x-amz-grant-*`, `x-amz-meta-*`, `x-amz-copy-source-if-match`, `x-amz-copy-source-if-modified-since`, `x-amz-copy-source-if-none-match`, `x-amz-copy-source-if-unmodified-since`, `x-amz-server-side-encryption`, `x-hcp-retention`, `x-hcp-retentionhold`, `x-hcp-labelretentionhold` |
| **Response** | XML `CopyObjectResult` with `ETag` and `LastModified` |
| **Response Headers** | `x-amz-copy-source-version-id` |
| **Status Codes** | `200 OK` |
| **Notes** | HCP does NOT copy ACLs or version IDs with objects. |

---

#### PUT /object-name?acl -- Set Object ACL

Replaces the entire ACL for an object. Applies to all versions.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `/object-name?acl` or `/bucket-name/object-name?acl` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Optional Headers** | `Content-Length` (for body), `x-amz-acl`, `x-amz-grant-*` |
| **Request Body** | XML `AccessControlPolicy` (alternative to headers). Can change object owner via body. |
| **Status Codes** | `200 OK` |

---

#### GET /object-name?acl -- Retrieve Object ACL

Returns the ACL for an object as XML.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `/object-name?acl` or `/bucket-name/object-name?acl` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Optional Headers** | `x-hcp-pretty-print` |
| **Response** | XML `AccessControlPolicy` |
| **Status Codes** | `200 OK` |

---

#### DELETE /object-name -- Delete an Object or Folder

Deletes an object, specific version, or folder. With versioning enabled, creates a delete marker instead of truly deleting.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `/object-name[?versionId=id]` or `/folder-name/` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Optional Query Params** | `versionId` |
| **Optional Headers** | `x-hcp-privileged` (privileged delete of retained objects), `x-amz-bypass-governance-retention` (`true`) |
| **Status Codes** | `204 No Content` (success, also returned if object does not exist) |
| **Notes** | Cannot delete objects under retention or on hold. When versioning is enabled, creates a delete marker. Delete marker can be removed via DELETE with its specific `versionId`. |

---

#### POST /?delete -- Delete Multiple Objects (Batch Delete)

Batch deletes multiple objects in a single request via XML body.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `/?delete` or `/bucket-name?delete` |
| **Required Headers** | `Authorization`, `Content-MD5`, `Host`, `x-amz-date` |
| **Request Body** | XML `Delete` element containing `Object` elements (each with `Key` and optional `VersionId`) |
| **Response** | XML `DeleteResult` with `Deleted` and `Error` elements |
| **Status Codes** | `200 OK` |
| **Notes** | `Content-MD5` is required (not optional). Cannot delete objects under retention, objects on hold, delete markers of objects, or multiple versions of objects. |

---

#### POST / (multipart/form-data) -- POST Object Upload (Browser-Based)

Browser-based form upload of an object using HTML form fields for authentication, policy, and metadata.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `/` or `/bucket-name` |
| **Content-Type** | `multipart/form-data` |
| **Required Form Fields** | `key`, `file`, `policy` (if not public bucket) |
| **Auth Form Fields (V2)** | `AWSAccessKeyId`, `Signature` |
| **Auth Form Fields (V4)** | `x-amz-algorithm`, `x-amz-credential`, `x-amz-date`, `x-amz-signature` |
| **Optional Form Fields** | `acl`, `success_action_redirect`, `success_action_status` (`200`, `201`, `204` default), `x-amz-meta-*`, `x-hcp-retention`, `x-hcp-retentionhold` |
| **Status Codes** | `200 OK`, `201 Created` (with XML body), `204 No Content` |
| **Notes** | Active Directory, SPNEGO, cookie, and HCP authentication are NOT supported for form uploads. |

---

### 1.4 Multipart Upload Operations

#### POST /object-name?uploads -- Initiate Multipart Upload

Starts a new multipart upload and returns an upload ID. Supports ACLs and custom metadata.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `/object-name?uploads` or `/bucket-name/object-name?uploads` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Query Parameters** | `uploads` (required, **case sensitive**) |
| **Optional Headers** | `Content-Type`, `x-amz-acl`, `x-amz-grant-*`, `x-amz-meta-*` |
| **Response** | XML `InitiateMultipartUploadResult` with `UploadId` |
| **Status Codes** | `200 OK` |
| **Notes** | Custom metadata and ACL specified at initiation apply to the completed object. |

---

#### PUT /object-name?partNumber=N&uploadId=id -- Upload a Part

Uploads a single part of a multipart upload.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `/object-name?partNumber=N&uploadId=id` |
| **Required Headers** | `Authorization`, `Content-Length`, `Date`, `Host` |
| **Query Parameters** | `partNumber` (1-10000, NOT case sensitive), `uploadId` (NOT case sensitive) |
| **Optional Headers** | `Content-MD5`, `Expect` |
| **Status Codes** | `200 OK`, `400 Bad Request` (custom metadata headers sent) |
| **Notes** | Minimum part size (except last): 1 MB. Maximum: 5 GB. Part numbers do not need to be consecutive. ACL headers are ignored. Custom metadata headers cause `400 Bad Request`. |

---

#### PUT /object-name?partNumber=N&uploadId=id (with x-amz-copy-source) -- Upload Part by Copying

Creates a part by copying data from an existing object. Supports copying a byte range and conditional copying.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `/object-name?partNumber=N&uploadId=id` |
| **Required Headers** | `Authorization`, `Date`, `Host`, `x-amz-copy-source` (format: `/bucket/object[?versionId=id]`) |
| **Query Parameters** | `partNumber`, `uploadId` |
| **Optional Headers** | `x-amz-copy-source-range` (`bytes=start-end`), `x-amz-copy-source-if-match`, `x-amz-copy-source-if-modified-since`, `x-amz-copy-source-if-none-match`, `x-amz-copy-source-if-unmodified-since` |
| **Status Codes** | `200 OK` |
| **Notes** | If the client times out, HCP continues the operation in the background. |

---

#### GET /object-name?uploadId=id -- List Parts of a Multipart Upload

Lists the parts that have been uploaded for a specific multipart upload.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `/object-name?uploadId=id` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Query Parameters** | `uploadId` (**case sensitive**), `encoding-type`, `max-parts` (default 1000, returns `400` if >1000), `part-number-marker` |
| **Optional Headers** | `x-hcp-pretty-print` |
| **Response** | XML `ListPartsResult` |
| **Response Headers** | `x-amz-abort-date` (date of automatic abort based on namespace config) |
| **Status Codes** | `200 OK`, `400 Bad Request` (if `max-parts` > 1000) |

---

#### POST /object-name?uploadId=id -- Complete Multipart Upload

Assembles previously uploaded parts into a final object. Requires XML body listing parts.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `/object-name?uploadId=id` |
| **Required Headers** | `Authorization`, `Content-Length`, `Content-Type` (`application/xml` or `text/xml`), `Date`, `Host` |
| **Query Parameters** | `uploadId` |
| **Request Body** | XML `CompleteMultipartUpload` containing `Part` elements (each with `PartNumber` and `ETag`) in ascending order |
| **Response** | XML `CompleteMultipartUploadResult` |
| **Status Codes** | `200 OK`, `400 Bad Request` (custom metadata headers sent, or no parts listed) |
| **Notes** | Must have at least one part. Parts must be listed in ascending order by part number. |

---

#### DELETE /object-name?uploadId=id -- Abort Multipart Upload

Cancels a multipart upload and frees associated storage.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `/object-name?uploadId=id` |
| **Required Headers** | `Authorization`, `Date`, `Host` |
| **Query Parameters** | `uploadId` |
| **Status Codes** | `204 No Content` |
| **Notes** | Parts may not be deleted immediately after abort. |

---

### 1.5 CORS Runtime Operations

#### OPTIONS /resource -- CORS Preflight Request

Browser-issued preflight to check cross-origin permissions before the actual request.

| Detail | Value |
|--------|-------|
| **Method** | `OPTIONS` |
| **Path** | Any resource path |
| **Request Headers (set by browser)** | `Origin`, `Access-Control-Request-Method`, `Access-Control-Request-Headers` |
| **Response Headers** | `Access-Control-Allow-Origin`, `Access-Control-Allow-Methods`, `Access-Control-Allow-Headers`, `Access-Control-Expose-Headers`, `Access-Control-Max-Age`, `Access-Control-Allow-Credentials`, `Vary` |
| **Status Codes** | `200 OK`, `403 Forbidden` (no matching CORS rule) |
| **Notes** | Namespaces with reserved keywords (`rest`, `webdav`, `fcfs_data`, `browser`, `hs3`, `swift`) are not supported for preflighted CORS requests. |

---

### 1.6 S3 API Cross-Cutting Details

- **Authentication**: AWS Signature V2, AWS Signature V4, Active Directory, HCP-native, anonymous access
- **HCP-specific headers** (not in standard S3): `x-hcp-pretty-print`, `x-hcp-retention`, `x-hcp-retentionhold`, `x-hcp-labelretentionhold`, `x-hcp-labelretentionhold-labels`, `x-hcp-privileged`
- **Versioning behavior**: When versioning is disabled and an object exists, PUT returns `409 Conflict`. When enabled, a new version is created.
- **Delete markers**: When versioning is enabled, DELETE creates a delete marker. The delete marker can be removed by DELETE with its specific `versionId`.
- **Presigned URLs**: Supported for pre-authenticated access to objects without requiring the requester to have credentials.

---

## Part 2: HCP Management API -- MAPI (~120+ Operations)

The HCP Management API (MAPI) is a RESTful HTTP interface on port **9090**. All resource paths are prefixed with `/mapi/`. Authentication uses `Authorization: HCP base64(username):md5(password)` or `Authorization: AD username@domain:password`, or a cookie-based session.

Base URLs:

- System-level: `https://admin.hcp.example.com:9090/mapi/...`
- Tenant-level: `https://<tenant>.hcp.example.com:9090/mapi/...`

### Cross-Cutting Query Parameters

| Parameter | Description | Applicable Resources |
|-----------|-------------|---------------------|
| `prettyprint` | Format output for readability | Most GET endpoints |
| `verbose` | Return detailed output | Group accounts, user accounts, licenses |
| `offset` | Skip N results (paging) | Namespaces, user accounts, data access permissions |
| `count` | Return N results (paging) | Namespaces, user accounts, data access permissions |
| `sortType` | Sort field (`name`, `hardQuota`, `username`) | Namespaces, user accounts |
| `sortOrder` | `ascending` or `descending` | Namespaces, user accounts |
| `filterType` | Filter field (`name`, `tag`, `username`) | Namespaces, user accounts |
| `filterString` | Filter value | Namespaces, user accounts |
| `template` | Return a blank XML/JSON template for resource creation | Most PUT endpoints |

### Output Formats

Controlled by `Accept` header: `application/xml`, `application/json`, or `text/csv` (chargeback reports only).

---

### 2.1 Content Classes

Resources for managing content classes within a tenant.

#### PUT .../tenants/{tenant-name}/contentClasses -- Create Content Class

Creates a content class for a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../tenants/{tenant-name}/contentClasses` |
| **Request Body** | `contentClass` (XML/JSON) with `name`, `contentProperties`, `namespaces` |
| **Access** | Tenant-level admin (HCP tenant) or system-level admin (default tenant) |

#### GET .../tenants/{tenant-name}/contentClasses -- List Content Classes

Retrieves a list of content classes for a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/contentClasses` |
| **Query Params** | `prettyprint` |
| **Response** | `List` of content classes |
| **Access** | Tenant-level monitor/admin or system-level monitor/admin |

#### GET .../tenants/{tenant-name}/contentClasses/{content-class-name} -- Get Content Class

Retrieves information about a specific content class.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/contentClasses/{content-class-name}` |
| **Response** | `contentClass` |

#### HEAD .../tenants/{tenant-name}/contentClasses/{content-class-name} -- Check Content Class Existence

Checks whether a specific content class exists.

| Detail | Value |
|--------|-------|
| **Method** | `HEAD` |
| **Path** | `.../tenants/{tenant-name}/contentClasses/{content-class-name}` |

#### POST .../tenants/{tenant-name}/contentClasses/{content-class-name} -- Modify Content Class

Modifies an existing content class.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/contentClasses/{content-class-name}` |
| **Request Body** | `contentClass` (XML/JSON) |

#### DELETE .../tenants/{tenant-name}/contentClasses/{content-class-name} -- Delete Content Class

Deletes a content class. The content class must have no content properties.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../tenants/{tenant-name}/contentClasses/{content-class-name}` |

---

### 2.2 Erasure Coding Topologies

Resources for managing erasure coding topologies and their tenant/link membership.

#### PUT .../services/erasureCoding/ecTopologies -- Create EC Topology

Creates an erasure coding topology.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../services/erasureCoding/ecTopologies` |
| **Request Body** | `ecTopology` (name, description, type, replicationLinks, erasureCodingDelay, fullCopy, minimumObjectSize, restorePeriod) |
| **Access** | System-level admin |

#### GET .../services/erasureCoding/ecTopologies -- List EC Topologies

Retrieves a list of existing erasure coding topologies.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/erasureCoding/ecTopologies` |
| **Query Params** | `verbose`, `prettyprint` |
| **Access** | System-level monitor/admin |

#### GET .../services/erasureCoding/ecTopologies/{ec-topology-name} -- Get EC Topology

Retrieves information about a specific erasure coding topology.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/erasureCoding/ecTopologies/{ec-topology-name}` |
| **Query Params** | `verbose`, `prettyprint` |

#### HEAD .../services/erasureCoding/ecTopologies/{ec-topology-name} -- Check EC Topology Existence

Checks whether a specific erasure coding topology exists.

| Detail | Value |
|--------|-------|
| **Method** | `HEAD` |
| **Path** | `.../services/erasureCoding/ecTopologies/{ec-topology-name}` |

#### POST .../services/erasureCoding/ecTopologies/{ec-topology-name} -- Modify or Retire EC Topology

Modifies or retires an erasure coding topology.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../services/erasureCoding/ecTopologies/{ec-topology-name}` |
| **Query Params** | `?retire` (to retire the topology) |
| **Request Body** | `ecTopology` (for modification) |

#### DELETE .../services/erasureCoding/ecTopologies/{ec-topology-name} -- Delete EC Topology

Deletes an erasure coding topology.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../services/erasureCoding/ecTopologies/{ec-topology-name}` |

#### GET .../services/erasureCoding/ecTopologies/{ec-topology-name}/tenantCandidates -- List Eligible Tenants

Retrieves the list of tenants eligible to be added to an EC topology.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/erasureCoding/ecTopologies/{ec-topology-name}/tenantCandidates` |
| **Query Params** | `verbose`, `prettyprint` |
| **Response** | `tenantCandidates` |

#### GET .../services/erasureCoding/ecTopologies/{ec-topology-name}/tenantConflictingCandidates -- List Ineligible Tenants

Retrieves the list of tenants NOT eligible due to name or link conflicts.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/erasureCoding/ecTopologies/{ec-topology-name}/tenantConflictingCandidates` |
| **Response** | `tenantCandidates` |

#### GET .../services/erasureCoding/ecTopologies/{ec-topology-name}/tenants -- List Included Tenants

Retrieves the list of tenants included in an EC topology.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/erasureCoding/ecTopologies/{ec-topology-name}/tenants` |
| **Response** | `List` |

#### PUT .../services/erasureCoding/ecTopologies/{ec-topology-name}/tenants/{tenant-name} -- Add Tenant to EC Topology

Adds a tenant to an erasure coding topology.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../services/erasureCoding/ecTopologies/{ec-topology-name}/tenants/{tenant-name}` |
| **Notes** | Use `-X PUT`, not `-T` |

#### DELETE .../services/erasureCoding/ecTopologies/{ec-topology-name}/tenants/{tenant-name} -- Remove Tenant from EC Topology

Removes a tenant from an erasure coding topology.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../services/erasureCoding/ecTopologies/{ec-topology-name}/tenants/{tenant-name}` |

#### GET .../services/erasureCoding/linkCandidates -- List Eligible Replication Links

Retrieves the list of replication links eligible for use in an EC topology.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/erasureCoding/linkCandidates` |
| **Query Params** | `verbose`, `prettyprint` |
| **Response** | `replicationLinks` |

---

### 2.3 Health Check Reports

Resources for preparing, downloading, and managing health check reports.

#### GET .../healthCheckReport -- Get Health Check Report Status

Retrieves the status of a health check report download in progress.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../healthCheckReport` |
| **Query Params** | `prettyprint` |
| **Response** | `healthCheckDownloadStatus` |
| **Access** | System-level admin/service role |

#### POST .../healthCheckReport/prepare -- Prepare Health Check Reports

Prepares health check reports for download.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../healthCheckReport/prepare` |
| **Request Body** | `healthCheckPrepare` (`startDate`, `endDate`, `exactTime`, `collectCurrent`) |

#### POST .../healthCheckReport/download -- Download Health Check Reports

Starts the health check report download.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../healthCheckReport/download` |
| **Request Body** | `healthCheckDownload` (`nodes`, `content`) |

#### POST .../healthCheckReport/cancel -- Cancel Health Check Report Download

Cancels a health check report download in progress.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../healthCheckReport/cancel` |
| **Query Params** | `?cancel` |

---

### 2.4 Licenses

Resources for managing HCP storage licenses.

#### GET .../storage/licenses -- Retrieve License(s)

Retrieves the current storage license or a list of current and past licenses.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../storage/licenses` |
| **Query Params** | `verbose` (for historical list), `prettyprint` |
| **Response** | `Licenses` |
| **Access** | System-level monitor/admin |

#### PUT .../storage/licenses -- Upload License

Uploads a new storage license.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../storage/licenses` |
| **Request Body** | License key text string |
| **Access** | System-level admin |

---

### 2.5 Logs

Resources for managing, packaging, and downloading system logs.

#### GET .../logs -- Get Log Download Status

Retrieves the status of a log download in progress.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../logs` |
| **Query Params** | `prettyprint` |
| **Response** | `LogDownloadStatus` |
| **Access** | System-level admin/service/monitor |

#### POST .../logs?mark -- Mark Logs

Marks the logs with a supplied message.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../logs` |
| **Query Params** | `?mark=<message>` (use `+` or `%20` for spaces) |
| **Access** | System-level admin/service |

#### POST .../logs?cancel -- Cancel Log Download

Cancels or clears a log download in progress.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../logs` |
| **Query Params** | `?cancel` |
| **Access** | System-level admin/service |

#### POST .../logs/prepare -- Prepare Logs for Download

Packages logs for download.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../logs/prepare` |
| **Request Body** | `logPrepare` (`startDate`, `endDate`) |
| **Notes** | Packages expire after 24 hours. |

#### POST .../logs/download -- Download Logs

Starts the log download.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../logs/download` |
| **Request Body** | `logDownload` (`nodes`, `content` e.g. `SERVICE`) |
| **Response** | `.zip` file |

---

### 2.6 Namespaces

Resources for managing HCP namespaces and their settings within a tenant.

#### PUT .../tenants/{tenant-name}/namespaces -- Create Namespace

Creates an HCP namespace.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../tenants/{tenant-name}/namespaces` |
| **Request Body** | `namespace` (XML/JSON) with `name`, `hashScheme`, `hardQuota`, `softQuota`, `servicePlan`, `optimizedFor`, `versioningSettings`, `searchEnabled`, `replicationEnabled`, `tags`, etc. |
| **Access** | Tenant-level admin or allow namespace management |

#### GET .../tenants/{tenant-name}/namespaces -- List Namespaces

Retrieves a list of namespaces owned by a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces` |
| **Query Params** | `offset`, `count`, `sortType` (`name` or `hardQuota`), `sortOrder`, `filterType` (`name` or `tag`), `filterString`, `prettyprint` |

#### GET .../tenants/{tenant-name}/namespaces/{namespace-name} -- Get Namespace

Retrieves information about a specific namespace.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}` |
| **Query Params** | `prettyprint` |
| **Response** | `namespace` |

#### HEAD .../tenants/{tenant-name}/namespaces/{namespace-name} -- Check Namespace Existence

Checks whether a specific namespace exists.

| Detail | Value |
|--------|-------|
| **Method** | `HEAD` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}` |

#### POST .../tenants/{tenant-name}/namespaces/{namespace-name} -- Modify Namespace

Modifies a namespace's properties.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}` |
| **Request Body** | `namespace` (XML/JSON) with properties to change |

#### DELETE .../tenants/{tenant-name}/namespaces/{namespace-name} -- Delete Namespace

Deletes an HCP namespace. Namespace must be empty. Not valid for the default namespace.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}` |

#### PUT .../tenants/{tenant-name}/namespaces/cors -- Set Namespace CORS

Sets CORS rules configuration for a namespace. Overrides tenant-level default CORS.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../tenants/{tenant-name}/namespaces/cors` |
| **Request Body** | `cors` (XML/JSON) |

#### GET .../tenants/{tenant-name}/namespaces/cors -- Get Namespace CORS

Retrieves CORS configuration for a namespace.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces/cors` |

#### DELETE .../tenants/{tenant-name}/namespaces/cors -- Delete Namespace CORS

Deletes the CORS configuration for a namespace. Returns `404` if not set.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../tenants/{tenant-name}/namespaces/cors` |

#### GET .../tenants/{tenant-name}/namespaces/{namespace-name}/chargebackReport -- Namespace Chargeback Report

Generates a chargeback report for a namespace.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/chargebackReport` |
| **Query Params** | `start` (ISO 8601), `end` (ISO 8601), `granularity` (`hour`, `day`, `total`) |
| **Response Formats** | XML, JSON, CSV |

#### GET .../tenants/{tenant-name}/namespaces/{namespace-name}/complianceSettings -- Get Compliance Settings

Retrieves retention, shred, custom metadata handling, and disposition settings.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/complianceSettings` |

#### POST .../tenants/{tenant-name}/namespaces/{namespace-name}/complianceSettings -- Modify Compliance Settings

Modifies compliance settings for a namespace.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/complianceSettings` |
| **Request Body** | `complianceSettings` (`retentionDefault`, `shreddingDefault`, `customMetadataChanges`, `dispositionEnabled`, `minimumRetentionAfterInitialUnspecified`) |
| **Access** | Compliance role |

#### GET .../tenants/{tenant-name}/namespaces/{namespace-name}/customMetadataIndexingSettings -- Get Custom Metadata Indexing

Retrieves custom metadata indexing settings for a search-enabled namespace.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/customMetadataIndexingSettings` |
| **Notes** | Not valid for non-search-enabled namespaces. |

#### POST .../tenants/{tenant-name}/namespaces/{namespace-name}/customMetadataIndexingSettings -- Modify Custom Metadata Indexing

Modifies custom metadata indexing settings.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/customMetadataIndexingSettings` |
| **Request Body** | `customMetadataIndexingSettings` |

#### GET .../tenants/{tenant-name}/namespaces/{namespace-name}/permissions -- Get Namespace Permissions

Retrieves the data access permission mask for a namespace.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/permissions` |
| **Response** | `List` of permissions |

#### POST .../tenants/{tenant-name}/namespaces/{namespace-name}/permissions -- Modify Namespace Permissions

Modifies the data access permission mask.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/permissions` |
| **Request Body** | List of permissions: `DELETE`, `PRIVILEGED`, `PURGE`, `READ`, `SEARCH`, `WRITE` (case sensitive) |

#### GET .../tenants/{tenant-name}/namespaces/{namespace-name}/protocols -- Get Default Namespace HTTP Protocol Settings

Retrieves HTTP protocol settings for the default namespace only.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/protocols` |
| **Notes** | Superseded by `.../protocols/http` for HCP namespaces. |

#### POST .../tenants/{tenant-name}/namespaces/{namespace-name}/protocols -- Modify Default Namespace HTTP Protocol Settings

Modifies HTTP protocol settings for the default namespace.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/protocols` |
| **Request Body** | `protocols` |

#### GET .../tenants/{tenant-name}/namespaces/{namespace-name}/protocols/{protocol-name} -- Get Protocol Settings

Retrieves protocol-specific settings for a namespace.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/protocols/{protocol-name}` |
| **Path Params** | `protocol-name`: `cifs`, `http`, `nfs`, or `smtp` (case sensitive) |
| **Response** | `cifsProtocol`, `httpProtocol`, `nfsProtocol`, or `smtpProtocol` |

#### POST .../tenants/{tenant-name}/namespaces/{namespace-name}/protocols/{protocol-name} -- Modify Protocol Settings

Modifies protocol-specific settings for a namespace.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/protocols/{protocol-name}` |
| **Request Body** | `cifsProtocol`, `httpProtocol`, `nfsProtocol`, or `smtpProtocol` |

#### GET .../tenants/{tenant-name}/namespaces/{namespace-name}/replicationCollisionSettings -- Get Replication Collision Settings

Retrieves replication collision handling settings for a namespace.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/replicationCollisionSettings` |

#### POST .../tenants/{tenant-name}/namespaces/{namespace-name}/replicationCollisionSettings -- Modify Replication Collision Settings

Modifies replication collision handling settings.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/replicationCollisionSettings` |
| **Request Body** | `replicationCollisionSettings` |

#### GET .../tenants/{tenant-name}/namespaces/{namespace-name}/statistics -- Get Namespace Statistics

Retrieves statistics about namespace content.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/statistics` |
| **Response** | `statistics` |

#### GET .../tenants/{tenant-name}/namespaces/{namespace-name}/versioningSettings -- Get Versioning Settings

Retrieves versioning settings for a namespace.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/versioningSettings` |

#### POST .../tenants/{tenant-name}/namespaces/{namespace-name}/versioningSettings -- Modify Versioning Settings

Modifies versioning settings for a namespace.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/versioningSettings` |
| **Request Body** | `versioningSettings` (`enabled`, `prune`, `pruneDays`, `useDeleteMarkers`) |

#### DELETE .../tenants/{tenant-name}/namespaces/{namespace-name}/versioningSettings -- Delete Versioning Settings

Deletes versioning settings for a namespace. Not valid for the default namespace.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/versioningSettings` |

---

### 2.7 Network

Resources for managing downstream DNS mode.

#### GET .../network -- Get Network Settings

Retrieves the current downstream DNS mode.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../network` |
| **Response** | `networkSettings` |
| **Access** | System-level monitor/admin |

#### POST .../network -- Modify Network Settings

Modifies the downstream DNS mode setting.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../network` |
| **Request Body** | `networkSettings` (`downstreamDNSMode`: `ADVANCED` or `BASIC`) |
| **Access** | System-level admin |

---

### 2.8 Node Statistics

Resources for retrieving per-node statistics.

#### GET .../nodes/statistics -- Get Node Statistics

Retrieves statistics of nodes in the HCP system (per-node CPU, bandwidth, HTTP connections, volume stats).

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../nodes/statistics` |
| **Query Params** | `prettyprint` |
| **Response** | `nodeStatistics` |
| **Access** | System-level admin/monitor |
| **Notes** | Best practice: poll no more than once per hour. |

---

### 2.9 Replication

Resources for managing the replication service, certificates, links, link content, candidates, and schedules.

#### GET .../services/replication -- Get Replication Service Settings

Retrieves replication service settings.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication` |
| **Response** | `replicationService` |

#### POST .../services/replication -- Modify Replication Service

Modifies or performs an action on the replication service.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../services/replication` |

#### PUT .../services/replication/certificates -- Upload Replication Certificate

Uploads a new replication certificate.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../services/replication/certificates` |
| **Request Body** | Certificate text file |

#### GET .../services/replication/certificates -- List Replication Certificates

Retrieves a list of replication certificates.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/certificates` |
| **Query Params** | `prettyprint` |
| **Response** | `Certificates` |

#### GET .../services/replication/certificates/{certificate-id} -- Get Certificate

Retrieves information about a specific replication certificate.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/certificates/{certificate-id}` |
| **Response** | `Text` |

#### DELETE .../services/replication/certificates/{certificate-id} -- Delete Certificate

Deletes a replication certificate.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../services/replication/certificates/{certificate-id}` |

#### GET .../services/replication/certificates/server -- Download Server Certificate

Downloads the replication server certificate.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/certificates/server` |
| **Response** | Saves as `server_certificate.txt` |
| **Access** | System-level admin |

#### PUT .../services/replication/links -- Create Replication Link

Creates a replication link.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../services/replication/links` |
| **Request Body** | `link` (`name`, `type` `ACTIVE_ACTIVE` or `ACTIVE_PASSIVE`, `connection`, `compression`, `encryption`, `priority`, `failoverSettings`) |

#### GET .../services/replication/links -- List Replication Links

Retrieves a list of replication links.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links` |
| **Response** | `List` |

#### GET .../services/replication/links/{link-name} -- Get Replication Link

Retrieves information about a specific replication link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}` |
| **Response** | `link` |

#### HEAD .../services/replication/links/{link-name} -- Check Link Existence

Checks whether a specific replication link exists.

| Detail | Value |
|--------|-------|
| **Method** | `HEAD` |
| **Path** | `.../services/replication/links/{link-name}` |

#### POST .../services/replication/links/{link-name} -- Modify or Act on Link

Modifies a replication link or performs actions (failover, failback, recovery).

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../services/replication/links/{link-name}` |
| **Query Params** | `?failOver`, `?failBack`, `?suspend`, `?resume`, `?beginRecovery`, `?completeRecovery` |

#### DELETE .../services/replication/links/{link-name} -- Delete Replication Link

Deletes a replication link.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../services/replication/links/{link-name}` |

#### GET .../services/replication/links/{link-name}/content -- List Link Content

Retrieves the list of items included in a replication link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/content` |
| **Response** | `content` |

#### GET .../services/replication/links/{link-name}/content/defaultNamespaceDirectories -- List Link Directories

Retrieves default-namespace directories included in a link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/content/defaultNamespaceDirectories` |
| **Notes** | Available only if default tenant exists. |

#### PUT .../services/replication/links/{link-name}/content/defaultNamespaceDirectories/{directory-name} -- Add Directory to Link

Adds a default-namespace directory to a replication link.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../services/replication/links/{link-name}/content/defaultNamespaceDirectories/{directory-name}` |

#### DELETE .../services/replication/links/{link-name}/content/defaultNamespaceDirectories/{directory-name} -- Remove Directory from Link

Removes a default-namespace directory from a replication link.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../services/replication/links/{link-name}/content/defaultNamespaceDirectories/{directory-name}` |

#### GET .../services/replication/links/{link-name}/content/chainedLinks -- List Chained Links

Retrieves the list of chained links in an active/passive replication link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/content/chainedLinks` |
| **Notes** | Not valid for active/active links. |

#### PUT .../services/replication/links/{link-name}/content/chainedLinks/{chained-link-name} -- Add Chained Link

Adds a chained link to an active/passive replication link.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../services/replication/links/{link-name}/content/chainedLinks/{chained-link-name}` |
| **Notes** | Cannot add to active/active links. |

#### DELETE .../services/replication/links/{link-name}/content/chainedLinks/{chained-link-name} -- Remove Chained Link

Removes a chained link from an active/passive replication link.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../services/replication/links/{link-name}/content/chainedLinks/{chained-link-name}` |

#### GET .../services/replication/links/{link-name}/content/tenants -- List Link Tenants

Retrieves the list of HCP tenants included in a replication link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/content/tenants` |

#### PUT .../services/replication/links/{link-name}/content/tenants/{tenant-name} -- Add Tenant to Link

Adds an HCP tenant to a replication link.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../services/replication/links/{link-name}/content/tenants/{tenant-name}` |

#### GET .../services/replication/links/{link-name}/content/tenants/{tenant-name} -- Get Tenant Replication Status

Retrieves replication status for a tenant within a link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/content/tenants/{tenant-name}` |

#### POST .../services/replication/links/{link-name}/content/tenants/{tenant-name} -- Act on Tenant in Link

Performs an action on a tenant within a replication link.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../services/replication/links/{link-name}/content/tenants/{tenant-name}` |

#### DELETE .../services/replication/links/{link-name}/content/tenants/{tenant-name} -- Remove Tenant from Link

Removes an HCP tenant from a replication link.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../services/replication/links/{link-name}/content/tenants/{tenant-name}` |

#### GET .../services/replication/links/{link-name}/localCandidates -- List Local Candidates

Retrieves the list of local items eligible for inclusion in a replication link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/localCandidates` |
| **Query Params** | `prettyprint` |
| **Response** | `content` |

#### GET .../services/replication/links/{link-name}/localCandidates/defaultNamespaceDirectories -- List Local Directory Candidates

Retrieves local default-namespace directories eligible for a link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/localCandidates/defaultNamespaceDirectories` |

#### GET .../services/replication/links/{link-name}/localCandidates/chainedLinks -- List Local Chained Link Candidates

Retrieves local inbound links eligible for chaining in an active/passive link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/localCandidates/chainedLinks` |

#### GET .../services/replication/links/{link-name}/localCandidates/tenants -- List Local Tenant Candidates

Retrieves local tenants eligible for inclusion in a link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/localCandidates/tenants` |

#### GET .../services/replication/links/{link-name}/remoteCandidates -- List Remote Candidates

Retrieves remote items eligible for inclusion in a replication link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/remoteCandidates` |
| **Response** | `content` |

#### GET .../services/replication/links/{link-name}/remoteCandidates/defaultNamespaceDirectories -- List Remote Directory Candidates

Retrieves remote default-namespace directories eligible for a link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/remoteCandidates/defaultNamespaceDirectories` |

#### GET .../services/replication/links/{link-name}/remoteCandidates/chainedLinks -- List Remote Chained Link Candidates

Retrieves remote inbound links eligible for chaining.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/remoteCandidates/chainedLinks` |

#### GET .../services/replication/links/{link-name}/remoteCandidates/tenants -- List Remote Tenant Candidates

Retrieves remote tenants eligible for inclusion in a link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/remoteCandidates/tenants` |

#### GET .../services/replication/links/{link-name}/schedule -- Get Link Schedule

Retrieves replication schedules for a link.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/replication/links/{link-name}/schedule` |
| **Response** | `schedule` |

#### POST .../services/replication/links/{link-name}/schedule -- Modify Link Schedule

Modifies replication schedules for a link.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../services/replication/links/{link-name}/schedule` |
| **Request Body** | `schedule` (local/remote with transitions: `time`, `performanceLevel` `HIGH`/`MEDIUM`/`LOW`) |

---

### 2.10 Retention Classes

Resources for managing retention classes within a namespace.

#### PUT .../tenants/{tenant-name}/namespaces/{namespace-name}/retentionClasses -- Create Retention Class

Creates a retention class for a namespace.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/retentionClasses` |
| **Request Body** | `retentionClass` (`name`, `description`, `value` e.g. `A+10y`, `allowDisposition`) |
| **Access** | Compliance role |

#### GET .../tenants/{tenant-name}/namespaces/{namespace-name}/retentionClasses -- List Retention Classes

Retrieves a list of retention classes for a namespace.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/retentionClasses` |
| **Query Params** | `prettyprint` |

#### GET .../tenants/{tenant-name}/namespaces/{namespace-name}/retentionClasses/{retention-class-name} -- Get Retention Class

Retrieves information about a specific retention class.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/retentionClasses/{retention-class-name}` |

#### HEAD .../tenants/{tenant-name}/namespaces/{namespace-name}/retentionClasses/{retention-class-name} -- Check Retention Class Existence

Checks whether a specific retention class exists.

| Detail | Value |
|--------|-------|
| **Method** | `HEAD` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/retentionClasses/{retention-class-name}` |

#### POST .../tenants/{tenant-name}/namespaces/{namespace-name}/retentionClasses/{retention-class-name} -- Modify Retention Class

Modifies a retention class.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/retentionClasses/{retention-class-name}` |
| **Request Body** | `retentionClass` |

#### DELETE .../tenants/{tenant-name}/namespaces/{namespace-name}/retentionClasses/{retention-class-name} -- Delete Retention Class

Deletes a retention class. Only valid if namespace is in enterprise mode.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../tenants/{tenant-name}/namespaces/{namespace-name}/retentionClasses/{retention-class-name}` |

---

### 2.11 Service Statistics

Resources for retrieving statistics of HCP services.

#### GET .../services/statistics -- Get Service Statistics

Retrieves statistics of services used by the HCP system (per-service state, objects examined/serviced/unable).

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../services/statistics` |
| **Query Params** | `prettyprint` |
| **Response** | `serviceStatistics` |
| **Access** | System-level admin/monitor |
| **Notes** | Best practice: poll no more than once per hour. |

---

### 2.12 Support Access Credentials

Resources for managing Hitachi Vantara Support access credentials.

#### GET .../supportaccesscredentials -- Get Support Credentials

Retrieves the currently configured Support access credentials.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../supportaccesscredentials` |
| **Query Params** | `prettyprint` |
| **Response** | `supportAccessCredentials` |
| **Access** | System-level monitor/admin/security/service |

#### PUT .../supportaccesscredentials -- Upload Support Credentials

Uploads an exclusive Hitachi Vantara Support access credentials package.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../supportaccesscredentials` |
| **Request Body** | SSH key package file (`.plk`) |
| **Access** | System-level admin/service |

---

### 2.13 System-Level Group Accounts

Resources for viewing system-level group accounts.

#### GET .../groupAccounts -- List System Group Accounts

Retrieves a list of system-level group accounts.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../groupAccounts` |
| **Query Params** | `verbose`, `prettyprint` |
| **Access** | System-level monitor/admin/security |

#### GET .../groupAccounts/{group-name} -- Get System Group Account

Retrieves information about a specific system-level group account.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../groupAccounts/{group-name}` |
| **Query Params** | `verbose`, `prettyprint` |

#### HEAD .../groupAccounts/{group-name} -- Check System Group Existence

Checks whether a specific system-level group account exists.

| Detail | Value |
|--------|-------|
| **Method** | `HEAD` |
| **Path** | `.../groupAccounts/{group-name}` |

---

### 2.14 System-Level User Accounts

Resources for managing system-level user accounts.

#### GET .../userAccounts -- List System User Accounts

Retrieves a list of system-level user accounts.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../userAccounts` |
| **Query Params** | `verbose`, `prettyprint` |
| **Access** | System-level monitor/admin/security |

#### GET .../userAccounts/{username} -- Get System User Account

Retrieves information about a specific system-level user account.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../userAccounts/{username}` |
| **Query Params** | `verbose`, `prettyprint` |

#### HEAD .../userAccounts/{username} -- Check System User Existence

Checks whether a specific system-level user account exists.

| Detail | Value |
|--------|-------|
| **Method** | `HEAD` |
| **Path** | `.../userAccounts/{username}` |

#### POST .../userAccounts/{username} -- Change System User Password (Query Param)

Changes the password for a locally authenticated system-level user.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../userAccounts/{username}` |
| **Query Params** | `?password=<new-password>` |
| **Access** | Security role |

#### POST .../userAccounts/{username}/changePassword -- Change System User Password (Request Body)

Changes the password for a system-level user using request body.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../userAccounts/{username}/changePassword` |
| **Request Body** | `updatePasswordRequest` (`newPassword`) |
| **Access** | Security role |

---

### 2.15 Tenants

Resources for managing HCP tenants and their configuration.

#### PUT .../tenants -- Create a Tenant

Creates an HCP tenant with an initial user or group account.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../tenants` |
| **Query Params** | `username`, `password`, `forcePasswordChange` (for initial user account) OR `groupname` (for initial AD group account) |
| **Request Body** | `tenant` (XML/JSON) with `name`, `hardQuota`, `softQuota`, `namespaceQuota`, `authenticationTypes`, `servicePlan`, `tags`, etc. |

#### GET .../tenants/{tenant-name} -- Get Tenant

Retrieves information about a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}` |
| **Response** | `tenant` |

#### HEAD .../tenants/{tenant-name} -- Check Tenant Existence

Checks whether a specific tenant exists.

| Detail | Value |
|--------|-------|
| **Method** | `HEAD` |
| **Path** | `.../tenants/{tenant-name}` |

#### POST .../tenants/{tenant-name} -- Modify Tenant

Modifies a tenant's properties.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}` |
| **Request Body** | `tenant` (XML/JSON) |

#### GET .../tenants/{tenant-name}/availableServicePlans -- List Available Service Plans

Retrieves the list of service plans available for the tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/availableServicePlans` |
| **Notes** | Only valid if tenant allows service plan selection. |

#### GET .../tenants/{tenant-name}/availableServicePlans/{service-plan-name} -- Get Service Plan

Retrieves information about a specific available service plan.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/availableServicePlans/{service-plan-name}` |
| **Response** | `availableServicePlan` |

#### GET .../tenants/{tenant-name}/chargebackReport -- Tenant Chargeback Report

Generates a chargeback report for a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/chargebackReport` |
| **Query Params** | `start` (ISO 8601), `end` (ISO 8601), `granularity` (`hour`, `day`, `total`) |
| **Response Formats** | XML, JSON, CSV |

#### GET .../tenants/{tenant-name}/consoleSecurity -- Get Console Security Config

Retrieves the Tenant Management Console configuration.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/consoleSecurity` |
| **Response** | `consoleSecurity` |
| **Access** | Security role |

#### POST .../tenants/{tenant-name}/consoleSecurity -- Modify Console Security Config

Modifies the Tenant Management Console configuration.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/consoleSecurity` |
| **Request Body** | `consoleSecurity` (password policies, IP settings, login message, timeout, etc.) |

#### GET .../tenants/{tenant-name}/contactInfo -- Get Tenant Contact Info

Retrieves contact information for a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/contactInfo` |
| **Response** | `contactInfo` |

#### POST .../tenants/{tenant-name}/contactInfo -- Modify Tenant Contact Info

Modifies contact information for a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/contactInfo` |
| **Request Body** | `contactInfo` (`firstName`, `lastName`, `emailAddress`, `phone`, `address`, etc.) |

#### PUT .../tenants/{tenant-name}/cors -- Set Tenant Default CORS

Sets default CORS rules for all tenant namespaces.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../tenants/{tenant-name}/cors` |
| **Request Body** | `cors` (XML/JSON) |

#### GET .../tenants/{tenant-name}/cors -- Get Tenant Default CORS

Retrieves default CORS configuration for the tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/cors` |

#### DELETE .../tenants/{tenant-name}/cors -- Delete Tenant Default CORS

Deletes default CORS configuration for the tenant. Returns `404` if not set.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../tenants/{tenant-name}/cors` |

#### GET .../tenants/{tenant-name}/emailNotification -- Get Email Notification Config

Retrieves email notification configuration for a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/emailNotification` |
| **Response** | `emailNotification` |

#### POST .../tenants/{tenant-name}/emailNotification -- Modify Email Notification Config

Modifies email notification configuration.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/emailNotification` |
| **Request Body** | `emailNotification` (`recipients`, `emailTemplate`, `smtpServer`, etc.) |

#### GET .../tenants/{tenant-name}/namespaceDefaults -- Get Namespace Defaults

Retrieves default settings for namespace creation. Not valid for default tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/namespaceDefaults` |
| **Response** | `namespaceDefaults` |

#### POST .../tenants/{tenant-name}/namespaceDefaults -- Modify Namespace Defaults

Modifies default settings for namespace creation.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/namespaceDefaults` |
| **Request Body** | `namespaceDefaults` |

#### GET .../tenants/{tenant-name}/permissions -- Get Tenant Permissions

Retrieves the data access permission mask for a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/permissions` |

#### POST .../tenants/{tenant-name}/permissions -- Modify Tenant Permissions

Modifies the data access permission mask for a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/permissions` |
| **Request Body** | List of permissions: `DELETE`, `PRIVILEGED`, `PURGE`, `READ`, `SEARCH`, `WRITE` |

#### GET .../tenants/{tenant-name}/searchSecurity -- Get Search Console Config

Retrieves the Search Console configuration for a tenant. Not valid for default tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/searchSecurity` |
| **Response** | `searchSecurity` |

#### POST .../tenants/{tenant-name}/searchSecurity -- Modify Search Console Config

Modifies the Search Console configuration.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/searchSecurity` |
| **Request Body** | `searchSecurity` |

#### GET .../tenants/{tenant-name}/statistics -- Get Tenant Statistics

Retrieves statistics about content of namespaces owned by the tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/statistics` |
| **Response** | `statistics` |

---

### 2.16 Tenant-Level Group Accounts

Resources for managing group accounts within a tenant.

#### PUT .../tenants/{tenant-name}/groupAccounts -- Create Group Account

Creates a group account for a tenant. Requires Active Directory.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../tenants/{tenant-name}/groupAccounts` |
| **Request Body** | `groupAccount` (`groupname`, `roles`) |
| **Access** | Security role |

#### GET .../tenants/{tenant-name}/groupAccounts -- List Group Accounts

Retrieves a list of group accounts for a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/groupAccounts` |
| **Query Params** | `prettyprint` |

#### GET .../tenants/{tenant-name}/groupAccounts/{group-name} -- Get Group Account

Retrieves information about a specific group account.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/groupAccounts/{group-name}` |

#### HEAD .../tenants/{tenant-name}/groupAccounts/{group-name} -- Check Group Existence

Checks whether a specific group account exists.

| Detail | Value |
|--------|-------|
| **Method** | `HEAD` |
| **Path** | `.../tenants/{tenant-name}/groupAccounts/{group-name}` |

#### POST .../tenants/{tenant-name}/groupAccounts/{group-name} -- Modify Group Account

Modifies a group account.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/groupAccounts/{group-name}` |
| **Request Body** | `groupAccount` (`roles`, `allowNamespaceManagement`) |
| **Notes** | Admin can modify only `allowNamespaceManagement`; security role cannot modify that property. |

#### DELETE .../tenants/{tenant-name}/groupAccounts/{group-name} -- Delete Group Account

Deletes a group account.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../tenants/{tenant-name}/groupAccounts/{group-name}` |

#### GET .../tenants/{tenant-name}/groupAccounts/{group-name}/dataAccessPermissions -- Get Group Data Access Permissions

Retrieves data access permissions for a group account.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/groupAccounts/{group-name}/dataAccessPermissions` |
| **Query Params** | `offset`, `count`, `sortType`, `sortOrder`, `filterType`, `filterString` |

#### POST .../tenants/{tenant-name}/groupAccounts/{group-name}/dataAccessPermissions -- Modify Group Data Access Permissions

Modifies data access permissions for a group account.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/groupAccounts/{group-name}/dataAccessPermissions` |
| **Request Body** | `dataAccessPermissions` (namespacePermission entries with per-namespace permissions) |
| **Access** | Admin role |
| **Notes** | Must include all permissions for each namespace in the body. |

---

### 2.17 Tenant-Level User Accounts

Resources for managing user accounts within a tenant.

#### PUT .../tenants/{tenant-name}/userAccounts -- Create User Account

Creates a user account for a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `PUT` |
| **Path** | `.../tenants/{tenant-name}/userAccounts` |
| **Query Params** | `password` |
| **Request Body** | `userAccount` (`username`, `fullName`, `roles`, `localAuthentication`, `enabled`, etc.) |
| **Access** | Security role |

#### GET .../tenants/{tenant-name}/userAccounts -- List User Accounts

Retrieves a list of user accounts for a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/userAccounts` |
| **Query Params** | `offset`, `count`, `sortType` (`username`), `sortOrder`, `filterType` (`username`), `filterString` |

#### POST .../tenants/{tenant-name}/userAccounts?resetPasswords -- Reset All Passwords

Resets passwords for all security-role users in a tenant.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/userAccounts` |
| **Query Params** | `?resetPasswords=<new-password>` |

#### GET .../tenants/{tenant-name}/userAccounts/{username} -- Get User Account

Retrieves information about a specific user account.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/userAccounts/{username}` |

#### HEAD .../tenants/{tenant-name}/userAccounts/{username} -- Check User Existence

Checks whether a specific user account exists.

| Detail | Value |
|--------|-------|
| **Method** | `HEAD` |
| **Path** | `.../tenants/{tenant-name}/userAccounts/{username}` |

#### POST .../tenants/{tenant-name}/userAccounts/{username} -- Modify User Account

Modifies a user account (roles, properties) or changes password via query param.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/userAccounts/{username}` |
| **Query Params** | `?password=<new-password>` (optional, for password change) |
| **Request Body** | `userAccount` (for property changes) |
| **Notes** | Admin can modify only `allowNamespaceManagement`; security role cannot modify that property. |

#### DELETE .../tenants/{tenant-name}/userAccounts/{username} -- Delete User Account

Deletes a user account.

| Detail | Value |
|--------|-------|
| **Method** | `DELETE` |
| **Path** | `.../tenants/{tenant-name}/userAccounts/{username}` |

#### POST .../tenants/{tenant-name}/userAccounts/{username}/changePassword -- Change User Password

Changes password for a tenant-level user using request body.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/userAccounts/{username}/changePassword` |
| **Request Body** | `updatePasswordRequest` (`newPassword`) |
| **Access** | System-level security or tenant-level security |

#### GET .../tenants/{tenant-name}/userAccounts/{username}/dataAccessPermissions -- Get User Data Access Permissions

Retrieves data access permissions for a user account.

| Detail | Value |
|--------|-------|
| **Method** | `GET` |
| **Path** | `.../tenants/{tenant-name}/userAccounts/{username}/dataAccessPermissions` |
| **Query Params** | `offset`, `count`, `sortType`, `sortOrder`, `filterType`, `filterString` |

#### POST .../tenants/{tenant-name}/userAccounts/{username}/dataAccessPermissions -- Modify User Data Access Permissions

Modifies data access permissions for a user account.

| Detail | Value |
|--------|-------|
| **Method** | `POST` |
| **Path** | `.../tenants/{tenant-name}/userAccounts/{username}/dataAccessPermissions` |
| **Request Body** | `dataAccessPermissions` (namespacePermission entries with per-namespace permissions: `READ`, `BROWSE`, `WRITE`, `DELETE`, `PURGE`, `SEARCH`) |
| **Notes** | Must include all permissions for each namespace in the body. |

---

### 2.18 Additional MAPI Features

#### Template Generation

Any PUT endpoint supports `?template` query parameter to return a blank XML/JSON template for resource creation, useful for discovering the required and optional fields.

#### XML Schema

The MAPI XML schema is retrievable at: `https://(tenant-name).hcp-domain-name:9090/static/mapi-9_3_0.xsd`

#### Session Cookie Authentication

Alternative to the `Authorization` header, use: `Cookie: hcp-api-auth=base64(username):md5(password)` or `HCAP-Login` cookie after AD login.

#### Partial Updates

POST (modify) operations support partial updates -- you only need to include the properties you want to change in the request body.

---

## Summary

| API | Categories | Total Operations |
|-----|-----------|-----------------|
| **S3-Compatible API** | 5 categories | **31** |
| **HCP Management API** | 17 categories + extras | **~120+** |
| **Combined Total** | | **~150+** |
