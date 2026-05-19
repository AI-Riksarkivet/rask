import { z } from "zod";
import { command, query } from "$app/server";
import { error } from "@sveltejs/kit";
import { apiFetch, throwIfNotOk } from "$lib/server/api.js";

export interface Namespace {
  name: string;
  description?: string;
  hardQuota?: string;
  softQuota?: string;
  hashScheme?: string;
  searchEnabled?: boolean;
  versioningSettings?: { enabled: boolean };
  tags?: { tag: string[] };
  owner?: string;
  ownerType?: string;
  creationTime?: string;
}

export interface NsProtocols {
  httpEnabled?: boolean;
  httpsEnabled?: boolean;
  cifsEnabled?: boolean;
  nfsEnabled?: boolean;
  smtpEnabled?: boolean;
  hs3Enabled?: boolean;
  restEnabled?: boolean;
  hswiftEnabled?: boolean;
  webdavEnabled?: boolean;
}

export type { IpSettings } from "./tenant-info.remote.js";
import type { IpSettings } from "./tenant-info.remote.js";

export interface NsPermissions {
  readAllowed?: boolean;
  writeAllowed?: boolean;
  deleteAllowed?: boolean;
  purgeAllowed?: boolean;
  searchAllowed?: boolean;
  readAclAllowed?: boolean;
  writeAclAllowed?: boolean;
}

export const get_namespaces = query(
  z.object({ tenant: z.string() }),
  async ({ tenant }) => {
    try {
      const res = await apiFetch(`/api/v1/mapi/tenants/${tenant}/namespaces`);
      if (res.ok) {
        const data = await res.json();
        const names: string[] = data.name ?? [];

        const details = await Promise.all(
          names.map(async (n) => {
            try {
              const r = await apiFetch(
                `/api/v1/mapi/tenants/${tenant}/namespaces/${
                  encodeURIComponent(n)
                }`,
              );
              if (r.ok) return (await r.json()) as Namespace;
            } catch (err) {
              console.error("[namespaces.remote]", err);
            }
            return { name: n } as Namespace;
          }),
        );
        return details;
      }
    } catch (err) {
      console.error("[namespaces.remote]", err);
    }
    return [] as Namespace[];
  },
);

export const create_namespace = command(
  z.object({
    tenant: z.string(),
    name: z.string(),
    description: z.string().optional(),
    hardQuota: z.string().optional(),
    softQuota: z.number().optional(),
    hashScheme: z.string().optional(),
    searchEnabled: z.boolean().optional(),
    versioningEnabled: z.boolean().optional(),
    keepDeletionRecords: z.boolean().optional(),
    optimizedFor: z.string().optional(),
    tags: z.array(z.string()).optional(),
    owner: z.string().optional(),
  }),
  async ({ tenant, ...body }) => {
    const payload: Record<string, unknown> = { name: body.name };
    if (body.description) payload.description = body.description;
    if (body.hardQuota) payload.hardQuota = body.hardQuota;
    if (body.softQuota != null) payload.softQuota = body.softQuota;
    if (body.hashScheme) payload.hashScheme = body.hashScheme;
    if (body.optimizedFor) payload.optimizedFor = body.optimizedFor;
    if (body.searchEnabled != null) payload.searchEnabled = body.searchEnabled;
    if (body.versioningEnabled != null) {
      payload.versioningSettings = {
        enabled: body.versioningEnabled,
        keepDeletionRecords: body.keepDeletionRecords ?? false,
      };
    }
    if (body.tags) payload.tags = { tag: body.tags };
    if (body.owner) {
      payload.owner = body.owner;
      payload.ownerType = "LOCAL";
    }
    const res = await apiFetch(`/api/v1/mapi/tenants/${tenant}/namespaces`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({
        detail: "Failed to create namespace",
      }));
      const detail = typeof err.detail === "string"
        ? err.detail
        : Array.isArray(err.detail)
        ? err.detail.map((e: { msg?: string }) => e.msg).join("; ")
        : JSON.stringify(err.detail);
      error(res.status, detail);
    }

    // Enable HS3 (S3-compatible API) by default on new namespaces
    const enc = encodeURIComponent(body.name);
    await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${enc}/protocols/http`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hs3Enabled: true }),
      },
    ).catch((err) =>
      console.warn("[namespaces.remote] Failed to enable HS3:", err)
    );
  },
);

export const update_namespace = command(
  z.object({
    tenant: z.string(),
    name: z.string(),
    body: z.record(z.string(), z.unknown()),
  }),
  async ({ tenant, name, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${encodeURIComponent(name)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to update namespace");
  },
);

export const get_namespace = query(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/namespaces/${encodeURIComponent(name)}`,
      );
      if (res.ok) return (await res.json()) as Namespace;
    } catch (err) {
      console.error("[namespaces.remote]", err);
    }
    return null;
  },
);

export const get_ns_protocols = query(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    try {
      const enc = encodeURIComponent(name);
      const [summaryRes, httpRes] = await Promise.all([
        apiFetch(`/api/v1/mapi/tenants/${tenant}/namespaces/${enc}/protocols`),
        apiFetch(
          `/api/v1/mapi/tenants/${tenant}/namespaces/${enc}/protocols/http`,
        ),
      ]);
      const summary = summaryRes.ok
        ? ((await summaryRes.json()) as NsProtocols)
        : ({} as NsProtocols);
      if (httpRes.ok) {
        const http = (await httpRes.json()) as Record<string, unknown>;
        summary.hs3Enabled = (http.hs3Enabled as boolean) ?? false;
        summary.restEnabled = (http.restEnabled as boolean) ?? false;
        summary.hswiftEnabled = (http.hswiftEnabled as boolean) ?? false;
        summary.webdavEnabled = (http.webdavEnabled as boolean) ?? false;
      }
      return summary;
    } catch (err) {
      console.error("[namespaces.remote]", err);
    }
    return {} as NsProtocols;
  },
);

export const get_ns_protocol_detail = query(
  z.object({
    tenant: z.string(),
    name: z.string(),
    protocol: z.enum(["http", "cifs", "nfs", "smtp"]),
  }),
  async ({ tenant, name, protocol }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/namespaces/${
          encodeURIComponent(name)
        }/protocols/${protocol}`,
      );
      if (res.ok) {
        return (await res.json()) as Record<string, unknown> & {
          ipSettings?: IpSettings;
        };
      }
    } catch (err) {
      console.error("[namespaces.remote]", err);
    }
    return {} as Record<string, unknown> & { ipSettings?: IpSettings };
  },
);

export const update_ns_protocol = command(
  z.object({
    tenant: z.string(),
    name: z.string(),
    protocol: z.enum(["http", "cifs", "nfs", "smtp"]),
    body: z.record(z.string(), z.unknown()),
  }),
  async ({ tenant, name, protocol, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(name)
      }/protocols/${protocol}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, `Failed to update ${protocol} protocol`);
  },
);

export const get_ns_permissions = query(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/namespaces/${
          encodeURIComponent(name)
        }/permissions`,
      );
      if (res.ok) return (await res.json()) as NsPermissions;
    } catch (err) {
      console.error("[namespaces.remote]", err);
    }
    return {} as NsPermissions;
  },
);

export const update_ns_permissions = command(
  z.object({
    tenant: z.string(),
    name: z.string(),
    body: z.record(z.string(), z.unknown()),
  }),
  async ({ tenant, name, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(name)
      }/permissions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to update permissions");
  },
);

export const update_versioning = command(
  z.object({
    tenant: z.string(),
    name: z.string(),
    enabled: z.boolean(),
  }),
  async ({ tenant, name, enabled }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(name)
      }/versioningSettings`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      },
    );
    await throwIfNotOk(res, "Failed to update versioning");
  },
);

// ── Versioning Settings (MAPI) ──────────────────────────────────────

export interface VersioningSettings {
  enabled?: boolean;
  prune?: boolean;
  pruneDays?: number;
  useDeleteMarkers?: boolean;
  keepDeletionRecords?: boolean;
}

export const get_ns_versioning = query(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/namespaces/${
          encodeURIComponent(name)
        }/versioningSettings`,
      );
      if (res.ok) return (await res.json()) as VersioningSettings;
    } catch (err) {
      console.error("[namespaces.remote]", err);
    }
    return {} as VersioningSettings;
  },
);

export const update_ns_versioning = command(
  z.object({
    tenant: z.string(),
    name: z.string(),
    body: z.record(z.string(), z.unknown()),
  }),
  async ({ tenant, name, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(name)
      }/versioningSettings`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to update versioning settings");
  },
);

export const delete_ns_versioning = command(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(name)
      }/versioningSettings`,
      { method: "DELETE" },
    );
    await throwIfNotOk(res, "Failed to reset versioning settings");
  },
);

// ── Namespace Statistics ──────────────────────────────────────────────

export interface NsStatistics {
  objectCount?: number;
  storageCapacityUsed?: number;
  ingestedVolume?: number;
  customMetadataCount?: number;
  customMetadataSize?: number;
  shredCount?: number;
  shredSize?: number;
}

export const get_ns_statistics = query(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/namespaces/${
          encodeURIComponent(name)
        }/statistics`,
      );
      if (res.ok) return (await res.json()) as NsStatistics;
    } catch (err) {
      console.error("[namespaces.remote]", err);
    }
    return null;
  },
);

// ── Compliance Settings ──────────────────────────────────────────────

export interface ComplianceSettings {
  retentionDefault?: string;
  minimumRetentionAfterInitialUnspecified?: string;
  shreddingDefault?: boolean;
  customMetadataChanges?: string;
  dispositionEnabled?: boolean;
}

export const get_ns_compliance = query(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/namespaces/${
          encodeURIComponent(name)
        }/complianceSettings`,
      );
      if (res.ok) return (await res.json()) as ComplianceSettings;
    } catch (err) {
      console.error("[namespaces.remote]", err);
    }
    return {} as ComplianceSettings;
  },
);

export const update_ns_compliance = command(
  z.object({
    tenant: z.string(),
    name: z.string(),
    body: z.record(z.string(), z.unknown()),
  }),
  async ({ tenant, name, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(name)
      }/complianceSettings`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to update compliance settings");
  },
);

// ── Retention Classes ────────────────────────────────────────────────

export interface RetentionClass {
  name?: string;
  value?: string;
  description?: string;
  allowDisposition?: boolean;
}

export const get_retention_classes = query(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    try {
      const listRes = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/namespaces/${
          encodeURIComponent(name)
        }/retentionClasses`,
      );
      if (!listRes.ok) return [] as RetentionClass[];
      const list = await listRes.json();
      const names: string[] = list.name ?? [];
      if (names.length === 0) return [] as RetentionClass[];

      const details = await Promise.all(
        names.map(async (rcName) => {
          try {
            const r = await apiFetch(
              `/api/v1/mapi/tenants/${tenant}/namespaces/${
                encodeURIComponent(name)
              }/retentionClasses/${encodeURIComponent(rcName)}`,
            );
            if (r.ok) return (await r.json()) as RetentionClass;
          } catch {
            // ignore individual failures
          }
          return { name: rcName } as RetentionClass;
        }),
      );
      return details;
    } catch (err) {
      console.error("[namespaces.remote]", err);
    }
    return [] as RetentionClass[];
  },
);

export const create_retention_class = command(
  z.object({
    tenant: z.string(),
    namespace: z.string(),
    body: z.object({
      name: z.string(),
      value: z.string(),
      description: z.string().optional(),
      allowDisposition: z.boolean().optional(),
    }),
  }),
  async ({ tenant, namespace: ns, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(ns)
      }/retentionClasses`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to create retention class");
  },
);

export const update_retention_class = command(
  z.object({
    tenant: z.string(),
    namespace: z.string(),
    className: z.string(),
    body: z.record(z.string(), z.unknown()),
  }),
  async ({ tenant, namespace: ns, className, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(ns)
      }/retentionClasses/${encodeURIComponent(className)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to update retention class");
  },
);

export const delete_retention_class = command(
  z.object({
    tenant: z.string(),
    namespace: z.string(),
    className: z.string(),
  }),
  async ({ tenant, namespace: ns, className }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(ns)
      }/retentionClasses/${encodeURIComponent(className)}`,
      { method: "DELETE" },
    );
    await throwIfNotOk(res, "Failed to delete retention class");
  },
);

// ── Custom Metadata Indexing ─────────────────────────────────────────

export interface IndexingSettings {
  contentClasses?: string[];
  fullIndexingEnabled?: boolean;
  excludedAnnotations?: string;
}

export const get_ns_indexing = query(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/namespaces/${
          encodeURIComponent(name)
        }/customMetadataIndexingSettings`,
      );
      if (res.ok) return (await res.json()) as IndexingSettings;
    } catch (err) {
      console.error("[namespaces.remote]", err);
    }
    return {} as IndexingSettings;
  },
);

export const update_ns_indexing = command(
  z.object({
    tenant: z.string(),
    name: z.string(),
    body: z.record(z.string(), z.unknown()),
  }),
  async ({ tenant, name, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(name)
      }/customMetadataIndexingSettings`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to update indexing settings");
  },
);

// ── CORS Configuration ───────────────────────────────────────────────

export interface CorsConfig {
  cors?: string;
}

export const get_ns_cors = query(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/namespaces/${
          encodeURIComponent(name)
        }/cors`,
      );
      if (res.ok) return (await res.json()) as CorsConfig;
    } catch (err) {
      console.error("[namespaces.remote]", err);
    }
    return {} as CorsConfig;
  },
);

export const set_ns_cors = command(
  z.object({
    tenant: z.string(),
    name: z.string(),
    body: z.object({ cors: z.string() }),
  }),
  async ({ tenant, name, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(name)
      }/cors`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to set CORS configuration");
  },
);

export const delete_ns_cors = command(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(name)
      }/cors`,
      { method: "DELETE" },
    );
    await throwIfNotOk(res, "Failed to delete CORS configuration");
  },
);

// ── Replication Collision Settings ───────────────────────────────────

export interface ReplicationCollision {
  action?: string;
  deleteDays?: number;
  deleteEnabled?: boolean;
}

export const get_repl_collision = query(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/namespaces/${
          encodeURIComponent(name)
        }/replicationCollisionSettings`,
      );
      if (res.ok) return (await res.json()) as ReplicationCollision;
    } catch (err) {
      console.error("[namespaces.remote]", err);
    }
    return {} as ReplicationCollision;
  },
);

export const update_repl_collision = command(
  z.object({
    tenant: z.string(),
    name: z.string(),
    body: z.record(z.string(), z.unknown()),
  }),
  async ({ tenant, name, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(name)
      }/replicationCollisionSettings`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to update replication collision settings");
  },
);

// ── Namespace Templates (Export) ─────────────────────────────────────

export const export_namespace_config = command(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${
        encodeURIComponent(name)
      }/export`,
    );
    await throwIfNotOk(res, "Export failed");
    return await res.json();
  },
);

export const export_namespace_configs = command(
  z.object({ tenant: z.string(), names: z.array(z.string()) }),
  async ({ tenant, names }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/export?names=${
        names.map(encodeURIComponent).join(",")
      }`,
    );
    await throwIfNotOk(res, "Export failed");
    return await res.json();
  },
);

// ── Delete Namespace ─────────────────────────────────────────────────

// ── Namespace Chargeback ──────────────────────────────────────────────

export const get_ns_chargeback = query(
  z.object({
    tenant: z.string(),
    name: z.string(),
    start: z.string().optional(),
    end: z.string().optional(),
    granularity: z.enum(["hour", "day", "total"]).optional(),
  }),
  async ({ tenant, name, start, end, granularity }) => {
    try {
      const params = new URLSearchParams();
      if (start) params.set("start", start);
      if (end) params.set("end", end);
      if (granularity) params.set("granularity", granularity);
      const qs = params.toString();
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/namespaces/${
          encodeURIComponent(name)
        }/chargebackReport${qs ? `?${qs}` : ""}`,
      );
      if (res.ok) return await res.json();
    } catch (err) {
      console.error("[namespaces.remote]", err);
    }
    return { chargebackData: [] };
  },
);

export const delete_namespace = command(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaces/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({
        detail: `Failed to delete namespace "${name}"`,
      }));
      const detail = typeof err.detail === "string"
        ? err.detail
        : JSON.stringify(err.detail);
      error(res.status, detail);
    }
  },
);
