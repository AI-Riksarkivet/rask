import { redirect } from "@sveltejs/kit";
import type { PageServerLoad } from "./$types.js";

export const load: PageServerLoad = ({ locals }) => {
  if (locals.token) {
    redirect(302, "/namespaces");
  }
  redirect(302, "/login");
};
