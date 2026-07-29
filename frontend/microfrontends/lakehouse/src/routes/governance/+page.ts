import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

// `/lakehouse/governance` is a GROUP, not a page — Access and Audit own the surfaces. Without this it
// 404'd, and every page under it rendered a "Governance" breadcrumb pointing at that 404: the crumb is
// built from the path segments, so an intermediate segment with no route is a dead link on every child
// page rather than on one URL. The sibling groups (catalog, models, admin, lineage) all answer 200,
// which is why this one looked like a rename slip and not a design choice.
//
// Access rather than Audit because it answers the question the area is FOR — who may do what. Audit is
// the record of what was done, which is the follow-up question, not the entry point.
//
// The area's fail-closed estate-admin door lives in +layout.server.ts and runs BEFORE this, so a denied
// identity gets its 403 here exactly as it would on either child.
export const load = () => redirect(307, `${base}/governance/access`);
