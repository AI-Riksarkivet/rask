import { env } from '$env/dynamic/private';
import type { LayoutServerLoad } from './$types';
import { zoneLayoutLoad } from '@rask/api/bff';
import { fetchMe } from '@rask/api';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Surface the signed-in identity + the auth-enabled flag to the shared shell. `user` is the
// single-sourced sessionToUser projection ("auth is identical in every MFE"); `me` is the frozen
// /v1/me contract fetched BFF-side with the session bearer — RESOLVED here (not streamed): the
// landing page's own load awaits it anyway (the gallery derives from it), so streaming bought no
// earlier paint, and a resolved `me` lets the navbar SSR its final entry set with zero
// skeleton→resolved swap. Degrade, never hang: fetchMe times out internally and answers null
// (signed out / catalog unreachable → base entries only, fail-closed on the admin surfaces).
export const load: LayoutServerLoad = async ({ locals, cookies }) => ({
	...zoneLayoutLoad({ locals, cookies }),
	me: await fetchMe({ catalogUrl: CATALOG_API, accessToken: locals.session?.accessToken }),
	// Whether a SESSION exists, separate from whether the catalog could confirm the identity.
	// `me` is null for BOTH "signed out" and "signed in but /v1/me was unreachable / 401 / drifted",
	// and those are opposite situations to a user: one is fixed by signing in, the other cannot be.
	// Witnessed 2026-07-28 — a real Dex sign-in as alice showed her name in the navbar (which reads
	// the session) while the page below told her to sign in (which read only `me`).
	hasSession: Boolean(locals.session),
});
