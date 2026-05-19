import type { PageServerLoad } from "./$types.js";
import { requireAdmin } from "$lib/server/guards.js";

export const load: PageServerLoad = async ({ parent }) => {
  const { accessLevel } = await parent();
  requireAdmin(accessLevel);
};
