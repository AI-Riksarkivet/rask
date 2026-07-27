import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

// The zone root has no surface of its own — the catalog is the lakehouse landing, and the four areas
// each own their own root (`/lakehouse/data`, `/lakehouse/lineage`, …). A redirect keeps `/lakehouse`
// meaningful without inventing a fifth page to maintain.
export const load = () => redirect(307, `${base}/data`);
