import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

// `/lakehouse/catalog` is a GROUP, not a page — Tables, Namespaces, Warehouses, Projects and
// Storage own the surfaces. It used to render a P0 MIGRATION SCAFFOLD ("The Data zone (P0
// scaffold). Routes move here from apps/web in P3."), which is worse than the 404 the sibling
// `governance` group used to answer: a 200 with developer scaffolding is a page a user can land on
// from the breadcrumb, read, and learn nothing from — and it looked deliberate, so nobody fixed it.
//
// Tables rather than Namespaces because it answers what the area is FOR: what data does this
// estate hold. Namespaces is how it is organised, which is the follow-up question.
export const load = () => redirect(307, `${base}/catalog/tables`);
