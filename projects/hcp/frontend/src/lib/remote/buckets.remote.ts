import { command, query } from "$app/server";
import { error } from "@sveltejs/kit";
import { z } from "zod";
import { apiFetch, throwIfNotOk } from "$lib/server/api.js";

export const get_buckets = query(async () => {
  try {
    const res = await apiFetch("/api/v1/buckets");
    if (res.ok) {
      const data = await res.json();
      const buckets = (data.buckets ?? []).map((
        b: Record<string, unknown>,
      ) => ({
        name: b.name ?? b.Name ?? "",
        creation_date: b.creation_date ?? b.CreationDate ?? "",
      }));
      return { buckets, owner: data.owner ?? "" };
    }
  } catch (err) {
    console.error("[buckets.remote]", err);
  }
  return {
    buckets: [] as { name: string; creation_date: string }[],
    owner: "",
  };
});

export const get_objects = query(
  z.object({
    bucket: z.string(),
    prefix: z.string(),
    continuation_token: z.string().optional(),
    flat: z.boolean().optional(),
    max_keys: z.number().optional(),
  }),
  async ({ bucket, prefix, continuation_token, flat, max_keys }) => {
    try {
      const params = new URLSearchParams({
        max_keys: String(max_keys ?? 1000),
      });
      if (!flat) params.set("delimiter", "/");
      if (prefix) params.set("prefix", prefix);
      if (continuation_token) {
        params.set("continuation_token", continuation_token);
      }
      const res = await apiFetch(
        `/api/v1/buckets/${encodeURIComponent(bucket)}/objects?${params}`,
      );
      if (res.ok) {
        const data = await res.json();
        const objects = (data.objects ?? []).map((
          o: Record<string, unknown>,
        ) => ({
          key: o.key ?? o.Key ?? "",
          size: o.size ?? o.Size ?? 0,
          last_modified: o.last_modified ?? o.LastModified ?? "",
          etag: o.etag ?? o.ETag ?? "",
          storage_class: o.storage_class ?? o.StorageClass ?? "",
          owner: (o.owner ?? o.Owner ?? null) as {
            display_name?: string;
            id?: string;
            DisplayName?: string;
            ID?: string;
          } | null,
        }));
        const commonPrefixes = (data.common_prefixes ?? []) as string[];
        return {
          objects,
          commonPrefixes,
          isTruncated: data.is_truncated ?? false,
          nextToken: data.next_continuation_token ?? null,
          keyCount: data.key_count ?? 0,
        };
      }
      if (res.status === 403) {
        return {
          objects: [],
          commonPrefixes: [] as string[],
          isTruncated: false,
          keyCount: 0,
          nextToken: null,
          error:
            "Access Denied — you don't have permission to list objects in this namespace." as
              | string
              | null,
        };
      }
    } catch (err) {
      console.error("[buckets.remote]", err);
    }
    return {
      objects: [],
      commonPrefixes: [] as string[],
      isTruncated: false,
      keyCount: 0,
      nextToken: null,
      error: null as string | null,
    };
  },
);

export const start_s3_stats = command(
  z.object({
    bucket: z.string(),
    prefix: z.string().optional(),
    delimiter: z.string().optional(),
  }),
  async ({ bucket, prefix, delimiter }) => {
    const params = new URLSearchParams();
    if (prefix) params.set("prefix", prefix);
    if (delimiter) params.set("delimiter", delimiter);
    const qs = params.toString();
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/s3_stats${qs ? `?${qs}` : ""}`,
      { method: "POST" },
    );
    await throwIfNotOk(res, "Failed to start object count");
    const data = await res.json();
    return {
      task_id: data.task_id as string,
      status: data.status as string,
    };
  },
);

export const create_bucket = command(
  z.object({ bucket: z.string() }),
  async ({ bucket }) => {
    const res = await apiFetch("/api/v1/buckets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bucket }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({
        detail: `Failed to create bucket "${bucket}"`,
      }));
      const raw = typeof err.detail === "string"
        ? err.detail
        : JSON.stringify(err.detail);
      // Extract a user-friendly message from backend errors
      let message = raw;
      if (/invalid namespace name/i.test(raw)) {
        message =
          `Invalid bucket name "${bucket}". Bucket names must use only lowercase letters, numbers, hyphens, and dots (no underscores or spaces). Must be 3–63 characters long.`;
      }
      return { error: message };
    }
    return { error: null as string | null };
  },
);

export const delete_bucket = command(
  z.object({ bucket: z.string(), force: z.boolean().optional() }),
  async ({ bucket, force }) => {
    const qs = force ? "?force=true" : "";
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}${qs}`,
      { method: "DELETE" },
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({
        detail: `Failed to delete bucket "${bucket}"`,
      }));
      const detail = typeof err.detail === "string"
        ? err.detail
        : JSON.stringify(err.detail);
      error(res.status, detail);
    }
  },
);

export const delete_object = command(
  z.object({ bucket: z.string(), key: z.string() }),
  async ({ bucket, key }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/${
        encodeURIComponent(key)
      }`,
      { method: "DELETE" },
    );
    await throwIfNotOk(res, "Failed to delete object");
  },
);

export const bulk_delete_objects = command(
  z.object({ bucket: z.string(), keys: z.array(z.string()) }),
  async ({ bucket, keys }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/delete`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys }),
      },
    );
    await throwIfNotOk(res, "Failed to delete objects");
    const data = await res.json();
    if (data.errors && data.errors.length > 0) {
      error(
        500,
        `Failed to delete ${data.errors.length} of ${keys.length} objects`,
      );
    }
  },
);

export const get_s3_credentials = query(async () => {
  try {
    const res = await apiFetch("/api/v1/credentials");
    if (res.ok) {
      const data = await res.json();
      return {
        access_key_id: (data.access_key_id ?? "") as string,
        secret_access_key: (data.secret_access_key ?? "") as string,
        username: (data.username ?? "") as string,
        endpoint_url: (data.endpoint_url ?? "") as string,
      };
    }
  } catch (err) {
    console.error("[buckets.remote]", err);
  }
  return {
    access_key_id: "",
    secret_access_key: "",
    username: "",
    endpoint_url: "",
  };
});

export const bulk_presign = command(
  z.object({
    bucket: z.string(),
    keys: z.array(z.string()).min(1),
    expires_in: z.number().min(1).max(604800).default(3600),
  }),
  async ({ bucket, keys, expires_in }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/presign`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys, expires_in }),
      },
    );
    await throwIfNotOk(res, "Failed to generate presigned URLs");
    const data = await res.json();
    return {
      urls: (data.urls ?? []) as { key: string; url: string }[],
      expires_in: (data.expires_in ?? expires_in) as number,
    };
  },
);

export const generate_presigned_url = command(
  z.object({
    bucket: z.string(),
    key: z.string(),
    expires_in: z.number().min(1).max(604800),
    method: z.enum(["get_object", "put_object"]),
  }),
  async ({ bucket, key, expires_in, method }) => {
    const res = await apiFetch("/api/v1/presign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bucket, key, expires_in, method }),
    });
    await throwIfNotOk(res, "Failed to generate presigned URL");
    const data = await res.json();
    return {
      url: data.url as string,
      bucket: data.bucket as string,
      key: data.key as string,
      expires_in: data.expires_in as number,
      method: data.method as string,
    };
  },
);

// ── ZIP Download Task ───────────────────────────────────────────────

export const start_zip_download = command(
  z.object({
    bucket: z.string(),
    prefix: z.string().optional(),
    keys: z.array(z.string()).optional(),
  }),
  async ({ bucket, prefix, keys }) => {
    const body: Record<string, unknown> = {};
    if (prefix != null) body.prefix = prefix;
    if (keys && keys.length > 0) body.keys = keys;
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/download`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({
        detail: "Failed to start ZIP download",
      }));
      error(
        res.status,
        typeof err.detail === "string" ? err.detail : JSON.stringify(
          err.detail,
        ),
      );
    }
    return (await res.json()) as {
      task_id: string;
      status: string;
      total: number;
    };
  },
);

export const poll_zip_download = query(
  z.object({ bucket: z.string(), task_id: z.string() }),
  async ({ bucket, task_id }) => {
    try {
      const res = await apiFetch(
        `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/download/${
          encodeURIComponent(task_id)
        }`,
      );
      if (res.ok) {
        const contentType = res.headers.get("content-type") ?? "";
        if (contentType.includes("application/json")) {
          return (await res.json()) as {
            task_id: string;
            status: string;
            total: number;
            completed: number;
            failed: number;
            failed_keys: string[];
          };
        }
        // If it's a ZIP file, the task is ready
        return {
          task_id,
          status: "ready" as string,
          total: 0,
          completed: 0,
          failed: 0,
          failed_keys: [] as string[],
        };
      }
    } catch (err) {
      console.error("[buckets.remote] poll_zip_download", err);
    }
    return null;
  },
);

// ── Head Object ─────────────────────────────────────────────────────

export const head_object = query(
  z.object({ bucket: z.string(), key: z.string() }),
  async ({ bucket, key }) => {
    try {
      const res = await apiFetch(
        `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/${
          encodeURIComponent(key)
        }`,
        { method: "HEAD" },
      );
      if (res.ok) {
        return {
          content_length: Number(res.headers.get("content-length") ?? 0),
          content_type: res.headers.get("content-type") ?? "",
          etag: res.headers.get("etag") ?? "",
          last_modified: res.headers.get("last-modified") ?? "",
        };
      }
    } catch (err) {
      console.error("[buckets.remote] head_object", err);
    }
    return null;
  },
);

// ── Copy Object ─────────────────────────────────────────────────────

export const copy_object = command(
  z.object({
    bucket: z.string(),
    key: z.string(),
    source_bucket: z.string(),
    source_key: z.string(),
  }),
  async ({ bucket, key, source_bucket, source_key }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/${
        encodeURIComponent(key)
      }/copy`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_bucket, source_key }),
      },
    );
    await throwIfNotOk(res, "Failed to copy object");
    return await res.json();
  },
);

// ── Bucket Versioning ───────────────────────────────────────────────

export type BucketVersioning = {
  status: string | null;
  mfa_delete: string | null;
  error?: string | null;
};

export const get_bucket_versioning = query(
  z.object({ bucket: z.string() }),
  async ({ bucket }) => {
    try {
      const res = await apiFetch(
        `/api/v1/buckets/${encodeURIComponent(bucket)}/versioning`,
      );
      if (res.ok) {
        const data = await res.json();
        return {
          status: (data.status ?? null) as string | null,
          mfa_delete: (data.mfa_delete ?? null) as string | null,
          error: null,
        };
      }
      if (res.status === 403) {
        return {
          status: null,
          mfa_delete: null,
          error: "Access Denied — insufficient permissions.",
        };
      }
    } catch (err) {
      console.error("[buckets.remote] get_bucket_versioning", err);
    }
    return { status: null, mfa_delete: null, error: null } as BucketVersioning;
  },
);

export const set_bucket_versioning = command(
  z.object({
    bucket: z.string(),
    status: z.enum(["Enabled", "Suspended"]),
  }),
  async ({ bucket, status }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/versioning`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      },
    );
    await throwIfNotOk(res, "Failed to update versioning");
    return await res.json();
  },
);

// ── Bucket ACL ──────────────────────────────────────────────────────

export type AclOwner = {
  ID?: string;
  DisplayName?: string;
};

export type AclGrant = {
  Grantee?: Record<string, unknown>;
  Permission?: string;
};

export type AclData = {
  owner: AclOwner | null;
  grants: AclGrant[];
  error?: string | null;
};

export const get_bucket_acl = query(
  z.object({ bucket: z.string() }),
  async ({ bucket }) => {
    try {
      const res = await apiFetch(
        `/api/v1/buckets/${encodeURIComponent(bucket)}/acl`,
      );
      if (res.ok) {
        const data = await res.json();
        return {
          owner: (data.owner ?? null) as AclOwner | null,
          grants: (data.grants ?? []) as AclGrant[],
          error: null,
        };
      }
      if (res.status === 403) {
        return {
          owner: null,
          grants: [] as AclGrant[],
          error:
            "Access Denied — you don't have READ_ACL permission on this namespace.",
        };
      }
      return {
        owner: null,
        grants: [] as AclGrant[],
        error: `Failed to load ACL (HTTP ${res.status})`,
      };
    } catch (err) {
      console.error("[buckets.remote] get_bucket_acl", err);
    }
    return {
      owner: null,
      grants: [] as AclGrant[],
      error: "Failed to load ACL",
    };
  },
);

export const put_bucket_acl = command(
  z.object({
    bucket: z.string(),
    owner: z.record(z.string(), z.unknown()).optional(),
    grants: z.array(z.record(z.string(), z.unknown())),
  }),
  async ({ bucket, owner, grants }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/acl`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ Owner: owner, Grants: grants }),
      },
    );
    await throwIfNotOk(res, "Failed to update bucket ACL");
    return await res.json();
  },
);

// ── Object ACL ──────────────────────────────────────────────────────

export const get_object_acl = query(
  z.object({ bucket: z.string(), key: z.string() }),
  async ({ bucket, key }) => {
    try {
      const res = await apiFetch(
        `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/${
          encodeURIComponent(key)
        }/acl`,
      );
      if (res.ok) {
        const data = await res.json();
        return {
          owner: (data.owner ?? null) as AclOwner | null,
          grants: (data.grants ?? []) as AclGrant[],
        };
      }
    } catch (err) {
      console.error("[buckets.remote] get_object_acl", err);
    }
    return { owner: null, grants: [] } as AclData;
  },
);

export const put_object_acl = command(
  z.object({
    bucket: z.string(),
    key: z.string(),
    owner: z.record(z.string(), z.unknown()).optional(),
    grants: z.array(z.record(z.string(), z.unknown())),
  }),
  async ({ bucket, key, owner, grants }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/${
        encodeURIComponent(key)
      }/acl`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ Owner: owner, Grants: grants }),
      },
    );
    await throwIfNotOk(res, "Failed to update object ACL");
    return await res.json();
  },
);

// ── Object Versions ────────────────────────────────────────────────

export type ObjectVersion = {
  Key: string;
  VersionId: string | null;
  IsLatest: boolean | null;
  LastModified: string | null;
  ETag: string | null;
  Size: number | null;
  StorageClass: string | null;
  Owner: { DisplayName?: string; ID?: string } | null;
};

export type DeleteMarker = {
  Key: string;
  VersionId: string | null;
  IsLatest: boolean | null;
  LastModified: string | null;
  Owner: { DisplayName?: string; ID?: string } | null;
};

export const get_object_versions = query(
  z.object({
    bucket: z.string(),
    prefix: z.string().optional(),
    max_keys: z.number().optional(),
    key_marker: z.string().optional(),
    version_id_marker: z.string().optional(),
  }),
  async ({ bucket, prefix, max_keys, key_marker, version_id_marker }) => {
    try {
      const params = new URLSearchParams();
      if (prefix) params.set("prefix", prefix);
      if (max_keys) params.set("max_keys", String(max_keys));
      if (key_marker) params.set("key_marker", key_marker);
      if (version_id_marker) {
        params.set("version_id_marker", version_id_marker);
      }
      const qs = params.toString();
      const res = await apiFetch(
        `/api/v1/buckets/${encodeURIComponent(bucket)}/versions${
          qs ? `?${qs}` : ""
        }`,
      );
      if (res.ok) {
        const data = await res.json();
        return {
          versions: (data.versions ?? []) as ObjectVersion[],
          delete_markers: (data.delete_markers ?? []) as DeleteMarker[],
          is_truncated: (data.is_truncated ?? false) as boolean,
          next_key_marker: (data.next_key_marker ?? null) as string | null,
          next_version_id_marker: (data.next_version_id_marker ?? null) as
            | string
            | null,
          key_count: (data.key_count ?? 0) as number,
          error: null as string | null,
        };
      }
      if (res.status === 403) {
        return {
          versions: [] as ObjectVersion[],
          delete_markers: [] as DeleteMarker[],
          is_truncated: false,
          next_key_marker: null,
          next_version_id_marker: null,
          key_count: 0,
          error: "Access Denied — insufficient permissions.",
        };
      }
    } catch (err) {
      console.error("[buckets.remote] get_object_versions", err);
    }
    return {
      versions: [] as ObjectVersion[],
      delete_markers: [] as DeleteMarker[],
      is_truncated: false,
      next_key_marker: null as string | null,
      next_version_id_marker: null as string | null,
      key_count: 0,
      error: null as string | null,
    };
  },
);

export const delete_object_version = command(
  z.object({
    bucket: z.string(),
    key: z.string(),
    version_id: z.string(),
  }),
  async ({ bucket, key, version_id }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/${
        encodeURIComponent(key)
      }?version_id=${encodeURIComponent(version_id)}`,
      { method: "DELETE" },
    );
    await throwIfNotOk(res, "Failed to delete version");
  },
);

// ── Bucket CORS ────────────────────────────────────────────────────

export type CorsRule = {
  AllowedHeaders?: string[];
  AllowedMethods?: string[];
  AllowedOrigins?: string[];
  ExposeHeaders?: string[];
  MaxAgeSeconds?: number;
};

export const get_bucket_cors = query(
  z.object({ bucket: z.string() }),
  async ({ bucket }) => {
    try {
      const res = await apiFetch(
        `/api/v1/buckets/${encodeURIComponent(bucket)}/cors`,
      );
      if (res.ok) {
        const data = await res.json();
        return {
          cors_rules: (data.cors_rules ?? []) as CorsRule[],
          error: null as string | null,
        };
      }
      if (res.status === 404) {
        return { cors_rules: [] as CorsRule[], error: null as string | null };
      }
      if (res.status === 501) {
        return {
          cors_rules: [] as CorsRule[],
          error: "CORS management is not supported on this storage backend.",
        };
      }
    } catch (err) {
      console.error("[buckets.remote] get_bucket_cors", err);
    }
    return {
      cors_rules: [] as CorsRule[],
      error: null as string | null,
    };
  },
);

export const put_bucket_cors = command(
  z.object({
    bucket: z.string(),
    cors_rules: z.array(z.record(z.string(), z.unknown())),
  }),
  async ({ bucket, cors_rules }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/cors`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ CORSRules: cors_rules }),
      },
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({
        detail: "Failed to update CORS configuration",
      }));
      error(
        res.status,
        typeof err.detail === "string" ? err.detail : JSON.stringify(
          err.detail,
        ),
      );
    }
    return await res.json();
  },
);

export const delete_bucket_cors = command(
  z.object({ bucket: z.string() }),
  async ({ bucket }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/cors`,
      { method: "DELETE" },
    );
    await throwIfNotOk(res, "Failed to delete CORS configuration");
  },
);

// ── List Multipart Uploads ─────────────────────────────────────────

export type MultipartUploadEntry = {
  Key: string;
  UploadId: string;
  Initiated: string | null;
  StorageClass: string | null;
};

export const list_multipart_uploads = query(
  z.object({
    bucket: z.string(),
    prefix: z.string().optional(),
  }),
  async ({ bucket, prefix }) => {
    try {
      const params = new URLSearchParams();
      if (prefix) params.set("prefix", prefix);
      const qs = params.toString();
      const res = await apiFetch(
        `/api/v1/buckets/${encodeURIComponent(bucket)}/uploads${
          qs ? `?${qs}` : ""
        }`,
      );
      if (res.ok) {
        const data = await res.json();
        return {
          uploads: (data.uploads ?? []) as MultipartUploadEntry[],
          is_truncated: (data.is_truncated ?? false) as boolean,
          error: null as string | null,
        };
      }
    } catch (err) {
      console.error("[buckets.remote] list_multipart_uploads", err);
    }
    return {
      uploads: [] as MultipartUploadEntry[],
      is_truncated: false,
      error: null as string | null,
    };
  },
);

// ── Create Folder ──────────────────────────────────────────────────

export const create_folder = command(
  z.object({
    bucket: z.string(),
    folder_name: z.string(),
  }),
  async ({ bucket, folder_name }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/objects/folder`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder_name }),
      },
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({
        detail: "Failed to create folder",
      }));
      error(
        res.status,
        typeof err.detail === "string" ? err.detail : JSON.stringify(
          err.detail,
        ),
      );
    }
    return (await res.json()) as {
      bucket: string;
      key: string;
      status: string;
    };
  },
);

// ── Multipart Upload ───────────────────────────────────────────────

export const presign_multipart_upload = command(
  z.object({
    bucket: z.string(),
    key: z.string(),
    file_size: z.number().positive(),
    part_size: z.number().optional(),
    expires_in: z.number().optional(),
  }),
  async ({ bucket, key, file_size, part_size, expires_in }) => {
    const body: Record<string, unknown> = { file_size };
    if (part_size != null) body.part_size = part_size;
    if (expires_in != null) body.expires_in = expires_in;
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/multipart/${
        encodeURIComponent(key)
      }/presign`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to generate presigned multipart URLs");
    return (await res.json()) as {
      bucket: string;
      key: string;
      upload_id: string;
      part_size: number;
      total_parts: number;
      urls: { part_number: number; url: string }[];
      expires_in: number;
    };
  },
);

export const create_multipart_upload = command(
  z.object({ bucket: z.string(), key: z.string() }),
  async ({ bucket, key }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/multipart/${
        encodeURIComponent(key)
      }`,
      { method: "POST" },
    );
    await throwIfNotOk(res, "Failed to initiate multipart upload");
    return (await res.json()) as {
      bucket: string;
      key: string;
      upload_id: string;
    };
  },
);

export const complete_multipart_upload = command(
  z.object({
    bucket: z.string(),
    key: z.string(),
    upload_id: z.string(),
    parts: z.array(
      z.object({ PartNumber: z.number(), ETag: z.string() }),
    ),
  }),
  async ({ bucket, key, upload_id, parts }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/multipart/${
        encodeURIComponent(key)
      }/complete`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ upload_id, parts }),
      },
    );
    await throwIfNotOk(res, "Failed to complete multipart upload");
    return (await res.json()) as {
      bucket: string;
      key: string;
      etag: string | null;
    };
  },
);

export const abort_multipart_upload = command(
  z.object({
    bucket: z.string(),
    key: z.string(),
    upload_id: z.string(),
  }),
  async ({ bucket, key, upload_id }) => {
    const res = await apiFetch(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/multipart/${
        encodeURIComponent(key)
      }/abort`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ upload_id }),
      },
    );
    await throwIfNotOk(res, "Failed to abort multipart upload");
  },
);
