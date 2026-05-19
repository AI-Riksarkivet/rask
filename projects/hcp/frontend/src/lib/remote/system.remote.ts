import { z } from "zod";
import { command, query } from "$app/server";
import { apiFetch, throwIfNotOk } from "$lib/server/api.js";

// ── Network ─────────────────────────────────────────────────────────

export interface NetworkSettings {
  downstreamDNSMode?: string;
}

export const get_network_settings = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(`/api/v1/mapi/system/network?verbose=true`);
      if (res.ok) return (await res.json()) as NetworkSettings;
    } catch (err) {
      console.error("[system.remote]", err);
    }
    return {} as NetworkSettings;
  },
);

export const update_network_settings = command(
  z.object({ body: z.record(z.string(), z.unknown()) }),
  async ({ body }) => {
    const res = await apiFetch(`/api/v1/mapi/system/network`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await throwIfNotOk(res, "Failed to update network settings");
  },
);

// ── Licenses ────────────────────────────────────────────────────────

export interface License {
  serialNumber?: string;
  localCapacity?: number;
  expirationDate?: string;
  extendedCapacity?: number;
  feature?: string;
  uploadDate?: string;
}

export const get_licenses = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/storage/licenses?verbose=true`,
      );
      if (res.ok) {
        const data = await res.json();
        return (data.license ?? []) as License[];
      }
    } catch (err) {
      console.error("[system.remote]", err);
    }
    return [] as License[];
  },
);

export const get_license = query(
  z.object({ serial: z.string() }),
  async ({ serial }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/storage/licenses/${encodeURIComponent(serial)}`,
      );
      if (res.ok) return (await res.json()) as License;
    } catch (err) {
      console.error("[system.remote]", err);
    }
    return {} as License;
  },
);

// ── Node Statistics ─────────────────────────────────────────────────

export interface Volume {
  id?: string;
  blocksRead?: number;
  blocksWritten?: number;
  diskUtilization?: number;
  transferSpeed?: number;
  totalBytes?: number;
  freeBytes?: number;
  totalInodes?: number;
  freeInodes?: number;
}

export interface NodeStats {
  nodeNumber?: number;
  frontendIpAddresses?: string[];
  backendIpAddress?: string;
  managementIpAddresses?: string[];
  openHttpConnections?: number;
  openHttpsConnections?: number;
  maxHttpConnections?: number;
  maxHttpsConnections?: number;
  cpuUser?: number;
  cpuSystem?: number;
  cpuMax?: number;
  ioWait?: number;
  swapOut?: number;
  maxFrontEndBandwidth?: number;
  frontEndBytesRead?: number;
  frontEndBytesWritten?: number;
  maxBackEndBandwidth?: number;
  backEndBytesRead?: number;
  backEndBytesWritten?: number;
  maxManagementPortBandwidth?: number;
  managementBytesRead?: number;
  managementBytesWritten?: number;
  collectionTimestamp?: number;
  volumes?: Volume[];
}

export interface NodeStatistics {
  requestTime?: number;
  nodes?: NodeStats[];
}

export const get_node_statistics = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/nodes/statistics?verbose=true`,
      );
      if (res.ok) return (await res.json()) as NodeStatistics;
    } catch (err) {
      console.error("[system.remote]", err);
    }
    return { nodes: [] } as NodeStatistics;
  },
);

// ── Service Statistics ──────────────────────────────────────────────

export interface ServiceInfo {
  name?: string;
  state?: string;
  startTime?: number;
  formattedStartTime?: string;
  endTime?: number;
  formattedEndTime?: string;
  performanceLevel?: string;
  objectsExamined?: number;
  objectsExaminedPerSecond?: number;
  objectsServiced?: number;
  objectsServicedPerSecond?: number;
  objectsUnableToService?: number;
  objectsUnableToServicePerSecond?: number;
}

export interface ServiceStatistics {
  requestTime?: number;
  services?: ServiceInfo[];
}

export const get_service_statistics = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/services/statistics?verbose=true`,
      );
      if (res.ok) return (await res.json()) as ServiceStatistics;
    } catch (err) {
      console.error("[system.remote]", err);
    }
    return { services: [] } as ServiceStatistics;
  },
);

// ── System User Accounts ────────────────────────────────────────────

export interface SystemUser {
  username?: string;
  fullName?: string;
  description?: string;
  localAuthentication?: boolean;
  enabled?: boolean;
  forcePasswordChange?: boolean;
  roles?: { role?: string[] };
  allowNamespaceManagement?: boolean;
  userGUID?: string;
  userID?: number;
}

export const get_system_users = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/userAccounts?verbose=true`,
      );
      if (res.ok) return (await res.json()) as SystemUser[];
    } catch (err) {
      console.error("[system.remote]", err);
    }
    return [] as SystemUser[];
  },
);

export const get_system_user = query(
  z.object({ username: z.string() }),
  async ({ username }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/userAccounts/${encodeURIComponent(username)}`,
      );
      if (res.ok) return (await res.json()) as SystemUser;
    } catch (err) {
      console.error("[system.remote]", err);
    }
    return {} as SystemUser;
  },
);

export const change_system_user_password = command(
  z.object({
    username: z.string(),
    newPassword: z.string(),
    oldPassword: z.string().optional(),
  }),
  async ({ username, newPassword, oldPassword }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/userAccounts/${
        encodeURIComponent(username)
      }/changePassword`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ newPassword, oldPassword }),
      },
    );
    await throwIfNotOk(res, "Failed to change password");
  },
);

// ── System Group Accounts ───────────────────────────────────────────

export interface SystemGroup {
  groupname?: string;
  externalGroupID?: string;
  roles?: { role?: string[] };
  allowNamespaceManagement?: boolean;
}

export const get_system_groups = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/groupAccounts?verbose=true`,
      );
      if (res.ok) return (await res.json()) as SystemGroup[];
    } catch (err) {
      console.error("[system.remote]", err);
    }
    return [] as SystemGroup[];
  },
);

export const get_system_group = query(
  z.object({ groupname: z.string() }),
  async ({ groupname }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/groupAccounts/${encodeURIComponent(groupname)}`,
      );
      if (res.ok) return (await res.json()) as SystemGroup;
    } catch (err) {
      console.error("[system.remote]", err);
    }
    return {} as SystemGroup;
  },
);

// ── Tenants (system-level) ──────────────────────────────────────────

export interface TenantListEntry {
  name?: string;
  systemVisibleDescription?: string;
  hardQuota?: string;
  softQuota?: number;
  namespaceQuota?: string;
  administrationAllowed?: boolean;
}

export const get_all_tenants = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/tenants?verbose=true`,
      );
      if (res.ok) return (await res.json()) as TenantListEntry[];
    } catch (err) {
      console.error("[system.remote]", err);
    }
    return [] as TenantListEntry[];
  },
);

export const create_tenant = command(
  z.object({
    body: z.record(z.string(), z.unknown()),
    username: z.string(),
    password: z.string(),
  }),
  async ({ body, username, password }) => {
    const params = new URLSearchParams({ username, password });
    const res = await apiFetch(
      `/api/v1/mapi/system/tenants?${params.toString()}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to create tenant");
  },
);

export const delete_tenant = command(
  z.object({ name: z.string() }),
  async ({ name }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
    await throwIfNotOk(res, "Failed to delete tenant");
  },
);

// ── Support Access Credentials ──────────────────────────────────────

export interface SupportCredentials {
  applyTimeStamp?: number;
  createTimeStamp?: number;
  type?: string;
  defaultKeyType?: string;
  serialNumberFromPackage?: number;
}

export const get_support_credentials = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/supportaccesscredentials`,
      );
      if (res.ok) return (await res.json()) as SupportCredentials;
    } catch (err) {
      console.error("[system.remote]", err);
    }
    return {} as SupportCredentials;
  },
);

// ── Log Management ──────────────────────────────────────────────────

export interface LogStatus {
  readyForStreaming?: boolean;
  streamingInProgress?: boolean;
  started?: boolean;
  error?: boolean;
  content?: string;
  selectedNodes?: string;
  selectedContent?: string;
  packageNodes?: string;
}

export const get_log_status = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(`/api/v1/mapi/system/logs`);
      if (res.ok) return (await res.json()) as LogStatus;
    } catch (err) {
      console.error("[system.remote]", err);
    }
    return {} as LogStatus;
  },
);

export const mark_logs = command(
  z.object({ message: z.string() }),
  async ({ message }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/logs?mark=${encodeURIComponent(message)}`,
      { method: "POST" },
    );
    await throwIfNotOk(res, "Failed to mark logs");
  },
);

export const prepare_logs = command(
  z.object({
    startDate: z.string().optional(),
    endDate: z.string().optional(),
  }),
  async ({ startDate, endDate }) => {
    const res = await apiFetch(`/api/v1/mapi/system/logs/prepare`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ startDate, endDate }),
    });
    await throwIfNotOk(res, "Failed to prepare logs");
  },
);

export const cancel_log_download = command(
  z.object({}),
  async () => {
    const res = await apiFetch(`/api/v1/mapi/system/logs?cancel=true`, {
      method: "POST",
    });
    await throwIfNotOk(res, "Failed to cancel log download");
  },
);

// ── Health Check Reports ────────────────────────────────────────────

export interface HealthStatus {
  readyForStreaming?: boolean;
  streamingInProgress?: boolean;
  error?: boolean;
  started?: boolean;
  content?: string;
}

export const get_health_status = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(`/api/v1/mapi/system/healthCheckReport`);
      if (res.ok) return (await res.json()) as HealthStatus;
    } catch (err) {
      console.error("[system.remote]", err);
    }
    return {} as HealthStatus;
  },
);

export const prepare_health_report = command(
  z.object({
    startDate: z.string().optional(),
    endDate: z.string().optional(),
    collectCurrent: z.boolean().optional(),
  }),
  async ({ startDate, endDate, collectCurrent }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/healthCheckReport/prepare`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ startDate, endDate, collectCurrent }),
      },
    );
    await throwIfNotOk(res, "Failed to prepare health report");
  },
);

export const cancel_health_report = command(
  z.object({}),
  async () => {
    const res = await apiFetch(`/api/v1/mapi/system/healthCheckReport/cancel`, {
      method: "POST",
    });
    await throwIfNotOk(res, "Failed to cancel health report");
  },
);
