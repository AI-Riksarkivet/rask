import type { PageServerLoad } from './$types';
import { ACTIVE_PROJECT_COOKIE } from '@rask/api/bff';

// Opening a project IS entering it (#103): the page load stamps the host-wide active-project
// cookie, and every zone's `zoneLayoutLoad` reads it back — which is what lets the shared shell
// name the project you are inside from the very first server render of any zone. `httpOnly: false`
// so shell chrome could read it client-side too; it carries a project SLUG, not a secret.
export const load: PageServerLoad = ({ params, cookies }) => {
	cookies.set(ACTIVE_PROJECT_COOKIE, params.project, {
		path: '/',
		sameSite: 'lax',
		httpOnly: false,
		maxAge: 60 * 60 * 24 * 365,
	});
	return { activeProject: params.project };
};
