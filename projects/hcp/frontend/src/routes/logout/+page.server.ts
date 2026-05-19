import { redirect } from "@sveltejs/kit";
import type { PageServerLoad } from "./$types.js";
import {
  deleteActiveSession,
  deleteAllSessions,
} from "$lib/server/sessions.js";

export const load: PageServerLoad = ({ cookies, url }) => {
  const logoutAll = url.searchParams.get("all") === "true";

  if (logoutAll) {
    deleteAllSessions(cookies);
    redirect(302, "/login");
  }

  deleteActiveSession(cookies);
  const remaining = cookies.get("hcp_token");
  redirect(302, remaining ? "/namespaces" : "/login");
};
