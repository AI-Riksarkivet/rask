import { redirect } from "@sveltejs/kit";
import type { AccessLevel } from "$lib/constants.js";

export function requireAdmin(accessLevel: AccessLevel) {
  if (accessLevel === "namespace-user") {
    redirect(302, "/buckets");
  }
}

export function requireSysAdmin(accessLevel: AccessLevel) {
  if (accessLevel !== "sys-admin") {
    redirect(302, "/buckets");
  }
}
