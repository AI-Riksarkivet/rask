import { error, redirect } from '@sveltejs/kit';
import { listPages } from '$lib/api';
import type { PageLoad } from './$types';

/**
 * `/viewer/<batch>` -> redirect to `/viewer/<batch>/<first-page-key>`.
 *
 * The viewer wants both volume + page in the URL, so a bare batch id can't
 * render anything itself. Resolve the first key here and 302 the browser.
 */
export const load: PageLoad = async ({ params, fetch }) => {
	const volume = params.volume;
	if (!volume) error(400, 'missing volume');
	const pages = await listPages(volume);
	if (pages.length === 0) error(404, `no pages cached for ${volume}`);
	redirect(302, `/viewer/${volume}/${encodeURIComponent(pages[0].key)}`);
};
