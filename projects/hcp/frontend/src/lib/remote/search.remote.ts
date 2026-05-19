import { z } from "zod";
import { command } from "$app/server";
import { error } from "@sveltejs/kit";
import { apiFetch } from "$lib/server/api.js";

export interface QueryStatus {
  totalResults: number;
  results: number;
  code: string;
  message?: string;
}

export interface QueryResultObject {
  urlName: string;
  operation: string;
  changeTimeMilliseconds?: string;
  changeTimeString?: string;
  version?: number | string;
  namespace?: string;
  utf8Name?: string;
  objectPath?: string;
  size?: number;
  contentType?: string;
  hold?: boolean;
  shred?: boolean;
  dpl?: number;
  retention?: number | string;
  retentionString?: string;
  retentionClass?: string;
  hashScheme?: string;
  hash?: string;
  customMetadata?: boolean;
  customMetadataAnnotation?: string;
  acl?: boolean;
  replicated?: boolean;
  replicationCollision?: boolean;
  index?: boolean;
  ingestTime?: number;
  ingestTimeMilliseconds?: string;
  ingestTimeString?: string;
  updateTime?: number;
  updateTimeString?: string;
  accessTime?: number;
  accessTimeString?: string;
  uid?: number;
  gid?: number;
  permissions?: number | string;
  owner?: string;
  type?: string;
}

export interface ObjectQueryInfo {
  expression?: string;
}

export interface OperationQueryInfo {
  start?: number | string;
  end?: number | string;
}

export interface ObjectQueryResponse {
  query?: ObjectQueryInfo;
  status: QueryStatus;
  resultSet: QueryResultObject[];
}

export interface OperationQueryResponse {
  query?: OperationQueryInfo;
  status: QueryStatus;
  resultSet: QueryResultObject[];
}

export const search_objects = command(
  z.object({
    tenant: z.string(),
    query: z.string(),
    count: z.number().optional(),
    offset: z.number().optional(),
    verbose: z.boolean().optional(),
    sort: z.string().optional(),
  }),
  async ({ tenant, ...params }) => {
    const body: Record<string, unknown> = { query: params.query };
    if (params.count != null) body.count = params.count;
    if (params.offset != null) body.offset = params.offset;
    if (params.verbose != null) body.verbose = params.verbose;
    if (params.sort) body.sort = params.sort;

    const res = await apiFetch(
      `/api/v1/query/tenants/${encodeURIComponent(tenant)}/objects`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: "" }));
      const detail = err.detail || "";
      if (res.status === 403) {
        error(
          403,
          detail ||
            "Access denied. Ensure the user has SEARCH permission and that namespaces have search enabled.",
        );
      }
      if (res.status === 400) {
        error(
          400,
          detail || "Invalid query syntax. Check your query expression.",
        );
      }
      if (res.status === 502 || res.status === 504) {
        error(
          res.status,
          detail ||
            "Could not reach the HCP query engine. Verify that the HCP system is reachable and search is enabled.",
        );
      }
      error(res.status, detail || `Object query failed (HTTP ${res.status})`);
    }
    return (await res.json()) as ObjectQueryResponse;
  },
);

export const search_operations = command(
  z.object({
    tenant: z.string(),
    count: z.number().optional(),
    verbose: z.boolean().optional(),
    transactions: z.array(z.string()).optional(),
    namespaces: z.array(z.string()).optional(),
    changeTimeFrom: z.string().optional(),
    changeTimeTo: z.string().optional(),
  }),
  async ({ tenant, ...params }) => {
    const body: Record<string, unknown> = {};
    if (params.count != null) body.count = params.count;
    if (params.verbose != null) body.verbose = params.verbose;

    const systemMetadata: Record<string, unknown> = {};
    if (params.changeTimeFrom || params.changeTimeTo) {
      const changeTime: Record<string, string> = {};
      if (params.changeTimeFrom) changeTime.start = params.changeTimeFrom;
      if (params.changeTimeTo) changeTime.end = params.changeTimeTo;
      systemMetadata.changeTime = changeTime;
    }
    if (params.namespaces?.length) {
      // HCP expects namespace names qualified with tenant: "ns-name.tenant-name"
      systemMetadata.namespaces = {
        namespace: params.namespaces.map((ns) =>
          ns.includes(".") ? ns : `${ns}.${tenant}`
        ),
      };
    }
    if (params.transactions?.length) {
      systemMetadata.transactions = { transaction: params.transactions };
    }
    if (Object.keys(systemMetadata).length > 0) {
      body.systemMetadata = systemMetadata;
    }

    const res = await apiFetch(
      `/api/v1/query/tenants/${encodeURIComponent(tenant)}/operations`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: "" }));
      const detail = err.detail || "";
      if (res.status === 403) {
        error(
          403,
          detail ||
            "Access denied. Ensure the user has SEARCH permission on the target namespaces.",
        );
      }
      if (res.status === 400) {
        error(400, detail || "Invalid query parameters.");
      }
      if (res.status === 502 || res.status === 504) {
        error(
          res.status,
          detail ||
            "Could not reach the HCP query engine. Verify that the HCP system is reachable.",
        );
      }
      error(
        res.status,
        detail || `Operation query failed (HTTP ${res.status})`,
      );
    }
    return (await res.json()) as OperationQueryResponse;
  },
);
