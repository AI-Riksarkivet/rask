import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

// `/lakehouse/admin` is a GROUP, not a page — Events, Streams, DLQ and Tenants own the surfaces.
// Same fix as the `catalog` sibling: it rendered a P0 migration scaffold ("The Admin zone (P0
// scaffold)"), a 200 that told a user nothing while looking intentional.
//
// Events first because it is the area's live pulse — Streams and DLQ are what you open once the
// feed shows something wrong.
export const load = () => redirect(307, `${base}/admin/events`);
