import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

/**
 * Home (/) -> redirect to /batches.
 *
 * The batches table is the canonical entry point now: open a batch from there
 * and click into the viewer. The old "type a volume id" form lived in
 * +page.svelte; we removed it.
 */
export const load: PageLoad = () => {
	redirect(302, '/batches');
};
