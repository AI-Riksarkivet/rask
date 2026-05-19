import type { Handle, HandleServerError } from "@sveltejs/kit";
import { enumerateSessions } from "$lib/server/sessions.js";
import { BACKEND_URL } from "$lib/server/env.js";

console.info(`[frontend] started — BACKEND_URL=${BACKEND_URL}`);

export const handle: Handle = ({ event, resolve }) => {
  const token = event.cookies.get("hcp_token");
  if (token) {
    event.locals.token = token;
  }
  event.locals.sessions = enumerateSessions(event.cookies, token);
  return resolve(event);
};

export const handleError: HandleServerError = ({ error, event, status }) => {
  const msg = error instanceof Error ? error.message : String(error);
  console.error(
    `[frontend] ${event.request.method} ${event.url.pathname} → ${status}: ${msg}`,
  );
  if (error instanceof Error && error.stack) console.error(error.stack);
  return {
    message: msg,
  };
};
