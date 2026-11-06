import { error } from '@sveltejs/kit';
import { fetchMe, MeSchema, parse, type Me } from '@rask/api';
import { requestJSON } from '@rask/api/client';
import { auth, initAuth, login, sessionToUser } from '#lib/auth.svelte.ts';
import { readActiveProject } from '#lib/active-project.ts';
import { CATALOG_API } from '#lib/upstreams.ts';
import type { LayoutLoad } from './$types';

// A single-page app: every route renders in the browser, from the static `index.html` fallback.
export const ssr = false;
export const prerender = false;

/**
 * The layout contract every page reads: the signed-in identity, whether this stack signs people in,
 * the active project, and the RESOLVED `/management/v1/me` — so the navbar renders its final entry set.
 *
 * SIGN-IN FIRST. On a stack that signs people in, a signed-out visit to any page goes to the issuer,
 * and comes back to the page it asked for. The `/auth/*` pages are the round trip itself.
 *
 * `me` is null for BOTH "signed out" and "signed in but /management/v1/me was unreachable / 401 /
 * drifted", and those are opposite situations to a user, so `hasSession` rides beside it. On a stack
 * that signs nobody in, the catalog is asked anyway, with no bearer: an auth-off catalog answers for
 * its anonymous caller, and the console renders what it says.
 */
export const load: LayoutLoad = async ({ params, url, untrack }) => {
	await initAuth();
	if (auth.configError) error(503, `Sign-in is not configured: ${auth.configError}`);
	const path = untrack(() => url.pathname + url.search);
	if (auth.enabled && !auth.session && !path.startsWith('/auth/')) {
		await login(path);
		// The browser is leaving for the issuer; render nothing meanwhile.
		await new Promise<never>(() => {});
	}
	return {
		user: sessionToUser(auth.session),
		authEnabled: auth.enabled,
		hasSession: auth.session !== null,
		activeProject: params.project ?? readActiveProject(),
		me: auth.enabled
			? await fetchMe({ catalogUrl: CATALOG_API, accessToken: auth.session?.accessToken })
			: await anonymousMe(),
	};
};

/** `/management/v1/me` without a bearer, for a stack that signs nobody in; null on any failure. */
async function anonymousMe(): Promise<Me | null> {
	const res = await requestJSON(CATALOG_API, 'management/v1/me');
	if (!res.ok) return null;
	try {
		return parse(MeSchema, res.data);
	} catch {
		return null;
	}
}
