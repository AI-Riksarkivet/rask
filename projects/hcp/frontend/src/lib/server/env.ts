import { dev } from "$app/environment";
import { env } from "$env/dynamic/private";

export const BACKEND_URL = env.BACKEND_URL ?? "http://127.0.0.1:8000";

/** Cookie secure flag: defaults to true in production, false in dev. Override with COOKIE_SECURE=true|false */
export const COOKIE_SECURE = env.COOKIE_SECURE
  ? env.COOKIE_SECURE === "true"
  : !dev;
