import { error } from '@sveltejs/kit';
import type { Me } from '@rask/api';

/**
 * The estate-admin guard in front of `/admin/**` and `/settings/**`, ONE rule for both areas.
 *
 * What the catalog answers for `/management/v1/me` decides, on every stack: an auth-off catalog
 * answers estate-admin for its anonymous caller, so both areas open there, and a catalog that cannot
 * answer keeps both shut. Every refusal is 403, never 404: the navbar names both areas, so a 404 would
 * claim a page does not exist when it does, and it would hide the next step from someone who may be
 * granted it. The two refusals say different things — "could not be confirmed" sends a person to their
 * session or the catalog, a named relation sends them to whoever grants it.
 *
 * This guard only decides what the console OFFERS, and a person can bypass it. What authorizes is the
 * catalog's and lineage's own check on every request behind these pages.
 */
export function requireEstateAdmin(me: Me | null, area: string, refusal: string): Me {
	if (!me) error(403, `Your identity could not be confirmed, so ${area} cannot be opened.`);
	if (!me.estate_admin) error(403, refusal);
	return me;
}
