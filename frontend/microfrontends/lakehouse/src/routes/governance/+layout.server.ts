import { env } from '$env/dynamic/private';
import { error } from '@sveltejs/kit';
import { fetchMe } from '@rask/api';
import type { LayoutServerLoad } from './$types';

/**
 * The estate-admin door, ON THE SERVER — the GOVERNANCE half.
 *
 * Access and Audit answer "who may do what" and "who did what", which is the same privilege as the
 * operational admin area even though it is a different question. They moved out of /admin/ so the
 * breadcrumb would stop contradicting the sidebar (the rail said Governance, the crumb said Admin),
 * and this file moved WITH them: a route rename must never be the thing that drops a door. Deliberately
 * a copy rather than a shared import — a governance route must fail closed on its own file, not on
 * someone remembering to re-export a guard from a sibling directory.
 *
 * The root layout already refuses to render the admin area to a non-admin, but that gate runs in the
 * BROWSER: the server still compiled and shipped every admin route's HTML and JS, and only then did the
 * client fetch `/v1/me` and swap in ForbiddenPage. Nothing was disclosed that the backend would have
 * served — every panel's data comes from the catalog or the lineage plane, which enforce estate-admin
 * in FGA themselves, so a non-admin got an empty shell and a wall of 403s. But "the data layer says no"
 * is one layer, and the admin area is the one place in the estate where it should be three.
 *
 * So the door is here too, and here it is FIRST: an identity that is not `estate_admin` gets a 403 from
 * the load, before a single admin component is rendered or sent. Fail-closed on every ambiguity —
 * `fetchMe` returns null for no token, a 401/403, a timeout, an unreachable catalog or a response that
 * drifted from the frozen contract, and all of those land in the same branch as "not an admin".
 *
 * Auth-off stacks (dev servers, the three non-admin e2e areas) keep their current behaviour: with no
 * OIDC configured there is no identity to check and the backend answers `estate_admin: true` anyway.
 */
export const load: LayoutServerLoad = async ({ locals, fetch, url }) => {
	if (!locals.authEnabled) return { estateAdmin: true };

	// NO SESSION IS A 401, NOT A 403, and the distinction is not pedantry.
	//
	// This used to collapse both into `error(403, 'Admin is estate-admin only')`. So a signed-OUT visitor
	// was told their identity did not hold the estate-admin privilege — an assertion about an identity
	// they did not have. It sent a real debugging session hunting a missing FGA tuple for a user who was
	// simply never authenticated, while OpenFGA had held the correct grant the whole time.
	//
	// Failing closed is right. Asserting a false REASON is not, and this is the one surface in the estate
	// whose entire job is explaining why access resolved the way it did.
	if (!locals.session) {
		error(401, `Sign in to view governance. |${url.pathname}`);
	}

	const me = await fetchMe({
		catalogUrl: env.CATALOG_API ?? 'http://localhost:2333',
		accessToken: locals.session.accessToken,
		fetchFn: fetch,
	});

	// Still fail-closed on every ambiguity — a null `me` (expired token, an unreachable catalog, a
	// drifted contract) is not proof of privilege. But the caller DID present a session, so the honest
	// wording is "your identity does not hold it", which is now true when it is shown.
	if (!me?.estate_admin) {
		error(403, 'Admin is estate-admin only');
	}
	return { estateAdmin: true };
};
