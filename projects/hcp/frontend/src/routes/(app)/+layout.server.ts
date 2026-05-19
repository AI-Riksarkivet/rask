import { redirect } from "@sveltejs/kit";
import type { LayoutServerLoad } from "./$types.js";
import { Buffer } from "node:buffer";
import { BACKEND_URL } from "$lib/server/env.js";
import { getAccessLevel } from "$lib/constants.js";

function parseJwtPayload(token: string): Record<string, unknown> {
  try {
    const payload = token.split(".")[1];
    const decoded = Buffer.from(payload, "base64url").toString("utf-8");
    return JSON.parse(decoded);
  } catch {
    return {};
  }
}

async function fetchUserProfile(
  token: string,
  tenant: string,
  username: string,
): Promise<{ userGUID: string | undefined; roles: string[] }> {
  try {
    const res = await fetch(
      `${BACKEND_URL}/api/v1/mapi/tenants/${tenant}/userAccounts/${
        encodeURIComponent(username)
      }?verbose=true`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    if (res.ok) {
      const data = await res.json();
      return {
        userGUID: data.userGUID as string | undefined,
        roles: (data.roles?.role as string[]) ?? [],
      };
    }
  } catch {
    // Non-critical — degrade gracefully
  }
  return { userGUID: undefined, roles: [] };
}

export const load: LayoutServerLoad = async ({ locals }) => {
  if (!locals.token) {
    redirect(302, "/login");
  }
  const claims = parseJwtPayload(locals.token);
  const username = (claims.sub as string) ?? "User";
  const tenant = claims.tenant as string | undefined;
  const profile = tenant
    ? await fetchUserProfile(locals.token, tenant, username)
    : { userGUID: undefined, roles: [] as string[] };
  const accessLevel = getAccessLevel(tenant, profile.roles);
  return {
    authenticated: true,
    username,
    tenant,
    userGUID: profile.userGUID,
    roles: profile.roles,
    accessLevel,
    sessions: locals.sessions,
  };
};
