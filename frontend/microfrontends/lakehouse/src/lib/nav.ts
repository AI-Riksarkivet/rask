import { base } from '$app/paths';
import type { ZoneNav } from '@rask/ui/shell';
import { DATA_ZONE_NAV } from '$lib/data/nav';
import { LINEAGE_ZONE_NAV } from '$lib/lineage/nav';
import { MODELS_ZONE_NAV } from '$lib/models/nav';
import { ADMIN_ZONE_NAV } from '$lib/admin/nav';

/**
 * The lakehouse zone hosts four areas — catalog (`data`), `lineage`, `models` and `admin` — that used
 * to be four separate SvelteKit apps. They were split by URL prefix but not by domain: all four read
 * the SAME catalog + lineage planes through the same client, the top navbar already presented them as
 * one "Lakehouse" surface, and three of them carried byte-identical copies of the lineage client. The
 * split cost four SSR servers, four copies of the shell in four bundles, and a full document reload on
 * every hop between them, and bought nothing — every zone shipped from one image tag anyway.
 *
 * Merged, a hop between areas is a SOFT navigation. What the sidebar must still do is show only the
 * CURRENT area's routes, so this resolves the area from the path — the same two-level routing split as
 * before (the shell owns the namespace boundary, the area owns its interior), except now both levels
 * live in one router.
 */
const AREAS: Record<string, ZoneNav> = {
	data: DATA_ZONE_NAV,
	lineage: LINEAGE_ZONE_NAV,
	models: MODELS_ZONE_NAV,
	admin: ADMIN_ZONE_NAV,
};

/** The area segment right after this zone's base — `''` on the zone root. */
export function areaOf(pathname: string): string {
	const rest = pathname.startsWith(base) ? pathname.slice(base.length) : pathname;
	return rest.split('/').filter(Boolean)[0] ?? '';
}

/** The sidebar config for whichever area the current path is in; the catalog is the zone's landing. */
export function lakehouseNav(pathname: string): ZoneNav {
	return AREAS[areaOf(pathname)] ?? DATA_ZONE_NAV;
}
