import { base } from '$app/paths';
import { redirect } from '@sveltejs/kit';

// discover's zone root (/discover) has no landing of its own — send it to
// Search, which is the nav-config parent target for the Discover group. Without this,
// a direct/bookmarked hit to the bare zone root 404s (the other apps each ship a root
// route). Universal load so the redirect happens on both server and client nav.
export const load = () => {
	redirect(307, `${base}/search`);
};
