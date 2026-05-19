import { z } from "zod";
import { command, query } from "$app/server";
import { apiFetch, throwIfNotOk } from "$lib/server/api.js";

export interface ContentProperty {
  name: string;
  expression: string;
  type: string;
  multivalued?: boolean;
  format?: string;
}

export interface ContentClass {
  name: string;
  contentProperties?: ContentProperty[];
  namespaces?: string[];
}

export const get_content_classes = query(
  z.object({ tenant: z.string() }),
  async ({ tenant }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/contentClasses?verbose=true`,
      );
      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data)) return data as ContentClass[];
        if (Array.isArray(data.name)) {
          return data.name.map((n: string) => ({ name: n })) as ContentClass[];
        }
        return (data.contentClasses ?? []) as ContentClass[];
      }
    } catch (err) {
      console.error("[content-classes.remote]", err);
    }
    return [] as ContentClass[];
  },
);

export const get_content_class = query(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    try {
      const res = await apiFetch(
        `/api/v1/mapi/tenants/${tenant}/contentClasses/${
          encodeURIComponent(name)
        }?verbose=true`,
      );
      if (res.ok) return (await res.json()) as ContentClass;
    } catch (err) {
      console.error("[content-classes.remote]", err);
    }
    return null;
  },
);

export const create_content_class = command(
  z.object({
    tenant: z.string(),
    name: z.string(),
    contentProperties: z.array(z.object({
      name: z.string(),
      expression: z.string(),
      type: z.string(),
      multivalued: z.boolean().optional(),
      format: z.string().optional(),
    })).optional(),
    namespaces: z.array(z.string()).optional(),
  }),
  async ({ tenant, ...body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/contentClasses`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to create content class");
  },
);

export const update_content_class = command(
  z.object({
    tenant: z.string(),
    name: z.string(),
    body: z.record(z.string(), z.unknown()),
  }),
  async ({ tenant, name, body }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/contentClasses/${
        encodeURIComponent(name)
      }`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    await throwIfNotOk(res, "Failed to update content class");
  },
);

export const delete_content_class = command(
  z.object({ tenant: z.string(), name: z.string() }),
  async ({ tenant, name }) => {
    const res = await apiFetch(
      `/api/v1/mapi/tenants/${tenant}/contentClasses/${
        encodeURIComponent(name)
      }`,
      { method: "DELETE" },
    );
    await throwIfNotOk(res, "Failed to delete content class");
  },
);
