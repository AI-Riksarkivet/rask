// LEGACY PATH — `/projects` moved to `/tasks` with the vocabulary unification. Same reasoning as
// the sibling `[id]` redirect: old links keep landing, the list lives at the zone landing.
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';
import type { PageLoad } from './$types';

export const load: PageLoad = () => {
	redirect(307, `${base}/`);
};
