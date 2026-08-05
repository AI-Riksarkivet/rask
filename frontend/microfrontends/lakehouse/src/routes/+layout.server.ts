import { env } from '$env/dynamic/private';
import type { LayoutServerLoad } from './$types';
import { zoneLayoutLoad } from '@rask/api/bff';
import { fetchMe } from '@rask/api';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// The estate-wide zone layout contract (identity + auth flag + active project), PLUS `me`
// RESOLVED server-side (#107): the navbar SSRs its final entry set, so a cross-zone landing
// paints the finished bar instead of skeleton pills that swap on hydration — which was the
// per-hop shell flash. fetchMe degrades to null (signed out / unreachable), never hangs.
export const load: LayoutServerLoad = async ({ locals, cookies }) => ({
	...zoneLayoutLoad({ locals, cookies }),
	me: await fetchMe({ catalogUrl: CATALOG_API, accessToken: locals.session?.accessToken }),
});
