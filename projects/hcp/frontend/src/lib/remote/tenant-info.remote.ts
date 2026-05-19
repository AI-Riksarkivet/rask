import { z } from "zod";
import { command, query } from "$app/server";
import { apiFetch, throwIfNotOk } from "$lib/server/api.js";

export interface TenantInfo {
  name: string;
  systemVisibleDescription?: string;
  tenantVisibleDescription?: string;
  hardQuota?: string;
  softQuota?: string;
  namespaceQuota?: number;
  authenticationTypes?: { authenticationType: string[] };
  complianceConfigurationEnabled?: boolean;
  versioningConfigurationEnabled?: boolean;
  searchConfigurationEnabled?: boolean;
  replicationConfigurationEnabled?: boolean;
  servicePlan?: string;
  softwareVersion?: string;
}

export interface TenantStatistics {
  objectCount: number;
  storageCapacityUsed: number;
  ingestedVolume?: number;
  customMetadataCount?: number;
  customMetadataSize?: number;
  shredCount?: number;
  shredSize?: number;
}

export interface TenantSettings {
  consoleSecurity: Record<string, unknown>;
  contactInfo: Record<string, unknown>;
  permissions: Record<string, unknown>;
  namespaceDefaults: Record<string, unknown>;
}

export type { ChargebackEntry } from "$lib/utils/format.js";
import type { ChargebackEntry } from "$lib/utils/format.js";

export interface ChargebackReport {
  chargebackData?: ChargebackEntry[];
}

export const get_tenant = query(
  z.object({ tenant: z.string() }),
  async ({ tenant }) => {
    try {
      const res = await apiFetch(`/api/v1/mapi/tenants/${tenant}?verbose=true`);
      if (res.ok) return (await res.json()) as TenantInfo;
    } catch (err) {
      console.error("[tenant-info.remote]", err);
    }
    return { name: tenant } as TenantInfo;
  },
);

export const get_tenant_statistics = query(
  z.object({ tenant: z.string() }),
  async ({ tenant }) => {
    try {
      const res = await apiFetch(`/api/v1/mapi/tenants/${tenant}/statistics`);
      if (res.ok) return (await res.json()) as TenantStatistics;
    } catch (err) {
      console.error("[tenant-info.remote]", err);
    }
    return {
      objectCount: 0,
      storageCapacityUsed: 0,
    } as TenantStatistics;
  },
);

export const get_tenant_chargeback = query(
  z.object({ tenant: z.string() }),
  async ({ tenant }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/chargebackReport`,
      );
      if (res.ok) return (await res.json()) as ChargebackReport;
    } catch (err) {
      console.error("[tenant-info.remote]", err);
    }
    return { chargebackData: [] } as ChargebackReport;
  },
);

export const get_tenant_settings = query(
  z.object({ tenant: z.string() }),
  async ({ tenant }) => {
    const base = `/api/v1/mapi/tenants/${tenant}`;
    const keys = [
      "consoleSecurity",
      "contactInfo",
      "permissions",
      "namespaceDefaults",
    ] as const;

    const results = await Promise.all(
      keys.map(async (key) => {
        try {
          const res = await apiFetch(`${base}/${key}`);
          if (res.ok) return [key, await res.json()] as const;
        } catch (err) {
          console.error("[tenant-info.remote]", err);
        }
        return [key, {}] as const;
      }),
    );

    return Object.fromEntries(results) as TenantSettings;
  },
);

export const update_contact_info = command(
  z.object({ tenant: z.string(), body: z.record(z.string(), z.unknown()) }),
  async ({ tenant, body }) => {
    const res = await apiFetch(`/api/v1/mapi/tenants/${tenant}/contactInfo`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await throwIfNotOk(res, "Failed to update contact info");
  },
);

export const update_namespace_defaults = command(
  z.object({ tenant: z.string(), body: z.record(z.string(), z.unknown()) }),
  async ({ tenant, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/namespaceDefaults`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to update namespace defaults");
  },
);

export const update_permissions = command(
  z.object({ tenant: z.string(), body: z.record(z.string(), z.unknown()) }),
  async ({ tenant, body }) => {
    const res = await apiFetch(`/api/v1/mapi/tenants/${tenant}/permissions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await throwIfNotOk(res, "Failed to update permissions");
  },
);

// ── Console Security ─────────────────────────────────────────────────

export interface IpSettings {
  allowAddresses?: string[];
  denyAddresses?: string[];
  allowIfInBothLists?: boolean;
}

export interface ConsoleSecurity {
  minimumPasswordLength?: number;
  lowerCaseLetterCount?: number;
  upperCaseLetterCount?: number;
  numericCharacterCount?: number;
  specialCharacterCount?: number;
  passwordCombination?: boolean;
  passwordContainsUsername?: boolean;
  passwordReuseDepth?: number;
  blockCommonPassword?: boolean;
  blockPasswordReUse?: boolean;
  forcePasswordChangeDays?: number;
  disableAfterAttempts?: number;
  coolDownPeriodSettings?: boolean;
  coolDownPeriodDuration?: number;
  automaticUserAccountUnlockSetting?: boolean;
  automaticUserAccoutUnlockDuration?: number;
  disableAfterInactiveDays?: number;
  logoutOnInactive?: number;
  loginMessage?: string;
  ipSettings?: IpSettings;
}

export const get_console_security = query(
  z.object({ tenant: z.string() }),
  async ({ tenant }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/consoleSecurity`,
      );
      if (res.ok) return (await res.json()) as ConsoleSecurity;
    } catch (err) {
      console.error("[tenant-info.remote]", err);
    }
    return {} as ConsoleSecurity;
  },
);

export const update_console_security = command(
  z.object({ tenant: z.string(), body: z.record(z.string(), z.unknown()) }),
  async ({ tenant, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/consoleSecurity`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to update console security");
  },
);

// ── Email Notifications ──────────────────────────────────────────────

export interface EmailTemplate {
  from?: string;
  subject?: string;
  body?: string;
}

export interface Recipient {
  address: string;
  importance?: string;
  severity?: string;
  type?: string;
}

export interface EmailNotification {
  enabled?: boolean;
  emailTemplate?: EmailTemplate;
  recipients?: Recipient[];
}

export const get_email_notification = query(
  z.object({ tenant: z.string() }),
  async ({ tenant }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/emailNotification`,
      );
      if (res.ok) return (await res.json()) as EmailNotification;
    } catch (err) {
      console.error("[tenant-info.remote]", err);
    }
    return {} as EmailNotification;
  },
);

export const update_email_notification = command(
  z.object({ tenant: z.string(), body: z.record(z.string(), z.unknown()) }),
  async ({ tenant, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/emailNotification`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to update email notification");
  },
);

// ── Search Security ──────────────────────────────────────────────────

export interface SearchSecurity {
  ipSettings?: IpSettings;
}

export const get_search_security = query(
  z.object({ tenant: z.string() }),
  async ({ tenant }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/searchSecurity`,
      );
      if (res.ok) return (await res.json()) as SearchSecurity;
    } catch (err) {
      console.error("[tenant-info.remote]", err);
    }
    return {} as SearchSecurity;
  },
);

export const update_search_security = command(
  z.object({ tenant: z.string(), body: z.record(z.string(), z.unknown()) }),
  async ({ tenant, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/searchSecurity`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to update search security");
  },
);

// ── Tenant CORS ──────────────────────────────────────────────────────

export interface TenantCors {
  cors?: string;
}

export const get_tenant_cors = query(
  z.object({ tenant: z.string() }),
  async ({ tenant }) => {
    try {
      const res = await apiFetch(`/api/v1/mapi/tenants/${tenant}/cors`);
      if (res.ok) return (await res.json()) as TenantCors;
    } catch (err) {
      console.error("[tenant-info.remote]", err);
    }
    return {} as TenantCors;
  },
);

export const set_tenant_cors = command(
  z.object({
    tenant: z.string(),
    body: z.record(z.string(), z.unknown()),
  }),
  async ({ tenant, body }) => {
    const res = await apiFetch(`/api/v1/mapi/tenants/${tenant}/cors`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await throwIfNotOk(res, "Failed to update CORS configuration");
  },
);

export const delete_tenant_cors = command(
  z.object({ tenant: z.string() }),
  async ({ tenant }) => {
    const res = await apiFetch(`/api/v1/mapi/tenants/${tenant}/cors`, {
      method: "DELETE",
    });
    await throwIfNotOk(res, "Failed to delete CORS configuration");
  },
);

// ── Tenant Operations (top-level POST) ──────────────────────────────

export interface TenantOperations {
  administrationAllowed?: boolean;
  maxNamespacesPerUser?: number;
  snmpLoggingEnabled?: boolean;
  syslogLoggingEnabled?: boolean;
  tenantVisibleDescription?: string;
  tags?: Record<string, unknown>;
}

export const update_tenant = command(
  z.object({ tenant: z.string(), body: z.record(z.string(), z.unknown()) }),
  async ({ tenant, body }) => {
    const res = await apiFetch(`/api/v1/mapi/tenants/${tenant}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await throwIfNotOk(res, "Failed to update tenant settings");
  },
);

// ── Available Service Plans ──────────────────────────────────────────

export interface ServicePlanList {
  name?: string[];
}

export interface ServicePlan {
  name?: string;
  description?: string;
}

export const get_service_plans = query(
  z.object({ tenant: z.string() }),
  async ({ tenant }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/availableServicePlans`,
      );
      if (res.ok) {
        const data = (await res.json()) as ServicePlanList;
        const names = data.name ?? [];
        const plans = await Promise.all(
          names.map(async (n) => {
            try {
              const r = await apiFetch(
                `/api/v1/mapi/tenants/${tenant}/availableServicePlans/${
                  encodeURIComponent(n)
                }`,
              );
              if (r.ok) return (await r.json()) as ServicePlan;
            } catch {
              // ignore
            }
            return { name: n } as ServicePlan;
          }),
        );
        return plans;
      }
    } catch (err) {
      console.error("[tenant-info.remote]", err);
    }
    return [] as ServicePlan[];
  },
);
