import { fail, redirect } from "@sveltejs/kit";
import { loginSchema } from "$lib/api/schemas.js";
import type { Actions, PageServerLoad } from "./$types.js";
import { BACKEND_URL } from "$lib/server/env.js";
import { setSessionCookies } from "$lib/server/sessions.js";

export const load: PageServerLoad = ({ locals, url }) => {
  // Allow accessing login with an active session when adding another tenant
  if (locals.token && !url.searchParams.has("add")) {
    redirect(302, "/namespaces");
  }
  return {
    hasExistingSession: !!locals.token,
    prefillTenant: url.searchParams.get("tenant") ?? undefined,
  };
};

export const actions = {
  default: async ({ request, cookies }) => {
    const formData = await request.formData();
    const data = {
      tenant: (formData.get("tenant") as string) || undefined,
      username: formData.get("username") as string,
      password: formData.get("password") as string,
    };

    const result = loginSchema.safeParse(data);
    if (!result.success) {
      const firstError = result.error.issues[0];
      return fail(400, { error: firstError.message });
    }

    try {
      const body = new URLSearchParams({
        grant_type: "password",
        username: result.data.username,
        password: result.data.password,
      });
      if (result.data.tenant) {
        body.set("tenant", result.data.tenant);
      }

      const response = await fetch(`${BACKEND_URL}/api/v1/auth/token`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
      });

      if (!response.ok) {
        const error = await response.json().catch(() => ({
          detail: "Authentication failed",
        }));
        return fail(401, {
          error: error.detail ?? "Invalid username or password",
        });
      }

      const tokenData = await response.json();

      setSessionCookies(cookies, tokenData.access_token, result.data.tenant);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "unknown error";
      console.error(`[login] fetch failed: ${msg}`);
      return fail(500, {
        error: `Unable to connect to backend service`,
      });
    }

    redirect(303, "/namespaces");
  },
} satisfies Actions;
