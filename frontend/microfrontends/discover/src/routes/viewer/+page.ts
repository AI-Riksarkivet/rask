import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';
import { listBatches } from '@rask/api';
import type { PageLoad } from './$types';

/**
 * `/viewer` (no volume) -> pick a volume and 302 into it.
 *
 * The viewer needs a volume in the URL (`viewer/[volume]` has no index), so a
 * bare "Viewer" nav link has nothing to render. Resolve the first volume that
 * actually has cached images and redirect to `/viewer/<volume>` (which in turn
 * 302s to its first page). With no cached volumes, fall back to Browse so the
 * user can pick one.
 *
 * Pass SvelteKit's `fetch` so `@rask/api`'s relative /api URL resolves under SSR.
 */
export const load: PageLoad = async ({ fetch }) => {
	const { batches } = await listBatches(fetch);
	const first = batches.find((b) => b.cached_pages > 0);
	if (!first) redirect(302, `${base}/browse`);
	redirect(302, `${base}/viewer/${encodeURIComponent(first.batch_id)}`);
};
