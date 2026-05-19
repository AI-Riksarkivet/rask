import { Buffer } from "node:buffer";
import type { Cookies } from "@sveltejs/kit";
import { COOKIE_SECURE } from "./env.js";
import type { TenantSession } from "$lib/types/session.js";

export type { TenantSession };

const COOKIE_PREFIX = "hcp_token__";
const ACTIVE_COOKIE = "hcp_token";
const SYSADMIN_SLUG = "__sysadmin";

function sanitize(value: string): string {
  return value.replace(/[^a-zA-Z0-9._-]/g, "_");
}

function sessionCookieName(
  tenant: string | undefined,
  username: string,
): string {
  const tenantPart = tenant ? sanitize(tenant) : SYSADMIN_SLUG;
  const userPart = sanitize(username);
  return `${COOKIE_PREFIX}${tenantPart}__${userPart}`;
}

function parseJwtPayload(token: string): Record<string, unknown> {
  try {
    const payload = token.split(".")[1];
    const decoded = Buffer.from(payload, "base64url").toString("utf-8");
    return JSON.parse(decoded);
  } catch {
    return {};
  }
}

const cookieOptions = {
  path: "/",
  httpOnly: true,
  sameSite: "lax" as const,
  secure: COOKIE_SECURE,
  maxAge: 60 * 60 * 8,
};

export function enumerateSessions(
  cookies: Cookies,
  activeToken: string | undefined,
): TenantSession[] {
  const all = cookies.getAll();
  const now = Math.floor(Date.now() / 1000);
  const sessions: TenantSession[] = [];

  for (const { name, value } of all) {
    if (!name.startsWith(COOKIE_PREFIX)) continue;

    const claims = parseJwtPayload(value);
    const tenant = claims.tenant as string | undefined;
    const username = (claims.sub as string) ?? "Unknown";
    const exp = (claims.exp as number) ?? 0;

    sessions.push({
      tenant,
      username,
      exp,
      expired: exp > 0 && exp < now,
      cookieName: name,
      isActive: activeToken === value,
    });
  }

  return sessions;
}

export function setSessionCookies(
  cookies: Cookies,
  token: string,
  tenant: string | undefined,
): void {
  const claims = parseJwtPayload(token);
  const username = (claims.sub as string) ?? "unknown";
  const cookieName = sessionCookieName(tenant, username);
  cookies.set(cookieName, token, cookieOptions);
  cookies.set(ACTIVE_COOKIE, token, cookieOptions);
}

export function switchSession(
  cookies: Cookies,
  targetCookieName: string,
): boolean {
  const token = cookies.get(targetCookieName);
  if (!token) return false;
  cookies.set(ACTIVE_COOKIE, token, cookieOptions);
  return true;
}

export function deleteAllSessions(cookies: Cookies): void {
  const all = cookies.getAll();
  for (const { name } of all) {
    if (name === ACTIVE_COOKIE || name.startsWith(COOKIE_PREFIX)) {
      cookies.delete(name, { path: "/" });
    }
  }
}

export function deleteActiveSession(cookies: Cookies): void {
  const activeToken = cookies.get(ACTIVE_COOKIE);
  const all = cookies.getAll();

  for (const { name, value } of all) {
    if (name.startsWith(COOKIE_PREFIX) && value === activeToken) {
      cookies.delete(name, { path: "/" });
      break;
    }
  }

  const remaining = all.filter(
    (c) => c.name.startsWith(COOKIE_PREFIX) && c.value !== activeToken,
  );
  if (remaining.length > 0) {
    cookies.set(ACTIVE_COOKIE, remaining[0].value, cookieOptions);
  } else {
    cookies.delete(ACTIVE_COOKIE, { path: "/" });
  }
}
