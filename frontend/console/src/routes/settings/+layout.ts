import { requireEstateAdmin } from '#lib/estate-admin.ts';
import type { LayoutLoad } from './$types';

// `/settings/**` — PLATFORM configuration. A LAYOUT load, so a new child page cannot be left ungated;
// the rule is `requireEstateAdmin`'s, shared with `/admin/**`.
export const load: LayoutLoad = async ({ parent }) => {
	const { me } = await parent();
	const admin = requireEstateAdmin(me, 'Settings');
	// The signed-in caller's principal key (`<idp-id>~<user id>`), the form a grant names as written.
	return { meSubject: admin.sub };
};
