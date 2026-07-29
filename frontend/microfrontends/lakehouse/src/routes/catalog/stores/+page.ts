import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

// `/lakehouse/catalog/stores` moved under the storage view as its "By tier" tab. Kept as a redirect
// rather than deleted: the route was linked from the sidebar and is in browser histories, and a
// bookmark that 404s is a worse answer than one that lands where the view now lives.
//
// `redirect-truth.test.ts` gates this target, so if the tab is ever renamed this fails in CI instead
// of becoming the next dead link — which is exactly how `/lakehouse` came to 404 after the
// data -> catalog rename.
export const load = () => redirect(308, `${base}/catalog/storage/tiers`);
