/**
 * The project filter of a catalog list's own page, kept in the URL (`?project=<id>`) so a reload and
 * a shared link show the same rows. A list mounted elsewhere (a workbench panel) keeps its filter in
 * its own state instead: choosing a project must not take the reader off the page it is on.
 */
import { goto } from '$app/navigation';
import { resolve } from '$app/paths';
import { queryWithProject, type WithQuery } from './project-filter.ts';

/** The catalog lists that carry a project filter, by their route. */
const LIST_ROUTES = {
	warehouses: '/catalog/warehouses',
	namespaces: '/catalog/namespaces',
	tables: '/catalog/tables',
	promotions: '/catalog/promotions',
} as const;

export type CatalogList = keyof typeof LIST_ROUTES;

/** Show `list` filtered to `project` (`''`: every project), as a history entry of its own, so Back
 *  returns to the rows as they were. */
export function showProject(list: CatalogList, url: WithQuery, project: string): Promise<void> {
	const route = LIST_ROUTES[list];
	const query = queryWithProject(url, project);
	return goto(query ? resolve(`${route}?${query}`) : resolve(route));
}
