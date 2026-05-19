import { query } from "$app/server";
import { z } from "zod";
import { apiFetch } from "$lib/server/api.js";

import type {
  LanceField,
  VectorPreviewEntry,
  VectorPreviewStats,
} from "$lib/types/lance.js";

export const get_lance_tables = query(
  z.object({ bucket: z.string(), path: z.string().optional() }),
  async ({ bucket, path }) => {
    try {
      const params = new URLSearchParams({ bucket });
      if (path) params.set("path", path);
      const res = await apiFetch(`/api/v1/lance/tables?${params}`);
      if (!res.ok) {
        if (res.status === 403) {
          return { tables: [] as string[], error: "Access denied" };
        }
        return { tables: [] as string[], error: "Failed to list tables" };
      }
      const data = await res.json();
      return { tables: data.tables as string[], error: null };
    } catch (err) {
      console.error("get_lance_tables error:", err);
      return { tables: [] as string[], error: "Connection failed" };
    }
  },
);

export const get_lance_schema = query(
  z.object({
    bucket: z.string(),
    path: z.string().optional(),
    table: z.string(),
  }),
  async ({ bucket, path, table }) => {
    try {
      const params = new URLSearchParams({ bucket, table });
      if (path) params.set("path", path);
      const res = await apiFetch(`/api/v1/lance/schema?${params}`);
      if (!res.ok) {
        return {
          fields: [] as LanceField[],
          table_name: table,
          error: "Failed",
        };
      }
      const data = await res.json();
      return { ...data, error: null } as {
        fields: LanceField[];
        table_name: string;
        error: null;
      };
    } catch (err) {
      console.error("get_lance_schema error:", err);
      return {
        fields: [] as LanceField[],
        table_name: table,
        error: "Connection failed",
      };
    }
  },
);

export const get_lance_rows = query(
  z.object({
    bucket: z.string(),
    path: z.string().optional(),
    table: z.string(),
    limit: z.number().optional(),
    offset: z.number().optional(),
    columns: z.string().optional(),
    filter: z.string().optional(),
  }),
  async ({ bucket, path, table, limit, offset, columns, filter }) => {
    try {
      const params = new URLSearchParams({ bucket, table });
      if (path) params.set("path", path);
      if (limit) params.set("limit", String(limit));
      if (offset) params.set("offset", String(offset));
      if (columns) params.set("columns", columns);
      if (filter) params.set("filter", filter);
      const res = await apiFetch(`/api/v1/lance/rows?${params}`);
      if (!res.ok) {
        return {
          rows: [],
          total: 0,
          limit: limit ?? 50,
          offset: offset ?? 0,
          error: "Failed",
        };
      }
      return { ...(await res.json()), error: null };
    } catch (err) {
      console.error("get_lance_rows error:", err);
      return {
        rows: [],
        total: 0,
        limit: limit ?? 50,
        offset: offset ?? 0,
        error: "Connection failed",
      };
    }
  },
);

export const search_lance = query(
  z.object({
    bucket: z.string(),
    path: z.string().optional(),
    table: z.string(),
    query: z.string().optional(),
    vector: z.string().optional(),
    vector_column: z.string().optional(),
    query_type: z.string().optional(),
    limit: z.number().optional(),
    filter: z.string().optional(),
    weight: z.number().optional(),
  }),
  async (
    {
      bucket,
      path,
      table,
      query: q,
      vector,
      vector_column,
      query_type,
      limit,
      filter,
      weight,
    },
  ) => {
    try {
      const params = new URLSearchParams({ bucket, table });
      if (path) params.set("path", path);
      if (q) params.set("query", q);
      if (vector) params.set("vector", vector);
      if (vector_column) params.set("vector_column", vector_column);
      if (query_type) params.set("query_type", query_type);
      if (limit) params.set("limit", String(limit));
      if (filter) params.set("filter", filter);
      if (weight != null) params.set("weight", String(weight));
      const res = await apiFetch(`/api/v1/lance/search?${params}`);
      if (!res.ok) {
        const body = await res.text();
        return { rows: [], total: 0, error: body || "Search failed" };
      }
      return { ...(await res.json()), error: null };
    } catch (err) {
      console.error("search_lance error:", err);
      return { rows: [], total: 0, error: "Connection failed" };
    }
  },
);

export const get_vector_preview = query(
  z.object({
    bucket: z.string(),
    path: z.string().optional(),
    table: z.string(),
    column: z.string(),
  }),
  async ({ bucket, path, table, column }) => {
    try {
      const params = new URLSearchParams({ bucket, table, column });
      if (path) params.set("path", path);
      const res = await apiFetch(`/api/v1/lance/vector-preview?${params}`);
      if (!res.ok) {
        return { stats: null, preview: [] as VectorPreviewEntry[] };
      }
      return (await res.json()) as {
        stats: VectorPreviewStats | null;
        preview: VectorPreviewEntry[];
      };
    } catch (err) {
      console.error("get_vector_preview error:", err);
      return { stats: null, preview: [] as VectorPreviewEntry[] };
    }
  },
);
