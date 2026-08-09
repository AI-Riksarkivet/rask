import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

import type { PageLoad } from './$types';

/**
 * `/annotator/tasks` would 404 (as `/annotator/projects` once did).
 *
 * Every link in the estate points at the CHILD (`/tasks/<id>`), so the parent was never
 * exercised until someone trimmed the URL — and then a path that plainly looks like "the list of my
 * labeling tasks" answered 404. A parent that 404s while its children work is the worst of both: it
 * looks like a missing page rather than a path that was never meant to exist.
 *
 * It redirects rather than duplicating the list, because the zone landing IS that list — `/` renders
 * `ProjectsLanding` under the heading "Labeling tasks". A second surface listing the same thing is
 * two places to keep in agreement.
 *
 * 307, not 301: this is a routing decision, not a permanent identity for the resource, and a browser
 * that cached a 301 here would keep redirecting after the route grows into something real.
 */
export const load: PageLoad = () => {
	redirect(307, `${base}/`);
};
