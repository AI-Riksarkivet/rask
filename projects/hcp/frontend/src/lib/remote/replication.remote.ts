import { z } from "zod";
import { command, query } from "$app/server";
import { apiFetch, throwIfNotOk } from "$lib/server/api.js";

// ── Replication Service ──────────────────────────────────────────────

export interface ReplicationServiceSettings {
  allowTenantsToMonitorNamespaces?: boolean;
  enableDNSFailover?: boolean;
  enableDomainAndCertificateSynchronization?: boolean;
  network?: string;
  connectivityTimeoutSeconds?: number;
  verification?: string;
  status?: string;
}

export const get_replication_service = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/services/replication?verbose=true`,
      );
      if (res.ok) return (await res.json()) as ReplicationServiceSettings;
    } catch (err) {
      console.error("[replication.remote]", err);
    }
    return {} as ReplicationServiceSettings;
  },
);

export const update_replication_service = command(
  z.object({ body: z.record(z.string(), z.unknown()) }),
  async ({ body }) => {
    const res = await apiFetch(`/api/v1/mapi/system/services/replication`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await throwIfNotOk(res, "Failed to update replication service");
  },
);

// ── Replication Certificates ─────────────────────────────────────────

export interface ReplicationCertificate {
  id?: string;
  subjectDN?: string;
  validOn?: string;
  expiresOn?: string;
}

export const get_replication_certificates = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/services/replication/certificates?verbose=true`,
      );
      if (res.ok) {
        const data = await res.json();
        return (data.certificate ?? []) as ReplicationCertificate[];
      }
    } catch (err) {
      console.error("[replication.remote]", err);
    }
    return [] as ReplicationCertificate[];
  },
);

export const delete_replication_certificate = command(
  z.object({ certificateId: z.string() }),
  async ({ certificateId }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/services/replication/certificates/${
        encodeURIComponent(certificateId)
      }`,
      { method: "DELETE" },
    );
    await throwIfNotOk(res, "Failed to delete certificate");
  },
);

// ── Replication Links ────────────────────────────────────────────────

export interface LinkConnection {
  remoteHost?: string;
  remotePort?: number;
  localHost?: string;
  localPort?: number;
}

export interface FailoverNode {
  autoFailover?: boolean;
  autoFailoverMinutes?: number;
  autoCompleteRecovery?: boolean;
  autoCompleteRecoveryMinutes?: number;
}

export interface FailoverSettings {
  local?: FailoverNode;
  remote?: FailoverNode;
}

export interface LinkStatistics {
  upToDateAsOfString?: string;
  upToDateAsOfMillis?: number;
  bytesPending?: number;
  bytesPendingRemote?: number;
  bytesReplicated?: number;
  bytesPerSecond?: number;
  objectsPending?: number;
  objectsPendingRemote?: number;
  objectsReplicated?: number;
  operationsPerSecond?: number;
  errors?: number;
  errorsPerSecond?: number;
}

export interface ReplicationLink {
  name?: string;
  type?: string;
  description?: string;
  connection?: LinkConnection;
  compression?: boolean;
  encryption?: boolean;
  priority?: string;
  id?: string;
  status?: string;
  statusMessage?: string;
  suspended?: boolean;
  failoverSettings?: FailoverSettings;
  statistics?: LinkStatistics;
}

export const get_replication_links = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/services/replication/links?verbose=true`,
      );
      if (res.ok) return (await res.json()) as ReplicationLink[];
    } catch (err) {
      console.error("[replication.remote]", err);
    }
    return [] as ReplicationLink[];
  },
);

export const get_replication_link = query(
  z.object({ linkName: z.string() }),
  async ({ linkName }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/services/replication/links/${
          encodeURIComponent(linkName)
        }?verbose=true`,
      );
      if (res.ok) return (await res.json()) as ReplicationLink;
    } catch (err) {
      console.error("[replication.remote]", err);
    }
    return {} as ReplicationLink;
  },
);

export const create_replication_link = command(
  z.object({ body: z.record(z.string(), z.unknown()) }),
  async ({ body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/services/replication/links`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to create link");
  },
);

export const delete_replication_link = command(
  z.object({ linkName: z.string() }),
  async ({ linkName }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/services/replication/links/${
        encodeURIComponent(linkName)
      }`,
      { method: "DELETE" },
    );
    await throwIfNotOk(res, "Failed to delete link");
  },
);

export const action_replication_link = command(
  z.object({
    linkName: z.string(),
    action: z.enum([
      "suspend",
      "resume",
      "failOver",
      "failBack",
      "beginRecover",
      "completeRecovery",
    ]),
  }),
  async ({ linkName, action }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/services/replication/links/${
        encodeURIComponent(linkName)
      }?${action}=true`,
      { method: "POST" },
    );
    await throwIfNotOk(res, `Failed to ${action} link`);
  },
);

// ── Link Content ─────────────────────────────────────────────────────

export interface LinkContent {
  tenants?: string[];
  defaultNamespaceDirectories?: string[];
  chainedLinks?: string[];
}

export const get_link_content = query(
  z.object({ linkName: z.string() }),
  async ({ linkName }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/services/replication/links/${
          encodeURIComponent(linkName)
        }/content`,
      );
      if (res.ok) return (await res.json()) as LinkContent;
    } catch (err) {
      console.error("[replication.remote]", err);
    }
    return {} as LinkContent;
  },
);

export const add_tenant_to_link = command(
  z.object({ linkName: z.string(), tenantName: z.string() }),
  async ({ linkName, tenantName }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/services/replication/links/${
        encodeURIComponent(linkName)
      }/content/tenants/${encodeURIComponent(tenantName)}`,
      { method: "PUT" },
    );
    await throwIfNotOk(res, "Failed to add tenant");
  },
);

export const remove_tenant_from_link = command(
  z.object({ linkName: z.string(), tenantName: z.string() }),
  async ({ linkName, tenantName }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/services/replication/links/${
        encodeURIComponent(linkName)
      }/content/tenants/${encodeURIComponent(tenantName)}`,
      { method: "DELETE" },
    );
    await throwIfNotOk(res, "Failed to remove tenant");
  },
);

// ── Link Schedule ────────────────────────────────────────────────────

export interface ScheduleTransition {
  time?: string;
  performanceLevel?: string;
}

export interface ScheduleSide {
  scheduleOverride?: string;
  transition?: ScheduleTransition[];
}

export interface LinkSchedule {
  local?: ScheduleSide;
  remote?: ScheduleSide;
}

export const get_link_schedule = query(
  z.object({ linkName: z.string() }),
  async ({ linkName }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/services/replication/links/${
          encodeURIComponent(linkName)
        }/schedule`,
      );
      if (res.ok) return (await res.json()) as LinkSchedule;
    } catch (err) {
      console.error("[replication.remote]", err);
    }
    return {} as LinkSchedule;
  },
);

export const update_link_schedule = command(
  z.object({
    linkName: z.string(),
    body: z.record(z.string(), z.unknown()),
  }),
  async ({ linkName, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/services/replication/links/${
        encodeURIComponent(linkName)
      }/schedule`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to update schedule");
  },
);

// ── Link Candidates ──────────────────────────────────────────────────

export const get_local_candidates = query(
  z.object({ linkName: z.string() }),
  async ({ linkName }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/services/replication/links/${
          encodeURIComponent(linkName)
        }/localCandidates`,
      );
      if (res.ok) return (await res.json()) as LinkContent;
    } catch (err) {
      console.error("[replication.remote]", err);
    }
    return {} as LinkContent;
  },
);

export const get_remote_candidates = query(
  z.object({ linkName: z.string() }),
  async ({ linkName }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/services/replication/links/${
          encodeURIComponent(linkName)
        }/remoteCandidates`,
      );
      if (res.ok) return (await res.json()) as LinkContent;
    } catch (err) {
      console.error("[replication.remote]", err);
    }
    return {} as LinkContent;
  },
);

// ── Erasure Coding ───────────────────────────────────────────────────

export interface ECReplicationLink {
  name?: string;
  uuid?: string;
  hcpSystems?: string[];
  pausedTenantsCount?: number;
  state?: string;
}

export interface ECTopology {
  name?: string;
  type?: string;
  description?: string;
  erasureCodingDelay?: number;
  fullCopy?: boolean;
  minimumObjectSize?: number;
  restorePeriod?: number;
  id?: string;
  state?: string;
  protectionStatus?: string;
  readStatus?: string;
  erasureCodedObjects?: number;
  replicationLinks?: ECReplicationLink[];
  hcpSystems?: string[];
  tenants?: string[];
}

export interface TenantCandidate {
  name?: string;
  uuid?: string;
  hcpSystems?: string[];
}

export const get_ec_topologies = query(
  z.object({}),
  async () => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/services/erasureCoding/ecTopologies?verbose=true`,
      );
      if (res.ok) return (await res.json()) as ECTopology[];
    } catch (err) {
      console.error("[replication.remote]", err);
    }
    return [] as ECTopology[];
  },
);

export const get_ec_topology = query(
  z.object({ topologyName: z.string() }),
  async ({ topologyName }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/services/erasureCoding/ecTopologies/${
          encodeURIComponent(topologyName)
        }?verbose=true`,
      );
      if (res.ok) return (await res.json()) as ECTopology;
    } catch (err) {
      console.error("[replication.remote]", err);
    }
    return {} as ECTopology;
  },
);

export const create_ec_topology = command(
  z.object({ body: z.record(z.string(), z.unknown()) }),
  async ({ body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/services/erasureCoding/ecTopologies`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to create topology");
  },
);

export const delete_ec_topology = command(
  z.object({ topologyName: z.string() }),
  async ({ topologyName }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/services/erasureCoding/ecTopologies/${
        encodeURIComponent(topologyName)
      }`,
      { method: "DELETE" },
    );
    await throwIfNotOk(res, "Failed to delete topology");
  },
);

export const retire_ec_topology = command(
  z.object({ topologyName: z.string() }),
  async ({ topologyName }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/services/erasureCoding/ecTopologies/${
        encodeURIComponent(topologyName)
      }?retire=true`,
      { method: "POST" },
    );
    await throwIfNotOk(res, "Failed to retire topology");
  },
);

export const get_ec_tenant_candidates = query(
  z.object({ topologyName: z.string() }),
  async ({ topologyName }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/system/services/erasureCoding/ecTopologies/${
          encodeURIComponent(topologyName)
        }/tenantCandidates?verbose=true`,
      );
      if (res.ok) {
        const data = await res.json();
        return (data.tenantCandidate ?? []) as TenantCandidate[];
      }
    } catch (err) {
      console.error("[replication.remote]", err);
    }
    return [] as TenantCandidate[];
  },
);

export const add_tenant_to_ec_topology = command(
  z.object({ topologyName: z.string(), tenantName: z.string() }),
  async ({ topologyName, tenantName }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/services/erasureCoding/ecTopologies/${
        encodeURIComponent(topologyName)
      }/tenants/${encodeURIComponent(tenantName)}`,
      { method: "PUT" },
    );
    await throwIfNotOk(res, "Failed to add tenant");
  },
);

export const remove_tenant_from_ec_topology = command(
  z.object({ topologyName: z.string(), tenantName: z.string() }),
  async ({ topologyName, tenantName }) => {
    const res = await apiFetch(
      `/api/v1/mapi/system/services/erasureCoding/ecTopologies/${
        encodeURIComponent(topologyName)
      }/tenants/${encodeURIComponent(tenantName)}`,
      { method: "DELETE" },
    );
    await throwIfNotOk(res, "Failed to remove tenant");
  },
);
