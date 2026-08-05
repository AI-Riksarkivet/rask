import { env } from '$env/dynamic/private';
import type { LayoutServerLoad } from './$types';
import { makeZoneLayoutLoad } from '@rask/api/bff';

const zoneLoad = makeZoneLayoutLoad(env);

// The estate-wide zone layout contract (identity, auth flag, active project, and the RESOLVED
// /v1/me) — owned by @rask/api so all seven zones render one shell by construction, including the
// active-project precedence (route param wins over the cookie on a project's first open, #103).
//
// Home adds ONE field the contract cannot carry: whether a SESSION exists, separate from whether
// the catalog could confirm the identity. `me` is null for BOTH "signed out" and "signed in but
// /v1/me was unreachable / 401 / drifted", and those are opposite situations to a user: one is
// fixed by signing in, the other cannot be. Witnessed 2026-07-28 — a real Dex sign-in as alice
// showed her name in the navbar (which reads the session) while the page below told her to sign in
// (which read only `me`).
export const load: LayoutServerLoad = async (event) => ({
	...(await zoneLoad(event)),
	hasSession: Boolean(event.locals.session),
});
