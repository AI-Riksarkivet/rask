import { json } from "@sveltejs/kit";
import type { RequestHandler } from "./$types.js";
import { switchSession } from "$lib/server/sessions.js";

export const POST: RequestHandler = async ({ request, cookies }) => {
  const { cookieName } = await request.json();

  if (
    !cookieName ||
    typeof cookieName !== "string" ||
    !cookieName.startsWith("hcp_token__")
  ) {
    return json({ error: "Invalid cookie name" }, { status: 400 });
  }

  const success = switchSession(cookies, cookieName);
  if (!success) {
    return json({ error: "Session not found" }, { status: 404 });
  }

  return json({ ok: true });
};
