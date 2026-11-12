import { redirect } from '@sveltejs/kit';

// `/catalog` is a GROUP, not a page: its lists are the surfaces, and a project's own page is its
// overview, so the breadcrumb names it without a link. A bookmark of the route lands on the table
// list, the catalog's widest view, rather than on a 404.
export const load = () => redirect(307, '/catalog/tables');
