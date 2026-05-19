export interface TemplateNamespace {
  name: string;
  description?: string;
  hardQuota?: string;
  softQuota?: number;
  hashScheme?: string;
  searchEnabled?: boolean;
  versioningEnabled?: boolean;
  tags?: { tag: string[] } | string[];
  versioning?: { enabled: boolean };
  compliance?: Record<string, unknown>;
  permissions?: Record<string, unknown>;
  protocols?: {
    http?: Record<string, unknown>;
    cifs?: Record<string, unknown>;
    nfs?: Record<string, unknown>;
    smtp?: Record<string, unknown>;
  };
  retentionClasses?: {
    name: string;
    value: string;
    description?: string;
    allowDisposition?: boolean;
  }[];
  indexing?: Record<string, unknown>;
  cors?: Record<string, unknown> | string;
  replicationCollision?: Record<string, unknown>;
}

export interface ImportStep {
  ns: string;
  step: string;
  status: "pending" | "running" | "done" | "failed";
  error?: string;
}

export const PROTOCOLS = ["http", "cifs", "nfs", "smtp"] as const;

export const PERMISSION_KEYS = [
  { key: "readAllowed", label: "Read" },
  { key: "writeAllowed", label: "Write" },
  { key: "deleteAllowed", label: "Delete" },
  { key: "purgeAllowed", label: "Purge" },
  { key: "searchAllowed", label: "Search" },
  { key: "readAclAllowed", label: "Read ACL" },
  { key: "writeAclAllowed", label: "Write ACL" },
] as const;

/** Extract tags as a flat string[] regardless of API format ({tag: string[]} or string[]). */
export function getTags(tags: TemplateNamespace["tags"]): string[] {
  if (!tags) return [];
  if (Array.isArray(tags)) return tags;
  if (typeof tags === "object" && "tag" in tags && Array.isArray(tags.tag)) {
    return tags.tag;
  }
  return [];
}
