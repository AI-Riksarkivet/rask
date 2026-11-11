import { redirect } from '@sveltejs/kit';

// `/catalog` is a GROUP, not a page: its lists are the surfaces, and a project's own page is its
// overview. The breadcrumb's Catalog crumb and any bookmark of the route land on the table list, the
// catalog's widest view, rather than on a 404.
export const load = () => redirect(307, '/catalog/tables');
