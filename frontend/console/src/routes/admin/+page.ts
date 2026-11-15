import { redirect } from '@sveltejs/kit';

// `/admin` is a GROUP, not a page — Events and DLQ own the surfaces. Events first because it is the
// area's live pulse; the DLQ is what you open once the feed shows something wrong.
export const load = () => redirect(307, '/admin/events');
