import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

// The zone root has no surface of its own — the catalog is the lakehouse landing, and the areas each
// own their own root (`/lakehouse/catalog`, `/lakehouse/lineage`, …). A redirect keeps `/lakehouse`
// meaningful without inventing another page to maintain.
//
// `/catalog`, not `/data`. The area was renamed data -> catalog and this redirect was missed, so the
// zone root 307'd to `/lakehouse/data`, which no longer exists — `/lakehouse` answered 404 for every
// visitor, and so did the "Lakehouse" breadcrumb on every page beneath it. nav-truth.test.ts walks
// nav HREFS and could not see it; a redirect target is not a nav entry. redirect-truth.test.ts now
// covers exactly that gap.
export const load = () => redirect(307, `${base}/catalog`);
