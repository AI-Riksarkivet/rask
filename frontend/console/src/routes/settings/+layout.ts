import { requireEstateAdmin } from '$lib/estate-admin';
import type { LayoutLoad } from './$types';

// `/settings/**` — PLATFORM configuration. A LAYOUT load, so a new child page cannot be left ungated;
// the rule is `requireEstateAdmin`'s, shared with `/admin/**`. The non-admin refusal names the
// relation the FGA denial names.
export const load: LayoutLoad = async ({ parent }) => {
	const { me } = await parent();
	const admin = requireEstateAdmin(
		me,
		'estate settings',
		'Estate settings are estate-admin only — your identity lacks can_observe_events on the FGA root.',
	);
	// The signed-in caller's principal key (`<idp-id>~<user id>`), the form a grant names as written.
	return { meSubject: admin.sub };
};
