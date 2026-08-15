import { error } from '@sveltejs/kit';
import type { LayoutServerLoad } from './$types';

// `/settings/**` — PLATFORM configuration, the third place in the main menu (Home · Projects ·
// Settings), and the door in front of every page under it.
//
// A LAYOUT load, not a page one, and that is the whole point of this file. The gate used to live in
// `settings/+page.server.ts`, which covered exactly one URL: the moment `/settings/access` and
// `/settings/audit` landed here (#105) an ungated child would have served the platform's whole tuple
// store and its cross-tenant audit trail to anyone who typed the path. A door that has to be
// remembered per child is a door that will be forgotten; this one is structural.
//
// The gate is SERVER-SIDE and fail-closed, and it has to be: the navbar hides the Settings entry from
// a non-admin, but hiding a link is not authorization — anyone can type the URL.
//
// The identity comes from the ROOT layout's `me` (the frozen `/v1/me` contract, fetched once per
// request with the session bearer), so every settings page answers from the same read the rest of the
// estate trusts rather than a second, drifting one — and reads it exactly once, rather than once per
// nested load.
//
// 404 rather than 403 on purpose, and only for a resolvable non-admin: a viewer who may not configure
// the platform should not learn that platform configuration exists here. An UNRESOLVED identity gets
// 403 instead — that is a different thing (the catalog could not answer), and answering "no such page"
// would send someone chasing a broken link when their session is the problem.
//
// The pages BELOW still render their own 401/403/501 states from their own reads (the audit viewer's,
// for one). That is not redundancy: this door answers for the ROUTE, those answer for the upstream,
// and on an auth-off stack this load is a no-op while the upstream verdicts still arrive.
export const load: LayoutServerLoad = async ({ parent }) => {
	const { me, hasSession } = await parent();
	if (!me) {
		if (hasSession) {
			error(403, 'Your identity could not be confirmed, so estate settings cannot be opened.');
		}
		error(404, 'Not found');
	}
	// 403 WITH THE REASON, not the 404 this was (#143). A confirmed identity that simply lacks the
	// privilege was told the route did not exist — the strongest form of hiding there is, and now
	// self-contradicting: the main menu shows Settings disabled to exactly this caller, so a 404 here
	// would deny the existence of something the same page just rendered. The wording matches the
	// navbar tooltip and the FGA denial, so all three name one relation on one object.
	if (!me.estate_admin) {
		error(
			403,
			'Estate settings are estate-admin only — your identity lacks can_observe_events on the FGA root.',
		);
	}
	// The signed-in caller in FGA subject form (`user:<sub>`) — an already-typed sub passes through.
	// The settings index prints it; keeping it here means the index needs no load of its own.
	return { meSubject: me.sub.includes(':') ? me.sub : `user:${me.sub}` };
};
