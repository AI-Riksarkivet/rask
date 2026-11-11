/**
 * The active-route matchers the sidebar's rows are built with (`nav.ts`): each turns an href into the
 * predicate a row lights by, against the full pathname.
 */

/** Drop a single trailing slash (except on root "/"), so every matcher is trailing-slash-robust. */
export const norm = (p: string) => (p.length > 1 && p.endsWith('/') ? p.slice(0, -1) : p);

/** Prefix-segment match: active when the path equals the href or is nested under it. */
export const seg = (href: string) => (p: string) =>
	norm(p) === href || norm(p).startsWith(href + '/');

/** Exact match: active ONLY on this exact path, for a row whose href prefixes every other route
 *  (Home = `/`), where `seg` would light it everywhere. */
export const exact = (href: string) => (p: string) => norm(p) === href;

/** Domain match: active when the path is under any of the given prefixes. */
export const under =
	(...prefixes: string[]) =>
	(p: string) =>
		prefixes.some((pre) => norm(p) === pre || norm(p).startsWith(pre + '/'));
