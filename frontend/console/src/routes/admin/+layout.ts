import { requireEstateAdmin } from '#lib/estate-admin.ts';
import type { LayoutLoad } from './$types';

// `/admin/**` — operations. A LAYOUT load, so a new child page cannot be left ungated; the rule is
// `requireEstateAdmin`'s, shared with `/settings/**`.
export const load: LayoutLoad = async ({ parent }) => {
	const { me } = await parent();
	requireEstateAdmin(me, 'the admin area', 'Admin is estate-admin only');
	return { estateAdmin: true };
};
