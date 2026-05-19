export const ACL_PERMISSIONS = [
  {
    value: "FULL_CONTROL",
    label: "Full Control",
    description:
      "Grants READ, WRITE, READ_ACP, and WRITE_ACP — the grantee can read/write objects and manage ACLs.",
  },
  {
    value: "READ",
    label: "Read",
    description: "List objects in the bucket and read their contents.",
  },
  {
    value: "WRITE",
    label: "Write",
    description: "Create, overwrite, and delete objects in the bucket.",
  },
  {
    value: "READ_ACP",
    label: "Read ACP",
    description: "Read the bucket access control policy (ACL).",
  },
  {
    value: "WRITE_ACP",
    label: "Write ACP",
    description: "Modify the bucket access control policy (ACL).",
  },
] as const;

export const PERMISSION_MAP: ReadonlyMap<
  string,
  (typeof ACL_PERMISSIONS)[number]
> = new Map(ACL_PERMISSIONS.map((p) => [p.value, p]));

export function permissionColor(
  p: string,
): "default" | "secondary" | "destructive" | "outline" {
  if (p === "FULL_CONTROL") return "destructive";
  if (p === "WRITE" || p === "WRITE_ACP") return "default";
  return "secondary";
}

export function permissionLabel(p: string): string {
  return PERMISSION_MAP.get(p)?.label ?? p;
}
