/**
 * How a person reads a catalog object: by its path, warehouse first, then each namespace, then the
 * table, and never by the flat `$` identifier the doors take (that one is for copying, beside the name).
 *
 * The breadcrumb of a catalog object's page is that path too, each ancestor a link to its own page, so
 * a table leads back through its namespace to its warehouse rather than to the flat list it was opened
 * from. Every id names its whole path, so the trail is read off the id alone, with no request.
 */
import type { ShellCrumb } from '#lib/shell/index.ts';
import { DELIMITER, childId } from '#lib/api/identifiers.ts';

/** The separator a person reads between the levels of a path. */
const PATH_SEPARATOR = ' › ';

/** The object's path for a person: `acme$bronze$orders` reads `acme › bronze › orders`. */
export function catalogPath(id: string): string {
	return id.split(DELIMITER).join(PATH_SEPARATOR);
}

const enc = encodeURIComponent;

/** The page of the object whose id is `id`, `segments` deep: a warehouse, a namespace, or the table. */
function pageOf(id: string, depth: number, isTable: boolean): string {
	if (depth === 1) return `/catalog/warehouses/${enc(id)}`;
	return isTable ? `/catalog/tables/${enc(id)}` : `/catalog/namespaces/${enc(id)}`;
}

/** The route parameter that names the object on each catalog object page. */
const OBJECT_PAGES: Readonly<Record<string, { param: string; table: boolean }>> = {
	'/catalog/warehouses/[id]': { param: 'id', table: false },
	'/catalog/namespaces/[id]': { param: 'id', table: false },
	'/catalog/tables/[table]': { param: 'table', table: true },
};

/**
 * The breadcrumb for the route `routeId` with `params`: Catalog, Warehouses, then one crumb per level of
 * the object's path, the last the object itself. `null` for a route that is no catalog object's page,
 * whose breadcrumb is its own path.
 */
export function catalogTrail(
	routeId: string | null,
	params: Partial<Record<string, string>>,
): ShellCrumb[] | null {
	const catalog: ShellCrumb = { id: '/catalog', label: 'Catalog', href: '/catalog', page: false };
	const route = routeId === null ? undefined : OBJECT_PAGES[routeId];
	const id = route === undefined ? undefined : params[route.param];
	if (route === undefined || !id) return null;
	const segments = id.split(DELIMITER);
	const levels = segments.map((label, i): ShellCrumb => {
		const prefix = segments.slice(1, i + 1).reduce(childId, segments[0] ?? '');
		const href = pageOf(prefix, i + 1, route.table && i === segments.length - 1);
		return { id: href, label, href, page: true };
	});
	return [
		catalog,
		{ id: '/catalog/warehouses', label: 'Warehouses', href: '/catalog/warehouses', page: true },
		...levels,
	];
}
