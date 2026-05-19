export const APP_VERSION: string = typeof __APP_VERSION__ !== "undefined"
  ? __APP_VERSION__
  : "0.0.0";

export const AVAILABLE_ROLES = [
  "ADMINISTRATOR",
  "SECURITY",
  "MONITOR",
  "COMPLIANCE",
] as const;

export type Role = (typeof AVAILABLE_ROLES)[number];

export const ROLE_DESCRIPTIONS: Record<string, string> = {
  ADMINISTRATOR:
    "Full tenant administration — create namespaces, manage users and groups, view statistics",
  SECURITY:
    "Manage console security, search security, and authentication settings",
  MONITOR:
    "View tenant and namespace statistics and chargeback reports (read-only)",
  COMPLIANCE: "Manage compliance and retention settings on namespaces",
};

export const PERMISSION_DESCRIPTIONS: Record<string, string> = {
  BROWSE: "List objects in the namespace",
  READ: "Read object data and metadata",
  WRITE: "Create and modify objects",
  DELETE: "Delete objects",
  PURGE: "Permanently remove objects (bypass retention)",
  SEARCH: "Query objects via HCP metadata search engine",
  READ_ACL: "Read object access control lists",
  WRITE_ACL: "Modify object access control lists",
  CHOWN: "Change object ownership",
  PRIVILEGED: "Perform privileged operations like deleting retained objects",
};

export interface User {
  username: string;
  fullName?: string;
  description?: string;
  enabled?: boolean;
  localAuthentication?: boolean;
  roles?: { role?: string[] } | string[];
  userGUID?: string;
  userID?: number;
}

export function getUserRoles(user: User): string[] {
  if (!user.roles) return [];
  if (Array.isArray(user.roles)) return user.roles;
  return user.roles.role ?? [];
}

// ── Group Accounts ──────────────────────────────────────────────────

export const GROUP_ROLES = [
  "ADMINISTRATOR",
  "SECURITY",
  "MONITOR",
  "COMPLIANCE",
  "SERVICE",
  "SEARCH",
] as const;

export interface GroupAccount {
  groupname?: string;
  name?: string;
  description?: string;
  roles?: { role?: string[] } | string[];
  allowNamespaceManagement?: boolean;
}

export function getGroupName(group: GroupAccount): string {
  return group.groupname ?? group.name ?? "";
}

export function getGroupRoles(group: GroupAccount): string[] {
  if (!group.roles) return [];
  if (Array.isArray(group.roles)) return group.roles;
  return group.roles.role ?? [];
}

// ── Access Level ────────────────────────────────────────────────────

export type AccessLevel = "sys-admin" | "tenant-admin" | "namespace-user";

export function getAccessLevel(
  tenant: string | undefined,
  roles: string[],
): AccessLevel {
  if (!tenant) return "sys-admin";
  if (roles.includes("ADMINISTRATOR")) return "tenant-admin";
  return "namespace-user";
}
