import { redirect } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';

// The project overview (batch dashboard) was carved out into the `overview-frontend`
// microfrontend (served at /<project>/overview). A bare /<project> has no page of its
// own here, so land the operator on the overview — the project's home surface.
//
// This is a SERVER load (not a universal +page.ts) on purpose: /<project>/overview
// belongs to a DIFFERENT MFE zone, not loaded in this (catch-all) zone. A redirect()
// from a universal load uses goto() semantics on client-side navs, so SvelteKit's
// router would try to resolve the target within THIS zone (404 → SPA fallback). From
// a server load the redirect is always an HTTP 307 the browser follows — a full
// document navigation that the proxy routes to overview-frontend (see §6, cross-zone).
export const load: PageServerLoad = ({ params }) => {
	redirect(307, `/${params.project}/overview`);
};
