import type { LayoutServerLoad } from "./$types.js";
import { requireSysAdmin } from "$lib/server/guards.js";

export const load: LayoutServerLoad = async ({ parent }) => {
  const { accessLevel } = await parent();
  requireSysAdmin(accessLevel);
};
