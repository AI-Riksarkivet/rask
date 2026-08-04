import { error } from '@sveltejs/kit';
import { loadGallery } from '$lib/gallery';
import type { PageServerLoad } from './$types';

// `/settings` — ESTATE configuration, the third place in the main menu (Home · Projects · Settings).
//
// The gate is SERVER-SIDE and fail-closed, and it has to be: the navbar hides the Settings entry
// from a non-admin, but hiding a link is not authorization — anyone can type the URL. `loadGallery`
// already resolves the caller's identity through the frozen `/v1/me` contract (including the
// "a session exists but the catalog cannot confirm who" case), so the answer comes from the same
// read the rest of the estate trusts rather than a second, drifting one.
//
// 404 rather than 403 on purpose, and only for a resolvable non-admin: a viewer who may not
// configure the estate should not learn that estate configuration exists here. An UNRESOLVED
// identity gets 403 instead — that is a different thing (the catalog could not answer), and
// answering "no such page" would send someone chasing a broken link when their session is the
// problem.
export const load: PageServerLoad = async (event) => {
	const gallery = await loadGallery(event);
	if (gallery.identityUnavailable) {
		error(403, 'Your identity could not be confirmed, so estate settings cannot be opened.');
	}
	if (!gallery.estateAdmin) error(404, 'Not found');
	return gallery;
};
