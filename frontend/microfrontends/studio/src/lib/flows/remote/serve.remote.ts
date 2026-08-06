/**
 * Serve discovery for the Model node — a remote `query()` over @rask/api's
 * `serveApplications`, passing `getRequestEvent().fetch` (the compute zone's
 * pattern: request-scoped fetch resolves the relative `/api/serve/*` URL via
 * this zone's `handleFetch` gateway rewrite). Read-only introspection; the
 * gateway's `/api/serve` row is GET-only by design.
 */
import { getRequestEvent, query } from '$app/server';
import { serveApplications } from '@rask/api';
import type { ServeAppsResult } from '../serve-contract';

/** Live Serve apps, reduced for the picker. Refreshed on demand (mount + the
 *  refresh button) — no poll loop; a sandbox reads the list when it needs it. */
export const getServeApps = query(async (): Promise<ServeAppsResult> => {
	try {
		const payload = await serveApplications(getRequestEvent().fetch);
		const apps = Object.values(payload.applications).map((a) => ({
			name: a.name,
			app: (a.route_prefix ?? `/${a.name}`).replace(/^\//, ''),
			status: a.status,
		}));
		return { ok: true, apps };
	} catch (err) {
		return { ok: false, detail: err instanceof Error ? err.message : String(err) };
	}
});
